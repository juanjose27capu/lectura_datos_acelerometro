# Adquisición Sísmica - ADS1256 (Versión Python Base)

Este script (`test_adc.py`) es la versión validada y funcional para la adquisición de datos de un acelerómetro Terra Technology SSA-320 a través de un conversor ADC ADS1256 conectado a una Raspberry Pi 3 Model B+. 

El propósito de este código es leer los 3 ejes analógicos (X, Y, Z), asegurar una tasa de muestreo estricta de 100 SPS por canal, y guardar los datos en formato MiniSEED utilizando ObsPy.

## ⚠️ PARÁMETROS INTOCABLES (HARDWARE Y TIMING)
Para evitar daños en el hardware y asegurar la integridad de la forma de onda sísmica, los siguientes parámetros **NO DEBEN SER MODIFICADOS** bajo ninguna circunstancia durante migraciones o refactorizaciones:

### 1. Pines GPIO (Numeración BCM)
*   **DRDY (Data Ready):** `GPIO 23` (Entrada, Pull-Up habilitado).
*   **CS (Chip Select):** `GPIO 8` (Salida, manejado de forma manual, no por el driver SPI interno).

### 2. Configuración del Bus SPI
*   **Velocidad Máxima (SCLK):** `1920000 Hz` (1.92 MHz). *Explicación: El cristal de la placa es de 7.68 MHz. El reloj SPI debe ser como máximo 7.68MHz/4.*
*   **Modo SPI:** `Modo 1` (CPOL=0, CPHA=1). Obligatorio para el ADS1256.
*   **Chip Select:** El pin CS debe mantenerse en estado BAJO (0V) manualmente durante toda la transacción completa (envío de comando + espera de DRDY + lectura de datos). Si el CS sube en el medio, el ADC aborta la operación.

### 3. Configuración del ADS1256 (Registros)
*   **MUX (Multiplexor):** 
    *   Eje X: `IN0` a `AINCOM` (Valor hexadecimal: `0x08`)
    *   Eje Y: `IN1` a `AINCOM` (Valor hexadecimal: `0x18`)
    *   Eje Z: `IN2` a `AINCOM` (Valor hexadecimal: `0x28`)
*   **DRATE (Data Rate interno):** `0xA1` (1000 SPS). *Explicación: Aunque el objetivo son 100 SPS, se usa el filtro interno a 1000 SPS para lograr un tiempo de estabilización (settling time) lo suficientemente bajo que permita conmutar los 3 canales en menos de 4 ms.*
*   **Comandos críticos:** Después de escribir en el registro `DRATE`, es obligatorio enviar el comando `SELFCAL` (`0xF0`) y esperar a que el pin DRDY vuelva a estado BAJO.

### 4. Lógica de Sincronización (100 SPS Reales)
*   El bucle principal tiene un período estricto de **0.01 segundos (10 ms)**.
*   El código debe capturar las 3 muestras de los ejes lo más rápido posible y dormir/bloquear el hilo exactamente el tiempo restante hasta cumplir el ciclo de 10 ms utilizando un reloj de alta precisión del sistema operativo.