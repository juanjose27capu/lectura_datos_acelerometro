import spidev
import RPi.GPIO as GPIO
import time
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from obspy import Trace
from collections import deque

# --- CONFIGURACIÓN DEL ADC ---
DRDY = 23
CS_PIN = 8

GPIO.setmode(GPIO.BCM)
GPIO.setup(DRDY, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(CS_PIN, GPIO.OUT)
GPIO.output(CS_PIN, GPIO.HIGH)

spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1920000
spi.mode = 1
spi.no_cs = True

def leer_canal_x():
    # Leemos solo el canal 0 (Eje X) para hacer el gráfico fluido
    mux_val = (0 << 4) | 0x08
    
    while GPIO.input(DRDY) == 1:
        pass
    
    GPIO.output(CS_PIN, GPIO.LOW)
    spi.writebytes([0x51, 0x00, mux_val])
    spi.writebytes([0xFC])
    spi.writebytes([0x00])
    GPIO.output(CS_PIN, GPIO.HIGH)
    
    while GPIO.input(DRDY) == 1:
        pass
        
    GPIO.output(CS_PIN, GPIO.LOW)
    spi.writebytes([0x01])
    time.sleep(0.000007)
    datos = spi.xfer2([0x00, 0x00, 0x00])
    GPIO.output(CS_PIN, GPIO.HIGH)
    
    valor = (datos[0] << 16) | (datos[1] << 8) | datos[2]
    if valor & 0x800000:
        valor -= 0x1000000
        
    return (valor * 5.0) / 8388607.0

# --- CONFIGURACIÓN DEL GRÁFICO Y OBSPY ---
CANTIDAD_MUESTRAS = 150
# Usamos un deque para tener una ventana de datos que se desliza sola
buffer_y = deque([2.5] * CANTIDAD_MUESTRAS, maxlen=CANTIDAD_MUESTRAS)

fig, ax = plt.subplots()
ax.set_title("Sismógrafo en Vivo (Eje X)")
ax.set_ylabel("Amplitud (Voltios centrados)")
ax.set_ylim(-1.5, 1.5) # Rango de oscilación esperado tras sacar los 2.5V
line, = ax.plot(np.zeros(CANTIDAD_MUESTRAS), color='r')

def actualizar_grafico(frame):
    # 1. Traemos el voltaje crudo y lo metemos al buffer
    voltaje_crudo = leer_canal_x()
    buffer_y.append(voltaje_crudo)
    
    # 2. Magia de ObsPy: armamos la traza con la ventana actual
    traza = Trace(data=np.array(buffer_y))
    traza.stats.sampling_rate = 50.0 # Tasa aproximada de este bucle en Python
    
    # 3. Procesamiento sísmico en tiempo real
    # demean elimina el offset de corriente continua (los 2.5V de reposo)
    traza.detrend('demean')
    
    # Podés descomentar la línea de abajo para meterle un filtro pasabanda
    # traza.filter('bandpass', freqmin=1.0, freqmax=10.0)
    
    # 4. Actualizamos la línea del gráfico con los datos procesados
    line.set_ydata(traza.data)
    return line,

# Intervalo en 20ms para intentar graficar a unos 50 FPS
ani = animation.FuncAnimation(fig, actualizar_grafico, interval=20, blit=True)

try:
    print("Levantando la interfaz gráfica...")
    plt.show()
except KeyboardInterrupt:
    print("\nCerrando todo...")
finally:
    spi.close()
    GPIO.cleanup()
