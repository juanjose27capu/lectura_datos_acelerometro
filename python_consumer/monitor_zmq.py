#!/usr/bin/env python3
"""
Monitor de validación en tiempo real para el productor ``rust_adc_daemon``.

Se conecta como SUB a ``tcp://127.0.0.1:5555`` y decodifica bloques binarios
de 616 bytes (ver ``protocolo.py`` para el layout exacto Byte a Byte).

Muestra una línea cada 20 muestras.  Como el sistema corre a 100 SPS por eje,
eso equivale a 5 líneas por segundo, igual que el script original
``test_adc.py``.

Uso:
    python monitor_zmq.py

Ctrl+C para salir.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import zmq

from protocolo import MUESTRAS_POR_BLOQUE, decodificar_bloque

ZMQ_ENDPOINT = "tcp://127.0.0.1:5555"
MUESTRAS_ENTRE_IMPRESIONES = 20


def main() -> int:
    contexto = zmq.Context()
    socket = contexto.socket(zmq.SUB)

    # Buffer amplio para tolerar ráfagas mientras la terminal imprime.
    socket.setsockopt(zmq.RCVHWM, 100_000)
    socket.setsockopt(zmq.SUBSCRIBE, b"")
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(ZMQ_ENDPOINT)

    print("=" * 72)
    print("Monitor ZeroMQ - AR.TEST.HNE/HNN/HNZ")
    print(f"Endpoint: {ZMQ_ENDPOINT}")
    print("Esperando bloques binarios de 616 bytes (50 muestras por eje)...")
    print("Ctrl+C para salir.")
    print("=" * 72)
    sys.stdout.flush()

    total_muestras = 0
    ultima_secuencia = None
    primer_bloque = True
    mensajes_invalidos = 0

    try:
        while True:
            payload = socket.recv()
            bloque = decodificar_bloque(payload)

            if bloque is None:
                mensajes_invalidos += 1
                # Avisamos solo las primeras veces para no inundar la consola.
                if mensajes_invalidos <= 5:
                    print(
                        f"ADVERTENCIA: mensaje de {len(payload)} bytes ignorado; "
                        f"se esperaban 616 bytes.",
                        file=sys.stderr,
                    )
                continue

            if primer_bloque:
                inicio_utc = datetime.fromtimestamp(
                    bloque.timestamp_segundos, tz=timezone.utc
                )
                print(
                    f"Primer bloque recibido: sequence={bloque.sequence}, "
                    f"timestamp UTC={inicio_utc.isoformat(timespec='milliseconds')}"
                )
                print("-" * 72)
                primer_bloque = False

            if (
                ultima_secuencia is not None
                and bloque.sequence != ultima_secuencia + 1
            ):
                print(
                    f"ADVERTENCIA: salto de secuencia "
                    f"{ultima_secuencia} -> {bloque.sequence}",
                    file=sys.stderr,
                )
            ultima_secuencia = bloque.sequence

            # x, y, z son vistas numpy int32 de 50 elementos.
            for indice in range(MUESTRAS_POR_BLOQUE):
                total_muestras += 1
                if total_muestras % MUESTRAS_ENTRE_IMPRESIONES == 0:
                    print(
                        f"[Muestra {total_muestras}] "
                        f"Eje X: {int(bloque.x[indice]):8d} cuentas | "
                        f"Eje Y: {int(bloque.y[indice]):8d} cuentas | "
                        f"Eje Z: {int(bloque.z[indice]):8d} cuentas",
                        flush=True,
                    )

    except KeyboardInterrupt:
        print("\nMonitor detenido por el usuario.")
    finally:
        socket.close(linger=0)
        contexto.term()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
