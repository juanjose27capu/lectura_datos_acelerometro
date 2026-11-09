# rust_adc_daemon

Demonio productor en Rust para la adquisición sísmica con un ADS1256 conectado a una Raspberry Pi. Lee los tres ejes del acelerómetro, mantiene un timing estricto de 100 SPS y publica las cuentas crudas por ZeroMQ para que un consumidor en Python (ObsPy/SeisBench) las procese.

Este componente reemplaza la lectura de hardware que actualmente hace `test_adc.py` y forma parte de la arquitectura Productor-Consumidor IPC descrita en `AGENTS.md`.

## Arquitectura

```
ADS1256 (SPI) ──> rust_adc_daemon ──> ZeroMQ PUB (tcp://127.0.0.1:5555) ──> Consumidor Python
                      │
                      └── GPIO manual: CS (BCM 8) y DRDY (BCM 23)
```

- **Productor (este binario):** corre a nivel de sistema, habla exclusivamente con el hardware (SPI/GPIO) y no realiza procesamiento pesado.
- **Consumidor (Python):** vive en `python_consumer/`; se suscribe al socket, arma objetos `Trace` de ObsPy, guarda MiniSEED STEIM2 y alimenta el pipeline de IA/MQTT.

## Parámetros de hardware (no modificar)

| Parámetro | Valor |
| --- | --- |
| DRDY | GPIO 23, entrada con pull-up |
| CS | GPIO 8, salida manejada manualmente |
| Bus SPI | SPI0 |
| Slave Select del driver | `Ss1` (CE1/BCM 7), usado como pin fantasma |
| Velocidad SCLK | 1.92 MHz |
| Modo SPI | Modo 1 (CPOL=0, CPHA=1) |
| DRATE | `0xA1` (1000 SPS internos) |
| MUX eje X | `0x08` (IN0 vs AINCOM) |
| MUX eje Y | `0x18` (IN1 vs AINCOM) |
| MUX eje Z | `0x28` (IN2 vs AINCOM) |
| Período de muestreo | 10 ms (100 SPS) |

### Por qué `SlaveSelect::Ss1`

`Spi::new` con `SlaveSelect::Ss0` hace que el kernel de Linux reclame CE0 (BCM 8), impidiendo que `gpio.get(CS_PIN)` funcione en tiempo de ejecución. Se usa `SlaveSelect::Ss1` (CE1/BCM 7) como pin fantasma para liberar BCM 8 y manejarlo puramente como `OutputPin`.

El CS se mantiene bajo durante toda la transacción (comando + espera de DRDY + lectura) y se sube al finalizar, tal como exige el ADS1256.

## Flujo de ejecución

1. Se inicializan GPIO (DRDY como entrada con pull-up, CS como salida en alto) y SPI0.
2. Se escribe `DRATE = 0xA1`, se envía `SELFCAL` (`0xF0`) y se espera a que DRDY vuelva a bajo.
3. Se crea el contexto ZeroMQ y se bindea un socket `PUB` en `tcp://127.0.0.1:5555`.
4. En cada ciclo de 10 ms:
   - Se leen las cuentas de X, Y y Z conmutando el MUX del ADS1256.
   - Se acumulan 50 muestras consecutivas de cada eje en un `OutputBlock`.
   - La `sequence` del bloque se incrementa en 1 y `timestamp_us` se toma al
     inicio del bloque (UTC, microsegundos desde epoch UNIX).
   - Cada 50 muestras se publica un bloque binario de 616 bytes por ZeroMQ
     con la flag `DONTWAIT`.
   - Si el buffer/HWM de ZeroMQ está lleno, el envío se descarta silenciosamente
     y el ciclo continúa.
   - Cada 20 muestras se imprime un resumen por consola.
5. El período se corrige con `Instant` y `saturating_duration_since` para compensar el tiempo de lectura y no acumular drift.

## Formato del mensaje

Cada mensaje publicado es un bloque binario de exactamente **616 bytes**,
little-endian, equivalente a esta estructura `#[repr(C)]`:

```rust
#[repr(C)]
struct OutputBlock {
    sequence: u64,              //  8 bytes
    timestamp_us: u64,          //  8 bytes, epoch UNIX en microsegundos (UTC)
    x: [i32; 50],               // 200 bytes
    y: [i32; 50],               // 200 bytes
    z: [i32; 50],               // 200 bytes
}                               // total: 616 bytes
```

| Offset | Tamaño | Campo | Tipo |
| ---: | ---: | --- | --- |
| 0 | 8 | `sequence` | `u64` little-endian |
| 8 | 8 | `timestamp_us` | `u64` little-endian |
| 16 | 200 | `x[0..49]` | `i32` little-endian |
| 216 | 200 | `y[0..49]` | `i32` little-endian |
| 416 | 200 | `z[0..49]` | `i32` little-endian |
| **Total** | **616** | — | — |

- `sequence` empieza en 0 y se incrementa en 1 por bloque.
- `timestamp_us` es la hora UTC del primer sample del bloque.
- Las cuentas son valores crudos de 24 bits con signo (extensión de signo a
  `i32`) leídos por `RDATA`. La conversión a unidades físicas (mg, m/s²) y el
  filtrado quedan a cargo del consumidor Python.
- El layout exacto está replicado en `python_consumer/protocolo.py` con un
  `numpy.dtype` estructurado. Cualquier cambio en esta estructura debe
  actualizar ambos lados a la vez.

## Comportamiento de ZeroMQ

- Socket tipo `PUB`, bind a `tcp://127.0.0.1:5555`.
- Envío con `zmq::DONTWAIT`: el hilo de hardware nunca se bloquea esperando red o suscriptores.
- Los errores de envío (`EAGAIN`, buffer lleno, etc.) se ignoran deliberadamente. Un `PUB` sin suscriptores simplemente descarta los mensajes.
- No hay reintentos ni buffering propio: la prioridad es mantener los 100 SPS clavados.

## Requisitos

- Rust (edición 2021).
- `libzmq3-dev` instalado, ya que el crate `zmq` enlaza contra la librería del sistema:
  ```bash
  sudo apt install libzmq3-dev
  ```
- Acceso a SPI y GPIO en la Raspberry Pi (usuario en los grupos correspondientes o ejecución con los permisos necesarios).

## Compilar y ejecutar

```bash
cargo build --release
./target/release/rust_adc_daemon
```

Para desarrollo:

```bash
cargo check
cargo run
```

## Verificar la salida

Consumidor mínimo de prueba con pyzmq y numpy:

```python
import numpy as np
import zmq

BLOQUE = np.dtype([
    ("sequence", "<u8"),
    ("timestamp_us", "<u8"),
    ("x", "<i4", (50,)),
    ("y", "<i4", (50,)),
    ("z", "<i4", (50,)),
])

context = zmq.Context()
socket = context.socket(zmq.SUB)
socket.connect("tcp://127.0.0.1:5555")
socket.setsockopt_string(zmq.SUBSCRIBE, "")

while True:
    bloque = np.frombuffer(socket.recv(), dtype=BLOQUE, count=1)[0]
    print(
        bloque["sequence"],
        bloque["timestamp_us"],
        bloque["x"][0],
        bloque["y"][0],
        bloque["z"][0],
    )
```

Para monitoreo en tiempo real y archivado MiniSEED usá los scripts de
`python_consumer/`.

## Estructura

```
rust_adc_daemon/
├── Cargo.toml
├── src/
│   └── main.rs
└── README.md
```

## Solución de problemas

- **`gpio.get(8)` falla:** verificar que se esté usando `SlaveSelect::Ss1` y que ningún otro proceso esté reclamando BCM 8.
- **No llegan mensajes:** confirmar que el suscriptor esté conectado a `tcp://127.0.0.1:5555`; un `PUB` descarta mensajes si no hay suscriptores.
- **Error de build por `zmq`:** falta `libzmq3-dev` o `pkg-config` no encuentra `libzmq`.
- **Deriva de muestreo:** revisar que el sistema no esté bajo carga extrema; el bucle corrige el período con `Instant`, pero no puede compensar bloqueos largos del sistema operativo.
