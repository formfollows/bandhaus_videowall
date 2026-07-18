"""Reproduziert den gemeldeten "Live -> Datei -> Live"-Freeze end-to-end.

Treibt die echte GStreamer-Pipeline (VideoSwitcher) genau wie server.py über
einen REST-Endpunkt (/program), mit einer simulierten Live-Quelle (ein
lokaler ffmpeg-Loop, der die vorhandene HEVC-Testdatei per SRT an den
"iphone"-Listener streamt) als Ersatz für eine echte iPhone-Kamera.

Aufruf (Standard: fakesink, 1 Zyklus, headless-tauglich):
    .venv/bin/pytest -s test_switch_repro.py

Näher am echten Setup (echter Screen, mehrere Zyklen, um einen langsamen
Leak/Ressourcen-Effekt sichtbar zu machen):
    LICHT_TEST_SINK=kmssink LICHT_TEST_CYCLES=10 .venv/bin/pytest -s test_switch_repro.py

Gegen das echte iPhone statt der Simulation (die App braucht ein paar
Sekunden zum (Re-)Connect, der Test wartet dafür auf das tatsächliche
SRT-Handshake-Signal statt einer festen Zeit):
    LICHT_TEST_REAL_IPHONE=1 LICHT_TEST_SRT_PORT=6001 \
        LICHT_TEST_SINK=kmssink LICHT_TEST_CYCLES=10 .venv/bin/pytest -s test_switch_repro.py

Umgebungsvariablen:
    LICHT_TEST_SINK          Sink für die Test-Pipeline (Default: kmssink)
    LICHT_TEST_CYCLES        Anzahl live->Datei->live-Zyklen (Default: 1)
    LICHT_TEST_SRT_PORT      Port des 'iphone'-Listeners (Default: 16001, eigener
                              Port; auf 6001 setzen, um das echte iPhone ohne
                              Umkonfigurieren der App direkt verbinden zu lassen)
    LICHT_TEST_REAL_IPHONE   Wenn gesetzt: die simulierte ffmpeg-Quelle nicht
                              starten, stattdessen auf eine echte Verbindung warten
    LICHT_TEST_CONNECT_TIMEOUT_S
                              Timeout fürs anfängliche SRT-Handshake (Default: 15s)

Diagnose-Ausgaben (Buffer-Rate pro Sekunde, RSS-Speicher, Element-Anzahl der
Pipeline - jeweils pro Zyklus) landen sowohl in licht.log (gleiche
Rotating-File-Konfiguration wie server.py) als auch im pytest-Output (-s, um
sie live zu sehen).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import gi
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

gi.require_version("Gst", "1.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gst

from video_switcher import VideoSettings, VideoSwitcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("licht.log"),
    ],
    force=True,
)
logger = logging.getLogger("repro")

REPO_ROOT = Path(__file__).parent
VIDEO_FILE = REPO_ROOT / "videos" / "berg_h265.mp4"

SRT_PORT = int(os.environ.get("LICHT_TEST_SRT_PORT", "16001"))
TEST_SINK = os.environ.get("LICHT_TEST_SINK", "kmssink")
CYCLES = int(os.environ.get("LICHT_TEST_CYCLES", "1"))
USE_REAL_IPHONE = bool(os.environ.get("LICHT_TEST_REAL_IPHONE"))
CONNECT_TIMEOUT_S = float(os.environ.get("LICHT_TEST_CONNECT_TIMEOUT_S", "15"))

INITIAL_WAIT_S = 10  # wie gewünscht: 10s auf der Live-Quelle, dann umschalten
FILE_WAIT_S = 3  # wie gewünscht: 3s auf der Datei-Quelle, dann zurückschalten
POST_SWITCH_MONITOR_S = 10  # Beobachtungsfenster pro Zyklus nach Rückschaltung


class SwitchRequest(BaseModel):
    source: str


def build_app(switcher: VideoSwitcher) -> FastAPI:
    """Minimaler Nachbau von server.py's /program-Endpunkt, gegen eine
    testeigene VideoSwitcher-Instanz statt der Produktions-Singleton."""
    app = FastAPI()

    @app.put("/program")
    def select_program(request: SwitchRequest) -> dict:
        try:
            switcher.switch(request.source)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unbekannte Videoquelle")
        return {"status": "ok", "active_source": request.source}

    return app


class BufferMonitor:
    """Zählt Buffer, die den gemeinsamen Ausgang (Sink-Pad des Tee, direkt
    nach dem Selector) erreichen - unabhängig davon, welche Quelle gerade
    aktiv ist. So lässt sich "fließt gerade Bild" objektiv messen, auch mit
    fakesink (zeigt nichts an)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._timestamps: list[float] = []

    def _probe(self, _pad: Gst.Pad, _info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        with self._lock:
            self._timestamps.append(time.monotonic())
        return Gst.PadProbeReturn.OK

    def attach(self, pad: Gst.Pad) -> None:
        pad.add_probe(Gst.PadProbeType.BUFFER, self._probe)

    def count_in_window(self, start: float, end: float) -> int:
        with self._lock:
            return sum(1 for t in self._timestamps if start <= t < end)


def _vmrss_kb() -> int:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return -1


def _element_count(pipeline: Gst.Pipeline) -> int:
    it = pipeline.iterate_elements()
    n = 0

    while True:
        result, _ = it.next()

        if result != Gst.IteratorResult.OK:
            break

        n += 1

    return n


@pytest.fixture
def fake_iphone_source():
    """Streamt videos/berg_h265.mp4 per SRT (Caller) an den 'iphone'-Listener,
    um eine echte Live-Quelle zu simulieren, ohne echtes iPhone im Netz.

    Bei LICHT_TEST_REAL_IPHONE wird nichts gestartet - dann muss ein echtes
    Gerät (z. B. die iPhone-Streaming-App, Zielport siehe LICHT_TEST_SRT_PORT)
    selbst verbinden."""
    if USE_REAL_IPHONE:
        logger.info(
            "LICHT_TEST_REAL_IPHONE gesetzt: erwarte eine echte Verbindung auf Port %d",
            SRT_PORT,
        )
        yield None
        return

    if not VIDEO_FILE.exists():
        pytest.skip(f"Testdatei fehlt: {VIDEO_FILE}")

    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "warning",
        "-re", "-stream_loop", "-1", "-i", str(VIDEO_FILE),
        "-c", "copy", "-f", "mpegts", f"srt://127.0.0.1:{SRT_PORT}?mode=caller",
    ]
    proc = subprocess.Popen(cmd)

    try:
        yield proc
    finally:
        proc.terminate()

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def switcher():
    settings = VideoSettings(
        sources={
            "iphone": f"srt://0.0.0.0:{SRT_PORT}?mode=listener&latency=120",
            "berg": str(VIDEO_FILE),
        },
        sink=TEST_SINK,
    )
    sw = VideoSwitcher(settings)

    glib_loop = GLib.MainLoop()
    loop_thread = threading.Thread(target=glib_loop.run, daemon=True)
    loop_thread.start()

    sw.start()

    yield sw

    sw.stop()
    glib_loop.quit()
    loop_thread.join(timeout=5)


def test_live_survives_file_roundtrip(switcher: VideoSwitcher, fake_iphone_source) -> None:
    logger.info("Test-Konfiguration: sink=%s cycles=%d", TEST_SINK, CYCLES)

    monitor = BufferMonitor()
    monitor.attach(switcher.tee.get_static_pad("sink"))

    client = TestClient(build_app(switcher))

    # Feste Sleeps sind hier keine gute Idee: die simulierte Quelle verbindet
    # in < 1s, die echte iPhone-App laut Beobachtung eher ~5s (Reconnect-Zeit
    # der App). Stattdessen auf das tatsächliche SRT-Handshake-Signal warten,
    # mit grosszügigem Timeout als Obergrenze.
    connected = threading.Event()
    iphone_source = switcher._srt_sources["iphone"]
    iphone_source.connect("caller-added", lambda *_args: connected.set())

    logger.info("Warte auf SRT-Verbindung von 'iphone' (Timeout %.0fs) ...", CONNECT_TIMEOUT_S)
    connect_wait_start = time.monotonic()
    got_connection = connected.wait(timeout=CONNECT_TIMEOUT_S)
    connect_duration = time.monotonic() - connect_wait_start

    assert got_connection, (
        f"Keine SRT-Verbindung von 'iphone' innerhalb {CONNECT_TIMEOUT_S:.0f}s. "
        + ("Läuft die simulierte Quelle (ffmpeg)?" if not USE_REAL_IPHONE
           else f"iPhone-App auf srt://<host>:{SRT_PORT} verbunden?")
    )
    logger.info("SRT-Verbindung nach %.1fs zustande gekommen", connect_duration)

    logger.info("Baseline: 'iphone' laeuft %ds", INITIAL_WAIT_S)
    baseline_start = time.monotonic()
    time.sleep(INITIAL_WAIT_S)
    baseline_end = time.monotonic()
    baseline_count = monitor.count_in_window(baseline_start, baseline_end)
    baseline_rate = baseline_count / (baseline_end - baseline_start)
    logger.info(
        "Baseline: %d Buffer in %.1fs (%.1f Buffer/s), RSS=%dkB, Elemente=%d",
        baseline_count, baseline_end - baseline_start, baseline_rate,
        _vmrss_kb(), _element_count(switcher.pipeline),
    )

    assert baseline_count > 0, (
        "Simulierte iPhone-Quelle liefert keine Frames - SRT-Verbindung "
        "nicht zustande gekommen? (ffmpeg-Log prüfen)"
    )

    expected_minimum_rate = baseline_rate * 0.3  # grosszügige Schwelle, siehe unten
    cycle_results: list[dict] = []

    for cycle in range(1, CYCLES + 1):
        logger.info("=== Zyklus %d/%d ===", cycle, CYCLES)

        logger.info("Wechsel zu 'berg' (Datei-Quelle) via REST /program")
        resp = client.put("/program", json={"source": "berg"})
        assert resp.status_code == 200, resp.text

        time.sleep(FILE_WAIT_S)

        logger.info("Wechsel zurueck zu 'iphone' via REST /program")
        switch_back_at = time.monotonic()
        resp = client.put("/program", json={"source": "iphone"})
        assert resp.status_code == 200, resp.text

        for second in range(POST_SWITCH_MONITOR_S):
            time.sleep(1)
            n = monitor.count_in_window(switch_back_at + second, switch_back_at + second + 1)
            logger.info("  Zyklus %d, t+%2ds nach Rueckschaltung: %d Buffer", cycle, second + 1, n)

        # 1s Anlaufzeit für Decoder-Neuaufbau/Renegotiation tolerieren.
        window_s = POST_SWITCH_MONITOR_S - 1
        post_switch_count = monitor.count_in_window(switch_back_at + 1, switch_back_at + POST_SWITCH_MONITOR_S)
        post_switch_rate = post_switch_count / window_s
        rss = _vmrss_kb()
        elements = _element_count(switcher.pipeline)

        logger.info(
            "Zyklus %d Ergebnis: %.1f Buffer/s nach Rueckschaltung (Baseline %.1f/s), "
            "RSS=%dkB, Elemente=%d",
            cycle, post_switch_rate, baseline_rate, rss, elements,
        )

        cycle_results.append({
            "cycle": cycle,
            "rate": post_switch_rate,
            "rss_kb": rss,
            "elements": elements,
        })

    logger.info("=== Zusammenfassung ueber %d Zyklen ===", CYCLES)
    logger.info("Baseline-Rate: %.1f Buffer/s (Schwelle: %.1f Buffer/s)", baseline_rate, expected_minimum_rate)

    for r in cycle_results:
        logger.info(
            "  Zyklus %d: %.1f Buffer/s, RSS=%dkB, Elemente=%d",
            r["cycle"], r["rate"], r["rss_kb"], r["elements"],
        )

    rss_growth = cycle_results[-1]["rss_kb"] - cycle_results[0]["rss_kb"]
    element_growth = cycle_results[-1]["elements"] - cycle_results[0]["elements"]
    logger.info("RSS-Wachstum ueber alle Zyklen: %+dkB, Element-Wachstum: %+d", rss_growth, element_growth)

    if element_growth > 0:
        logger.warning(
            "Pipeline-Element-Anzahl waechst ueber wiederholtes Umschalten (%+d) - Hinweis auf Leak.",
            element_growth,
        )

    if rss_growth > 20_000:  # >20MB Wachstum ueber den gesamten Testlauf
        logger.warning("RSS waechst deutlich ueber wiederholtes Umschalten (%+dkB) - Hinweis auf Leak.", rss_growth)

    failing_cycles = [r for r in cycle_results if r["rate"] < expected_minimum_rate]

    assert not failing_cycles, (
        f"Live-Quelle friert in {len(failing_cycles)}/{CYCLES} Zyklen nach Rueckschaltung "
        f"ein (Rate < {expected_minimum_rate:.1f} Buffer/s): "
        f"{[(r['cycle'], round(r['rate'], 1)) for r in failing_cycles]}"
    )
