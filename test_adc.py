import spidev
import RPi.GPIO as GPIO
import time
import numpy as np
from obspy import Trace, Stream, UTCDateTime

# --- CONFIGURACIÓN DE PINES ---
DRDY = 23
CS_PIN = 8

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(DRDY, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(CS_PIN, GPIO.OUT)
GPIO.output(CS_PIN, GPIO.HIGH)

# --- CONFIGURACIÓN SPI ---
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1920000
spi.mode = 1
spi.no_cs = True

def configurar_adc():
    # Esperamos que el chip esté listo
    while GPIO.input(DRDY) == 1:
        pass
    
    GPIO.output(CS_PIN, GPIO.LOW)
    # Escribimos en el registro DRATE (0x03): comando WREG (0x50 | 0x03 = 0x53), 1 byte (0x00)
    # Usamos 0xA1 (1000 SPS internos) para poder barrer los 3 canales en menos de 10 ms (100 SPS por eje).
    # Nota: Si leyeras 1 solo canal sin multiplexar, acá pondrías 0x82 (100 SPS internos).
    spi.writebytes([0x53, 0x00, 0xA1])
    time.sleep(0.00001)
    
    # Mandamos SELFCAL (0xF0) para calibrar offset y ganancia a la nueva tasa
    spi.writebytes([0xF0])
    GPIO.output(CS_PIN, GPIO.HIGH)
    
    # Esperamos que termine de calibrar (DRDY vuelve a ponerse en BAJO)
    time.sleep(0.01)
    while GPIO.input(DRDY) == 1:
        pass
    print("ADC configurado y calibrado.")

def leer_cuentas_canal(canal):
    mux_val = (canal << 4) | 0x08
    
    while GPIO.input(DRDY) == 1:
        pass
    
    # Cambiamos el canal en el registro MUX (0x01) y reiniciamos conversión
    GPIO.output(CS_PIN, GPIO.LOW)
    spi.writebytes([0x51, 0x00, mux_val])
    spi.writebytes([0xFC]) # SYNC
    spi.writebytes([0x00]) # WAKEUP
    GPIO.output(CS_PIN, GPIO.HIGH)
    
    while GPIO.input(DRDY) == 1:
        pass
        
    # Leemos los 3 bytes crudos con RDATA (0x01)
    GPIO.output(CS_PIN, GPIO.LOW)
    spi.writebytes([0x01])
    time.sleep(0.000007)
    datos = spi.xfer2([0x00, 0x00, 0x00])
    GPIO.output(CS_PIN, GPIO.HIGH)
    
    # Armamos el entero de 24 bits (Cuentas)
    cuentas = (datos[0] << 16) | (datos[1] << 8) | datos[2]
    if cuentas & 0x800000:
        cuentas -= 0x1000000
        
    return cuentas

# Listas para almacenar todas las muestras de la sesión
datos_x = []
datos_y = []
datos_z = []

FRECUENCIA_OBJETIVO = 100.0 # 100 SPS clavados
PERIODO = 1.0 / FRECUENCIA_OBJETIVO # 0.01 segundos (10 ms)

try:
    configurar_adc()
    print(f"Adquiriendo a {FRECUENCIA_OBJETIVO} SPS por canal... (Apretar Ctrl+C para cortar y guardar el MiniSEED)")
    
    # Guardamos la estampa de tiempo exacta de inicio en UTC (estándar sismológico)
    tiempo_inicio = UTCDateTime()
    proximo_ciclo = time.perf_counter()
    contador_muestras = 0
    
    while True:
        # 1. Leemos los 3 ejes en cuentas enteras
        cx = leer_cuentas_canal(0)
        cy = leer_cuentas_canal(1)
        cz = leer_cuentas_canal(2)
        
        datos_x.append(cx)
        datos_y.append(cy)
        datos_z.append(cz)
        contador_muestras += 1
        
        # 2. Mostramos por terminal cada 20 muestras (5 veces por segundo) para no frenar el reloj
        if contador_muestras % 20 == 0:
            print(f"[Muestra {contador_muestras}] Eje X: {cx:8d} cuentas | Eje Y: {cy:8d} cuentas | Eje Z: {cz:8d} cuentas")
        
        # 3. Control estricto de tiempo para clavar los 100 Hz
        proximo_ciclo += PERIODO
        tiempo_espera = proximo_ciclo - time.perf_counter()
        if tiempo_espera > 0:
            time.sleep(tiempo_espera)
        else:
            # Si el sistema se atrasó en un ciclo, reajustamos la referencia
            proximo_ciclo = time.perf_counter()

except KeyboardInterrupt:
    print("\n\nFinalizando lecturas...\n Generando archivo MiniSEED...")

finally:
    spi.close()
    GPIO.cleanup()
    
    if len(datos_x) > 0:
        # Convertimos a enteros de 32 bits (requerido para compresión STEIM en SeisComP)
        arr_x = np.array(datos_x, dtype=np.int32)
        arr_y = np.array(datos_y, dtype=np.int32)
        arr_z = np.array(datos_z, dtype=np.int32)
        
        # Metadatos base para que SeisComP lo reconozca sin dramas
        # HN* = High Broad Band (100 Hz) + Accelerometer (N)
        def crear_traza(datos, codigo_canal):
            tr = Trace(data=datos)
            tr.stats.network = "AR"
            tr.stats.station = "TEST"
            tr.stats.location = "00"
            tr.stats.channel = codigo_canal
            tr.stats.sampling_rate = FRECUENCIA_OBJETIVO
            tr.stats.starttime = tiempo_inicio
            return tr

        tr_x = crear_traza(arr_x, "HNE") # Este-Oeste (X)
        tr_y = crear_traza(arr_y, "HNN") # Norte-Sur (Y)
        tr_z = crear_traza(arr_z, "HNZ") # Vertical (Z)
        
        st = Stream(traces=[tr_x, tr_y, tr_z])
        
        # Nombre de archivo con fecha y hora UTC
        nombre_archivo = f"lecturas_{tiempo_inicio.strftime('%Y%m%d_%H%M%S')}.mseed"
        
        # Guardamos con codificación STEIM2 (estándar en sismología)
        st.write(nombre_archivo, format="MSEED", encoding="STEIM2")
        
        duracion = len(datos_x) / FRECUENCIA_OBJETIVO
        print(f"Se guardaron {len(datos_x)} muestras por canal ({duracion:.2f} s).")
        print(f"Archivo generado: {nombre_archivo}")
