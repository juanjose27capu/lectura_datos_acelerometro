from obspy import read

# 1. Cargamos el archivo MiniSEED que generaste
# (Podés usar "*.mseed" para que agarre el que tengas en la carpeta)
st = read("lecturas_20261002_232421.mseed")

# 2. Imprimimos el resumen técnico (Red, Estación, Frecuencia y Cantidad de muestras)
print("=== RESUMEN DEL ARCHIVO MINISEED ===")
print(st)
print("=" * 36)

# 3. Verificamos que los datos estén en CUENTAS (enteros int32)
for tr in st:
    print(f"\nCanal {tr.stats.channel} | Tipo de dato: {tr.data.dtype}")
    print(f"Primeras 5 muestras (en cuentas): {tr.data[:5]}")
    print(f" Mínimo: {tr.data.min()} cuentas | Máximo: {tr.data.max()} cuentas")

# 4. Graficamos las 3 trazas estilo sismograma oficial
# Le pasamos Autoscale y color negro como se usa en sismología
st.plot(size=(1000, 600), equal_scale=False)