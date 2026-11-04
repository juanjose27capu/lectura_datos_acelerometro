use std::error::Error;
use std::thread;
use std::time::{Duration, Instant};

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

    let mut proximo_ciclo = Instant::now();
    let mut contador_muestras: u64 = 0;

    loop {
        let cuentas_x = leer_cuentas(&mut spi, &mut drdy_pin, &mut cs_pin, 0)?;
        let cuentas_y = leer_cuentas(&mut spi, &mut drdy_pin, &mut cs_pin, 1)?;
        let cuentas_z = leer_cuentas(&mut spi, &mut drdy_pin, &mut cs_pin, 2)?;

        contador_muestras += 1;
        if contador_muestras % SAMPLES_PER_PRINT == 0 {
            println!(
                "[Muestra {}] Eje X: {:8} cuentas | Eje Y: {:8} cuentas | Eje Z: {:8} cuentas",
                contador_muestras, cuentas_x, cuentas_y, cuentas_z
            );
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
    cs_pin.set_low();
    while drdy_pin.is_high() {}

    let mut datos = [0u8; 3];
    spi.transfer(&mut datos, &[ADC_RDATA_COMMAND, 0x00, 0x00])?;

    let mut cuentas = ((datos[0] as i32) << 16) | ((datos[1] as i32) << 8) | (datos[2] as i32);
    if (cuentas & 0x800000) != 0 {
        cuentas -= 0x1000000;
    }

    cs_pin.set_high();

    Ok(cuentas)
}
