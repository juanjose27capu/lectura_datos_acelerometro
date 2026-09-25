import spidev
import RPi.GPIO as GPIO
import time

DRDY = 23
CS_PIN = 8  # Pin lógico del Chip Select (CE0)

GPIO.setmode(GPIO.BCM)
GPIO.setup(DRDY, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(CS_PIN, GPIO.OUT)
GPIO.output(CS_PIN, GPIO.HIGH) # CS arranca arriba

spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 1920000
spi.mode = 1
spi.no_cs = True  # Le decimos a spidev que no se meta con el CS

def leer_canal(canal):
    mux_val = (canal << 4) | 0x08
    
    while GPIO.input(DRDY) == 1:
        pass
    
    # Bajamos el CS a mano para hablar
    GPIO.output(CS_PIN, GPIO.LOW)
    spi.writebytes([0x51, 0x00, mux_val])
    spi.writebytes([0xFC])
    spi.writebytes([0x00])
    # Subimos el CS para que el chip procese
    GPIO.output(CS_PIN, GPIO.HIGH)
    
    while GPIO.input(DRDY) == 1:
        pass
        
    # Volvemos a bajar el CS para pedir los datos y leer de corrido
    GPIO.output(CS_PIN, GPIO.LOW)
    spi.writebytes([0x01])
    time.sleep(0.000007)
    datos = spi.xfer2([0x00, 0x00, 0x00])
    GPIO.output(CS_PIN, GPIO.HIGH) # Terminamos de leer, subimos CS
    
    valor = (datos[0] << 16) | (datos[1] << 8) | datos[2]
    
    if valor & 0x800000:
        valor -= 0x1000000
    
    voltaje = (valor * 5.0) / 8388607.0
    return voltaje

try:
    print("Midiendo... (Apretá Ctrl+C para cortar)")
    while True:
        x = leer_canal(0)
        y = leer_canal(1)
        z = leer_canal(2)
        
        print(f"Eje X: {x:.4f} V  |  Eje Y: {y:.4f} V  |  Eje Z: {z:.4f} V")
        time.sleep(0.2)

except KeyboardInterrupt:
    print("\n¡Listo, cortamos!")
finally:
    spi.close()
    GPIO.cleanup()
