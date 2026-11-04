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
- **Consumidor (Python, a futuro):** se suscribe al socket, arma objetos `Trace` de ObsPy, aplica filtros, guarda MiniSEED y alimenta el pipeline de IA/MQTT.

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
   - Se leen las cuentas de X, Y y Z conmuntando el MUX del ADS1256.
   - Se formatea `"{x},{y},{z}"` y se publica por ZeroMQ con la flag `DONTWAIT`.
   - Si el buffer/HWM de ZeroMQ está lleno, el envío se descarta silenciosamente y el ciclo continúa.
   - Cada 20 muestras se imprime un resumen por consola.
5. El período se corrige con `Instant` y `saturating_duration_since` para compensar el tiempo de lectura y no acumular drift.

## Formato del mensaje

Cada mensaje publicado es una cadena ASCII liviana, sin JSON, con tres enteros con signo separados por comas:

```
<cuentas_x>,<cuentas_y>,<cuentas_z>
```

Ejemplo:

```
-123456,7890,42
```

Las cuentas son valores crudos de 24 bits con signo (extensión de signo a `i32`) leídos por `RDATA`. La conversión a unidades físicas (mg, m/s²) y el filtrado quedan a cargo del consumidor.

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

Consumidor mínimo de prueba con pyzmq:

```python
import zmq

context = zmq.Context()
socket = context.socket(zmq.SUB)
socket.connect("tcp://127.0.0.1:5555")
socket.setsockopt_string(zmq.SUBSCRIBE, "")

while True:
    x, y, z = socket.recv_string().split(",")
    print(int(x), int(y), int(z))
```

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
