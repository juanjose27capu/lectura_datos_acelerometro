# Directivas de Arquitectura y Refactorización (Proyecto Sismología)

## Contexto del Proyecto
Este proyecto desarrolla una estación sismológica de alta precisión utilizando una Raspberry Pi, un acelerómetro analógico y un conversor ADC de 24 bits (ADS1256). Los datos procesados se envían a un pipeline de análisis sismológico (SeisBench + PhaseNet) e infraestructura MQTT.

## Arquitectura de Ingesta (El Objetivo)
Actualmente, la lectura del hardware y el empaquetado de datos (ObsPy/MiniSEED) conviven en un solo script de Python (`test_adc.py`). Esto es inviable a largo plazo debido a los tiempos impredecibles del *Garbage Collector* de Python y el *Global Interpreter Lock* (GIL), que provocan pérdida de muestras sísmicas (jitter).

**La solución es una arquitectura Productor-Consumidor IPC (Inter-Process Communication):**
1.  **Productor (Rust):** Un demonio que corre a nivel de sistema. Se comunica exclusivamente con el hardware (SPI/GPIO), mantiene el timing estricto de muestreo, y empuja los datos crudos (cuentas int32 de los 3 ejes) a un *Named Pipe* (FIFO) o socket local.
2.  **Consumidor (Python):** Lee pasivamente el flujo de datos del IPC, genera objetos `Trace` de ObsPy, aplica filtros, guarda en MiniSEED y se comunica con los modelos de IA.

## Tarea Asignada al Agente
Tu objetivo es **iniciar el desarrollo de la etapa Productor en Rust**. Debes replicar exactamente el comportamiento de lectura a bajo nivel y sincronización temporal (timing) descrito en el archivo `test_adc.py` y detallado en `README.md`.

### Reglas de Oro para la Migración:
1.  **Cero invención de hardware:** Los pines GPIO y la configuración del bus SPI detallados en `README.md` son SAGRADOS. Un cambio erróneo en los valores hexadecimales o de pines puede causar un cortocircuito y freír la placa. Limítate a mapear la lógica de Python a Rust.
2.  **Librería sugerida:** Utiliza la caja `rppal` (Raspberry Pi Peripheral Access Library) de Rust para el manejo asíncrono o sincrónico de GPIO y SPI.
3.  **Salida de datos:** En esta primera iteración, en lugar de guardar un archivo MiniSEED, el código Rust simplemente debe imprimir los valores por consola (simulando la salida que luego irá al Named Pipe) manteniendo un tiempo de ejecución estricto que garantice los 100 SPS.
4.  **Aislamiento:** El proyecto en Rust debe vivir en su propio subdirectorio aislado para no pisar el código de Python que aún se necesita.