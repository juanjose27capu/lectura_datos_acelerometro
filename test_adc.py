import spidev
import RPi.GPIO as GPIO
import time

#Conexion al GPIO 35 con DRDY

DRDY = 23
GPIO.setmode(GPIO.BCM)
GPIO.setup(DRDY, GPIO.IN, pull_up_down=GPIO.PUD_UP)

#Inicializacion del bus SPI

spi = spidev.SpiDev()
spi.open(0,0)
spi.max_speed_hz=1920000
spi.mode = 1

def leer_canal(canal):

	mux_val = (canal << 4) | 0x08
	
	while GPIO.input(DRDY) == 1:
		pass

	spi.writebytes([0x51,0x00, mux_val])
	
	spi.writebytes([0xFC])
	spi.writebytes([0x00])

	while GPIO.input(DRDY) == 1:
		pass 

	spi.writebytes([0x01])
	time.sleep(0.000007)

	datos=spi.xfer([0x00, 0x00, 0x00])

	valor = (datos[0] << 16) | (datos[1] << 8) | datos[2]

	if valor & 0x800000:
		valor -= 0x1000000

	voltaje = (valor * 5.0) / 8388607.0
	return voltaje

try: 

	print("Midiendo...")
	while True:
		
		x = leer_canal(0)
		y = leer_canal(2)
		z = leer_canal(3)

		print(f"Eje X: {x:.4f} V | Ejer Y: {y:.4f} V | Eje Z: {z:.4f} V")			
		time.sleep(0.2)

except KeyboardInterrupt:
	print("\n¡Listo!")
finally: 
	spi.close()
	GPIO.cleanup()
