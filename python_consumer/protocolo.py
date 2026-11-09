"""
Protocolo binario compartido entre ``rust_adc_daemon`` y los consumidores Python.

CONTRATO DE BYTES (little-endian, sin padding)
==============================================

El productor Rust publica en ZeroMQ bloques de exactamente 616 bytes que
corresponden a esta estructura ``#[repr(C)]``::

    #[repr(C)]
    struct BloqueSalida {
        sequence:     u64,          //  8 bytes
        timestamp_us: u64,          //  8 bytes, microsegundos desde epoch UNIX (UTC)
        x:            [i32; 50],    // 200 bytes
        y:            [i32; 50],    // 200 bytes
        z:            [i32; 50],    // 200 bytes
    }

Mapa de offsets::

    Offset  Tamaño  Campo            Tipo
    ------  ------  ---------------  -------------------------------
         0       8  sequence         u64 little-endian
         8       8  timestamp_us     u64 little-endian (epoch UNIX, µs)
        16     200  x[0..49]         i32 little-endian
       216     200  y[0..49]         i32 little-endian
       416     200  z[0..49]         i32 little-endian
    ------  ------  ---------------  -------------------------------
    Total   616 bytes

Cada bloque contiene 50 muestras consecutivas de cada eje.  ``sequence``
empieza en 0 y se incrementa en 1 por bloque.  ``timestamp_us`` es la hora
UTC del *primer* sample del bloque.  Las cuentas son los enteros crudos de
24 bits con signo leídos del ADS1256, extendidos a i32.

La decodificación se hace con un único ``numpy.dtype`` estructurado, por lo
que no se recorre byte por byte ni se hacen conversiones innecesarias.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Constantes del protocolo (no confundir con las constantes de hardware).
# ---------------------------------------------------------------------------
SPS = 100.0
MUESTRAS_POR_BLOQUE = 50
BLOQUE_BYTES = 616

# Rango de validez para un timestamp UNIX en microsegundos.
# 2000-01-01 00:00:00 UTC  ->  946684800_000_000
# 2100-01-01 00:00:00 UTC  ->  4102444800_000_000
_TIMESTAMP_US_MIN = 946_684_800_000_000
_TIMESTAMP_US_MAX = 4_102_444_800_000_000

# Layout binario exacto.  Los offsets son 0, 8, 16, 216 y 416.
BLOQUE_DTYPE = np.dtype(
    [
        ("sequence", "<u8"),
        ("timestamp_us", "<u8"),
        ("x", "<i4", (MUESTRAS_POR_BLOQUE,)),
        ("y", "<i4", (MUESTRAS_POR_BLOQUE,)),
        ("z", "<i4", (MUESTRAS_POR_BLOQUE,)),
    ]
)

# Si alguien cambia el layout por error, fallamos al importar en vez de
# interpretar mal los datos sísmicos.
if BLOQUE_DTYPE.itemsize != BLOQUE_BYTES:
    raise RuntimeError(
        f"Layout binario inválido: {BLOQUE_DTYPE.itemsize} bytes, "
        f"se esperaban {BLOQUE_BYTES}."
    )


@dataclass(frozen=True)
class Bloque:
    """Un bloque ya decodificado de 50 muestras por eje."""

    sequence: int
    timestamp_us: int
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray

    @property
    def timestamp_segundos(self) -> float:
        """Timestamp UNIX en segundos (UTC)."""
        return self.timestamp_us / 1_000_000.0


def timestamp_plausible(timestamp_us: int) -> bool:
    """True si el timestamp cae entre los años 2000 y 2100."""
    return _TIMESTAMP_US_MIN <= timestamp_us <= _TIMESTAMP_US_MAX


def decodificar_bloque(payload: bytes) -> Optional[Bloque]:
    """
    Decodifica un mensaje ZeroMQ.

    Devuelve ``None`` si el tamaño no es exactamente 616 bytes.  No se acepta
    ningún otro formato: el contrato con el productor Rust es binario puro.
    """
    if len(payload) != BLOQUE_BYTES:
        return None

    # frombuffer crea una vista sin copiar; el dtype estructurado aplica los
    # offsets y el endianness little-endian de una sola vez.
    registro = np.frombuffer(payload, dtype=BLOQUE_DTYPE, count=1)[0]

    return Bloque(
        sequence=int(registro["sequence"]),
        timestamp_us=int(registro["timestamp_us"]),
        x=registro["x"],
        y=registro["y"],
        z=registro["z"],
    )
