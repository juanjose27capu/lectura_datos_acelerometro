use std::error::Error;
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rppal::gpio::{Gpio, InputPin, OutputPin};
use rppal::spi::{Bus, Mode, SlaveSelect, Spi};

const DRDY_PIN: u8 = 23;
const CS_PIN: u8 = 8;
const SPI_BUS: Bus = Bus::Spi0;
const SPI_SLAVE_SELECT: SlaveSelect = SlaveSelect::Ss1;
const SPI_CLOCK_SPEED_HZ: u32 = 1_920_000;
const ADC_DRATE_WRITE_COMMAND: u8 = 0x53;
const ADC_DRATE_VALUE: u8 = 0xA1;
const ADC_SELFCAL_COMMAND: u8 = 0xF0;
const ADC_MUX_WRITE_COMMAND: u8 = 0x51;
const ADC_SYNC_COMMAND: u8 = 0xFC;
const ADC_WAKEUP_COMMAND: u8 = 0x00;
const ADC_RDATA_COMMAND: u8 = 0x01;
const SAMPLES_PER_SECOND: f64 = 100.0;
const SAMPLE_PERIOD: Duration = Duration::from_millis(10);
const SAMPLES_PER_PRINT: u64 = 20;
const SAMPLES_PER_BLOCK: usize = 50;
const BYTES_PER_BLOCK: usize = 8 + 8 + 3 * SAMPLES_PER_BLOCK * 4;
const ZMQ_ENDPOINT: &str = "tcp://127.0.0.1:5555";

/// Bloque binario publicado por ZeroMQ.
///
/// Contrato compartido con `python_consumer/protocolo.py`:
///
/// Offset  Tamaño  Campo
/// 0       8       sequence      u64 little-endian
/// 8       8       timestamp_us  u64 little-endian (epoch UNIX, microsegundos)
/// 16      200     x[0..49]      i32 little-endian
/// 216     200     y[0..49]      i32 little-endian
/// 416     200     z[0..49]      i32 little-endian
/// Total:  616 bytes
///
/// `#[repr(C)]` garantiza el orden de los campos y evita que Rust los
/// reordene. En ARM little-endian (Raspberry Pi) no hay bytes extra porque
/// el tamaño total ya es múltiplo de la alineación de u64.
#[repr(C)]
#[allow(dead_code)] // los campos se leen como bytes crudos, no campo por campo
struct OutputBlock {
    sequence: u64,
    timestamp_us: u64,
    x: [i32; SAMPLES_PER_BLOCK],
    y: [i32; SAMPLES_PER_BLOCK],
    z: [i32; SAMPLES_PER_BLOCK],
}

// Falla en tiempo de compilación si el layout deja de ser de 616 bytes.
const _: [(); BYTES_PER_BLOCK] = [(); std::mem::size_of::<OutputBlock>()];

fn main() -> Result<(), Box<dyn Error>> {
    let gpio = Gpio::new()?;
    let mut drdy_pin = gpio.get(DRDY_PIN)?.into_input_pullup();
    let mut cs_pin = gpio.get(CS_PIN)?.into_output_high();
    let mut spi = Spi::new(SPI_BUS, SPI_SLAVE_SELECT, SPI_CLOCK_SPEED_HZ, Mode::Mode1)?;

    println!("Adquiriendo a {} SPS por canal...", SAMPLES_PER_SECOND);

    cs_pin.set_low();
    spi.write(&[ADC_DRATE_WRITE_COMMAND, 0x00, ADC_DRATE_VALUE])?;
    thread::sleep(Duration::from_micros(10));
    cs_pin.set_low();
    spi.write(&[ADC_SELFCAL_COMMAND])?;
    cs_pin.set_high();
    thread::sleep(Duration::from_millis(10));
    while drdy_pin.is_high() {}

    let zmq_context = zmq::Context::new();
    let zmq_publisher = zmq_context.socket(zmq::PUB)?;
    zmq_publisher.bind(ZMQ_ENDPOINT)?;

    let mut proximo_ciclo = Instant::now();
    let mut contador_muestras: u64 = 0;
    let mut secuencia_bloque: u64 = 0;
    let mut indice_bloque: usize = 0;
    let mut bloque = OutputBlock {
        sequence: 0,
        timestamp_us: 0,
        x: [0; SAMPLES_PER_BLOCK],
        y: [0; SAMPLES_PER_BLOCK],
        z: [0; SAMPLES_PER_BLOCK],
    };

    loop {
        // El timestamp corresponde a la primera muestra del bloque, tomado
        // antes de leer los tres canales.
        if indice_bloque == 0 {
            bloque.sequence = secuencia_bloque;
            bloque.timestamp_us = ahora_micros();
        }

        let cuentas_x = leer_cuentas(&mut spi, &mut drdy_pin, &mut cs_pin, 0)?;
        let cuentas_y = leer_cuentas(&mut spi, &mut drdy_pin, &mut cs_pin, 1)?;
        let cuentas_z = leer_cuentas(&mut spi, &mut drdy_pin, &mut cs_pin, 2)?;

        bloque.x[indice_bloque] = cuentas_x;
        bloque.y[indice_bloque] = cuentas_y;
        bloque.z[indice_bloque] = cuentas_z;
        indice_bloque += 1;

        contador_muestras += 1;
        if contador_muestras % SAMPLES_PER_PRINT == 0 {
            println!(
                "[Muestra {}] Eje X: {:8} cuentas | Eje Y: {:8} cuentas | Eje Z: {:8} cuentas",
                contador_muestras, cuentas_x, cuentas_y, cuentas_z
            );
        }

        // Publicamos un bloque binario cada 50 muestras (0.5 s a 100 SPS).
        if indice_bloque == SAMPLES_PER_BLOCK {
            let bytes = unsafe {
                std::slice::from_raw_parts(
                    &bloque as *const OutputBlock as *const u8,
                    std::mem::size_of::<OutputBlock>(),
                )
            };
            let _ = zmq_publisher.send(bytes, zmq::DONTWAIT);

            secuencia_bloque = secuencia_bloque.wrapping_add(1);
            indice_bloque = 0;
        }

        proximo_ciclo += SAMPLE_PERIOD;
        let ahora = Instant::now();
        let tiempo_espera = proximo_ciclo.saturating_duration_since(ahora);
        if tiempo_espera > Duration::from_secs(0) {
            thread::sleep(tiempo_espera);
        } else {
            proximo_ciclo = ahora;
        }
    }
}

fn ahora_micros() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duracion| duracion.as_micros() as u64)
        .unwrap_or(0)
}

fn leer_cuentas(
    spi: &mut Spi,
    drdy_pin: &mut InputPin,
    cs_pin: &mut OutputPin,
    canal: u8,
) -> Result<i32, Box<dyn Error>> {
    let mux_val = (canal << 4) | 0x08;

    cs_pin.set_low();
    while drdy_pin.is_high() {}

    spi.write(&[ADC_MUX_WRITE_COMMAND, 0x00, mux_val])?;
    spi.write(&[ADC_SYNC_COMMAND])?;
    spi.write(&[ADC_WAKEUP_COMMAND])?;
    while drdy_pin.is_high() {}

    spi.write(&[ADC_RDATA_COMMAND])?;
    thread::sleep(Duration::from_micros(7));

    let mut datos = [0u8; 3];
    spi.read(&mut datos)?;

    let mut cuentas = ((datos[0] as i32) << 16) | ((datos[1] as i32) << 8) | (datos[2] as i32);
    if (cuentas & 0x800000) != 0 {
        cuentas -= 0x1000000;
    }

    cs_pin.set_high();

    Ok(cuentas)
}
