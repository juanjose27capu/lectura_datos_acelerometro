# Plan: Corregir violación del delay t6 (RDATA) en `leer_cuentas`

**Estado:** Aprobado por el usuario. Pendiente de ejecución (modo plan activo bloquea la edición de código).

## Problema

`rust_adc_daemon/src/main.rs:163` envía el comando `RDATA` (0x01) y lee los 3 bytes de datos en la **misma transacción SPI**:

```rust
spi.transfer(&mut datos, &[ADC_RDATA_COMMAND, 0x00, 0x00])?;
```

El datasheet del ADS1256 exige **t6 = 50·τCLKIN ≈ 6.51 µs** entre el comando `RDATA` y el primer flanco de reloj de salida de datos. Al no respetarse, el monitor recibe patrones binarios de hardware (64, 65, 320) en lugar de cuentas del ADC.

Referencia correcta en Python: `test_adc.py:64-66` (`writebytes([0x01])` → `sleep(7µs)` → `xfer2([0,0,0])`).

## Cambio aprobado

Solo se modifica `leer_cuentas` (rust_adc_daemon/src/main.rs:145-173). No se tocan constantes de hardware, el bloque binario de 616 bytes, la secuencia de init (DRATE/SELFCAL) ni el lazo de 100 SPS.

### Diff exacto

```diff
     spi.write(&[ADC_MUX_WRITE_COMMAND, 0x00, mux_val])?;
     spi.write(&[ADC_SYNC_COMMAND])?;
     spi.write(&[ADC_WAKEUP_COMMAND])?;
-    cs_pin.set_low();
     while drdy_pin.is_high() {}
 
+    spi.write(&[ADC_RDATA_COMMAND])?;
+    thread::sleep(Duration::from_micros(7));
+
     let mut datos = [0u8; 3];
-    spi.transfer(&mut datos, &[ADC_RDATA_COMMAND, 0x00, 0x00])?;
+    spi.read(&mut datos)?;
```

### Flujo resultante (CS en LOW durante toda la secuencia)

1. Escritura del registro MUX (`0x51, 0x00, mux_val`) para seleccionar el eje.
2. `SYNC` (0xFC) y `WAKEUP` (0x00).
3. Espera de DRDY en LOW.
4. `RDATA` (0x01) como transacción aislada: `spi.write(&[0x01])`.
5. **Pausa t6:** `thread::sleep(Duration::from_micros(7))`.
6. Lectura de los 3 bytes puros: `spi.read(&mut datos)`.
7. Ensamblado Big-Endian de 24 bits con extensión de signo (sin cambios; ya era correcto).

Cambios concretos:
- Eliminar el `cs_pin.set_low()` redundante de main.rs:159 (CS ya está en LOW; el flujo debe ser estricto).
- Aislar `RDATA` en su propia transacción `spi.write`.
- Insertar la pausa obligatoria de 7 µs (t6) antes de leer.
- Leer los 3 bytes con `spi.read(&mut datos)` (equivalente al `xfer2([0,0,0])` de Python).
- Mantener el ensamblado i32 con extensión de signo de 24 bits y el `cs_pin.set_high()` final.

## Verificación

1. `cargo check` (compilación).
2. `cargo build --release`.
3. Prueba en hardware (Raspberry Pi + ADS1256): los valores impresos deben ser cuentas plausibles del acelerómetro (rango de ±8.388.607 a escala completa), no los patrones 64/65/320.
