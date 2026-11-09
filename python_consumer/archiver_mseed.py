#!/usr/bin/env python3
"""
Archivador continuo MiniSEED para el productor ``rust_adc_daemon``.

Arquitectura:
    - Hilo receptor ZeroMQ: recibe bloques binarios de 616 bytes sin bloquearse
      en el disco.
    - Hilo escritor ObsPy: arma el Stream y guarda el MiniSEED STEIM2.

Cada ventana acumula exactamente 60.000 muestras por eje (600 segundos a
100 SPS).  Al completarse, los buffers se entregan al hilo escritor y el hilo
receptor continúa inmediatamente con buffers nuevos, para no perder muestras
mientras se comprime/escribe el archivo.

El layout binario está documentado Byte a Byte en ``protocolo.py``.

Uso:
    python archiver_mseed.py

Ctrl+C (o SIGTERM) guarda un archivo parcial con las muestras acumuladas
hasta ese momento.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import zmq
from obspy import Stream, Trace, UTCDateTime

from protocolo import (
    SPS,
    Bloque,
    decodificar_bloque,
    timestamp_plausible,
)

# ---------------------------------------------------------------------------
# Parámetros del consumidor.  Las constantes de hardware (100 SPS, pines BCM,
# 1.92 MHz, MUX, DRATE) pertenecen exclusivamente al productor Rust.
# ---------------------------------------------------------------------------
ZMQ_ENDPOINT = "tcp://127.0.0.1:5555"
DURACION_VENTANA_S = 600.0
MUESTRAS_POR_VENTANA = int(round(SPS * DURACION_VENTANA_S))  # 60.000
RED = "AR"
ESTACION = "TEST"
LOCALIZACION = "00"
CANALES = ("HNE", "HNN", "HNZ")
SALIDA_POR_DEFECTO = Path.home() / "Escritorio"


# ---------------------------------------------------------------------------
# Reloj / NTP
# ---------------------------------------------------------------------------
def _ntp_sincronizado() -> Optional[bool]:
    """Consulta systemd-timesyncd/chrony vía timedatectl. None si no se puede."""
    try:
        resultado = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None

    if resultado.returncode != 0:
        return None
    return resultado.stdout.strip().lower() == "yes"


def esperar_reloj_ntp(max_espera_s: float) -> None:
    """
    Verifica que el reloj de la RPi esté sincronizado antes de fijar el ancla
    UTC.  La RPi 3 B+ no tiene RTC: si arranca sin red, la hora puede ser la
    de la última sesión o directamente 1970.
    """
    ahora_utc = datetime.now(timezone.utc)
    ahora_local = datetime.now().astimezone()
    print(f"Reloj del sistema (UTC):   {ahora_utc.isoformat(timespec='seconds')}")
    print(f"Reloj del sistema (local): {ahora_local.isoformat(timespec='seconds')}")

    if not sys.platform.startswith("linux"):
        print("NTP: verificación omitida (no es Linux).")
        return

    estado = _ntp_sincronizado()

    if estado is True:
        print("NTP: reloj sincronizado.")
    elif estado is False:
        print("NTP: reloj NO sincronizado.")
        if max_espera_s > 0:
            print(f"Esperando hasta {max_espera_s:.0f} s a que NTP sincronice...")
            limite = time.monotonic() + max_espera_s
            while time.monotonic() < limite:
                time.sleep(1.0)
                if _ntp_sincronizado() is True:
                    estado = True
                    break
            if estado is True:
                print("NTP: reloj sincronizado.")
            else:
                print(
                    "ADVERTENCIA: NTP sigue sin sincronizar. Los timestamps "
                    "pueden ser incorrectos.\n"
                    "  Verificá con:  timedatectl status\n"
                    "  Activá NTP con: sudo timedatectl set-ntp true\n"
                    "  Hora UTC actual: date -u",
                    file=sys.stderr,
                )
    else:
        print(
            "ADVERTENCIA: no se pudo consultar timedatectl; "
            "no se puede verificar NTP.",
            file=sys.stderr,
        )

    if time.time() < 1_704_067_200:  # 2024-01-01 UTC
        print(
            "ADVERTENCIA: el reloj parece anterior a 2024. "
            "Revisá la sincronización NTP antes de confiar en los timestamps.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Hilo escritor
# ---------------------------------------------------------------------------
class EscritorMiniSEED(threading.Thread):
    """Consume buffers completos/parciales y escribe MiniSEED STEIM2."""

    def __init__(self, directorio_salida: Path) -> None:
        super().__init__(name="escritor-mseed", daemon=False)
        self.directorio_salida = directorio_salida
        self.cola: queue.Queue = queue.Queue()

    def encolar(
        self,
        x: np.ndarray,
        y: np.ndarray,
        z: np.ndarray,
        inicio: UTCDateTime,
        motivo: str,
    ) -> None:
        self.cola.put((x, y, z, inicio, motivo))

    def detener(self) -> None:
        """Espera a que se escriba todo lo pendiente y termina el hilo."""
        self.cola.put(None)
        self.cola.join()
        self.join()

    def run(self) -> None:
        while True:
            elemento = self.cola.get()
            try:
                if elemento is None:
                    break
                self._guardar(*elemento)
            except Exception:
                print(
                    "ERROR: no se pudo escribir un archivo MiniSEED:",
                    file=sys.stderr,
                )
                traceback.print_exc()
            finally:
                self.cola.task_done()

    def _guardar(
        self,
        x: np.ndarray,
        y: np.ndarray,
        z: np.ndarray,
        inicio: UTCDateTime,
        motivo: str,
    ) -> None:
        if len(x) == 0:
            return
        if not (len(x) == len(y) == len(z)):
            raise ValueError(
                f"Longitudes inconsistentes: X={len(x)}, Y={len(y)}, Z={len(z)}"
            )

        self.directorio_salida.mkdir(parents=True, exist_ok=True)

        base = f"sismo_{inicio.strftime('%Y%m%d_%H%M%S')}"
        destino = self.directorio_salida / f"{base}.mseed"

        # Evita pisar un archivo si se reinicia el servicio dentro del mismo
        # segundo.  El nombre normal sigue siendo sismo_YYYYMMDD_HHMMSS.mseed.
        if destino.exists():
            for sufijo in range(1, 10_000):
                candidato = self.directorio_salida / f"{base}_{sufijo:03d}.mseed"
                if not candidato.exists():
                    destino = candidato
                    break

        trazas = []
        for datos, canal in zip((x, y, z), CANALES):
            traza = Trace(data=np.ascontiguousarray(datos, dtype=np.int32))
            traza.stats.network = RED
            traza.stats.station = ESTACION
            traza.stats.location = LOCALIZACION
            traza.stats.channel = canal
            traza.stats.sampling_rate = SPS
            traza.stats.starttime = inicio
            trazas.append(traza)

        stream = Stream(traces=trazas)
        temporal = destino.with_name(destino.name + ".part")

        try:
            stream.write(
                str(temporal),
                format="MSEED",
                encoding="STEIM2",
            )
            os.replace(temporal, destino)
        except Exception:
            try:
                temporal.unlink(missing_ok=True)
            except OSError:
                pass
            raise

        duracion = len(x) / SPS
        print(
            f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] "
            f"{motivo}: {destino.name} | "
            f"{len(x)} muestras/eje ({duracion:.2f} s) | "
            f"inicio {inicio.isoformat()} UTC"
        )
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Acumulador de ventanas
# ---------------------------------------------------------------------------
class Archivador:
    """Acumula bloques en buffers preasignados y dispara las ventanas."""

    def __init__(self, escritor: EscritorMiniSEED, hora_arranque: UTCDateTime) -> None:
        self.escritor = escritor
        self.hora_arranque = hora_arranque

        self.buf_x = np.empty(MUESTRAS_POR_VENTANA, dtype=np.int32)
        self.buf_y = np.empty(MUESTRAS_POR_VENTANA, dtype=np.int32)
        self.buf_z = np.empty(MUESTRAS_POR_VENTANA, dtype=np.int32)
        self.n = 0

        self.inicio: Optional[UTCDateTime] = None
        self.ultima_secuencia: Optional[int] = None
        self.total_muestras = 0
        self.ventanas_guardadas = 0
        self.mensajes_invalidos = 0

        self.tolerancia_ts_s = _float_env("SISMO_TS_TOLERANCIA", 120.0)

    # -- API ---------------------------------------------------------------
    def procesar(self, payload: bytes) -> None:
        bloque = decodificar_bloque(payload)
        if bloque is None:
            self.mensajes_invalidos += 1
            if self.mensajes_invalidos <= 5:
                print(
                    f"ADVERTENCIA: mensaje de {len(payload)} bytes ignorado; "
                    f"se esperaban 616.",
                    file=sys.stderr,
                )
            return

        if self.inicio is None:
            self.inicio = self._elegir_inicio(bloque)

        # Secuencia: detectamos bloques perdidos por HWM/red o reinicios del
        # productor.  No inventamos muestras: cerramos un parcial y seguimos.
        if self.ultima_secuencia is not None:
            secuencia_esperada = self.ultima_secuencia + 1
            if bloque.sequence != secuencia_esperada:
                print(
                    f"ADVERTENCIA: salto de secuencia "
                    f"{self.ultima_secuencia} -> {bloque.sequence}",
                    file=sys.stderr,
                )
                if self.n > 0:
                    self._cerrar_ventana("parcial por salto de secuencia")
                if timestamp_plausible(bloque.timestamp_us):
                    self.inicio = UTCDateTime(ns=bloque.timestamp_us * 1_000)

        self.ultima_secuencia = bloque.sequence
        self._acumular(bloque.x, bloque.y, bloque.z)

    def cerrar(self) -> None:
        """Guarda lo acumulado antes de apagar el servicio."""
        if self.n > 0:
            self._cerrar_ventana("parcial por cierre del servicio")
        else:
            print("No hay muestras pendientes para guardar.")

    # -- Internos ----------------------------------------------------------
    def _elegir_inicio(self, bloque: Bloque) -> UTCDateTime:
        if not timestamp_plausible(bloque.timestamp_us):
            print(
                "ADVERTENCIA: timestamp de Rust fuera del rango epoch válido; "
                "se usa el ancla UTC del consumidor.",
                file=sys.stderr,
            )
            return self.hora_arranque

        inicio_bloque = UTCDateTime(ns=bloque.timestamp_us * 1_000)
        diferencia = abs(float(inicio_bloque) - time.time())

        if diferencia <= self.tolerancia_ts_s:
            print(
                f"Timestamp de Rust validado (desvío {diferencia:.3f} s). "
                f"Inicio de la primera ventana: {inicio_bloque.isoformat()} UTC"
            )
            return inicio_bloque

        print(
            f"ADVERTENCIA: el timestamp de Rust difiere {diferencia:.1f} s del "
            f"reloj del consumidor; se usa el ancla UTC del consumidor.",
            file=sys.stderr,
        )
        return self.hora_arranque

    def _acumular(
        self,
        x: np.ndarray,
        y: np.ndarray,
        z: np.ndarray,
    ) -> None:
        origen = 0
        total_bloque = len(x)

        while origen < total_bloque:
            espacio = MUESTRAS_POR_VENTANA - self.n
            tomar = min(espacio, total_bloque - origen)

            self.buf_x[self.n : self.n + tomar] = x[origen : origen + tomar]
            self.buf_y[self.n : self.n + tomar] = y[origen : origen + tomar]
            self.buf_z[self.n : self.n + tomar] = z[origen : origen + tomar]

            self.n += tomar
            self.total_muestras += tomar
            origen += tomar

            if self.n == MUESTRAS_POR_VENTANA:
                self._cerrar_ventana("ventana completa de 10 minutos")

    def _cerrar_ventana(self, motivo: str) -> None:
        if self.n == 0 or self.inicio is None:
            return

        muestras_ventana = self.n

        if muestras_ventana == MUESTRAS_POR_VENTANA:
            # Entregamos los buffers completos al hilo escritor y creamos
            # buffers nuevos de inmediato para no frenar la recepción.
            x, y, z = self.buf_x, self.buf_y, self.buf_z
        else:
            x = self.buf_x[:muestras_ventana].copy()
            y = self.buf_y[:muestras_ventana].copy()
            z = self.buf_z[:muestras_ventana].copy()

        self.escritor.encolar(x, y, z, self.inicio, motivo)
        self.ventanas_guardadas += 1

        print(
            f"Ventana cerrada ({motivo}): {muestras_ventana} muestras/eje "
            f"({muestras_ventana / SPS:.2f} s). Encolada para escritura."
        )
        sys.stdout.flush()

        # La ventana siguiente empieza exactamente donde termina esta, según
        # el conteo de muestras a 100 SPS (no según el reloj de pared).
        self.inicio = self.inicio + (muestras_ventana / SPS)

        self.buf_x = np.empty(MUESTRAS_POR_VENTANA, dtype=np.int32)
        self.buf_y = np.empty(MUESTRAS_POR_VENTANA, dtype=np.int32)
        self.buf_z = np.empty(MUESTRAS_POR_VENTANA, dtype=np.int32)
        self.n = 0


def _float_env(nombre: str, por_defecto: float) -> float:
    try:
        return float(os.environ.get(nombre, por_defecto))
    except ValueError:
        return por_defecto


# ---------------------------------------------------------------------------
# Servicio
# ---------------------------------------------------------------------------
def main() -> int:
    esperar_reloj_ntp(_float_env("SISMO_NTP_WAIT", 15.0))

    # Ancla UTC del consumidor, tomada después de verificar NTP.
    hora_arranque = UTCDateTime(ns=time.time_ns())

    salida = Path(
        os.environ.get("SISMO_OUTPUT_DIR", str(SALIDA_POR_DEFECTO))
    ).expanduser()
    salida.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("Archivador MiniSEED - AR.TEST.00.HNE/HNN/HNZ")
    print(f"Endpoint:        {ZMQ_ENDPOINT}")
    print(f"Ancla UTC:       {hora_arranque.isoformat()} UTC")
    print(f"Ventana:         {DURACION_VENTANA_S:.0f} s "
          f"({MUESTRAS_POR_VENTANA} muestras/eje a {SPS:.0f} SPS)")
    print(f"Directorio:      {salida}")
    print("Ctrl+C guarda un archivo parcial con lo acumulado.")
    print("=" * 72)
    sys.stdout.flush()

    escritor = EscritorMiniSEED(salida)

    archivador = Archivador(escritor, hora_arranque)

    contexto = zmq.Context()
    socket = contexto.socket(zmq.SUB)
    socket.setsockopt(zmq.RCVHWM, 100_000)
    socket.setsockopt(zmq.SUBSCRIBE, b"")
    socket.setsockopt(zmq.RCVTIMEO, 500)  # permite chequear stop_event
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(ZMQ_ENDPOINT)

    detener = threading.Event()

    def manejar_senal(numero_senal, _frame) -> None:
        if detener.is_set():
            print(
                "Segunda señal recibida: salida forzada. "
                "Puede perderse el buffer pendiente.",
                file=sys.stderr,
            )
            os._exit(130)
        print(
            f"\nSeñal {numero_senal} recibida. "
            "Drenando socket y guardando datos pendientes...",
            file=sys.stderr,
        )
        detener.set()

    signal.signal(signal.SIGINT, manejar_senal)
    signal.signal(signal.SIGTERM, manejar_senal)

    # El hilo escritor arranca recién cuando todo lo anterior está listo, para
    # no dejar un hilo no-daemon colgado si falla la inicialización.
    escritor.start()

    codigo_salida = 0
    try:
        while not detener.is_set():
            try:
                payload = socket.recv()
            except zmq.Again:
                continue
            archivador.procesar(payload)

    except Exception:
        print("ERROR fatal en el receptor ZeroMQ:", file=sys.stderr)
        traceback.print_exc()
        codigo_salida = 1
    finally:
        # Drenamos lo que ya esté en la cola local de ZeroMQ antes de cerrar.
        # Ponemos un límite de tiempo para no quedar esperando indefinidamente
        # si el productor sigue publicando mientras apagamos el servicio.
        limite_drenaje = time.monotonic() + 2.0
        drenados = 0
        try:
            while time.monotonic() < limite_drenaje and drenados < 10_000:
                try:
                    payload = socket.recv(flags=zmq.NOBLOCK)
                except zmq.Again:
                    break
                archivador.procesar(payload)
                drenados += 1
        except Exception:
            print(
                "ADVERTENCIA: error durante el drenaje final del socket:",
                file=sys.stderr,
            )
            traceback.print_exc()

        archivador.cerrar()

        socket.close(linger=0)
        contexto.term()

        # Esperamos a que el hilo escritor termine todos los archivos.
        escritor.detener()

        print(
            f"Servicio finalizado. Muestras recibidas: "
            f"{archivador.total_muestras} por eje. "
            f"Ventanas encoladas/guardadas: {archivador.ventanas_guardadas}."
        )

    return codigo_salida


if __name__ == "__main__":
    raise SystemExit(main())
