# Changelog

## 2026-07-18 (uncommitted)

Alles unten ist noch nicht committed (Stand dieser Session).

### Bugfix-Versuche: Live → Datei → Live Freeze

Zentrales Thema der Session war ein Bug, bei dem nach dem Ablauf Live-Quelle →
Datei-Quelle → Live-Quelle der zurückgeschaltete Live-Stream einfriert.
**Der Bug ist am Ende der Session noch nicht behoben** — siehe
[FREEZE_INVESTIGATION.md](FREEZE_INVESTIGATION.md) für das vollständige
Sitzungsprotokoll mit Ergebnis pro Versuch.

Änderungen in `video_switcher.py` im Rahmen der Untersuchung:
- `input-selector`: `sync-streams` auf `False` gesetzt (Verdacht: die
  Datei-Loop-Seeks verwirren die Segment-/Running-Time-Buchhaltung des
  Selectors und blockieren danach reaktivierte Live-Pads dauerhaft).
- Eigene `queue` direkt vor jedem Selector-Sink-Pad ergänzt (SRT- und
  Datei-Zweig), um den Branch-Thread vom Pad-Switch/Flush-Handling des
  Selectors zu entkoppeln.
- EOS-Loop-Handling überarbeitet: `FLUSH_START`/`FLUSH_STOP`-Events werden an
  der Selector-Grenze abgefangen (`DROP`), damit ein Flush durch den
  Datei-Loop-Seek nicht den gemeinsamen Ausgang (und damit einen gerade
  aktiven Live-Zweig) mitreißt; Probe lauscht jetzt zusätzlich auf
  `EVENT_FLUSH`, nicht nur `EVENT_DOWNSTREAM`.
- Logging um Seek-Position und `seek_simple()`-Ergebnis ergänzt.
- `pad-removed`-Handler für `decodebin` ergänzt, der pro Auswahlzyklus
  angelegte `fakesink`-Audio-Drains wieder abbaut (verhinderte vorher eine
  Pipeline-Leiche pro Umschaltzyklus).
- Zustandswechsel auf `Gst.State.NULL` beim Deaktivieren einer Datei-Quelle
  wird jetzt über `get_state()` synchron abgewartet (`_set_state_checked`),
  da der v4l2-Hardware-Decoder sein Gerät erst nach vollständigem Teardown
  freigibt und sich Datei- und Live-Quellen denselben begrenzten
  HEVC-Decode-Session-Pool teilen.

### Test-Infrastruktur

- Neu: [test_switch_repro.py](test_switch_repro.py) — Pytest-Reproduktionstest,
  der die echte `VideoSwitcher`-Pipeline über den echten `/program`-Endpunkt
  treibt (FastAPI `TestClient`), inkl. `BufferMonitor` zum Zählen von Buffern
  am gemeinsamen `tee`-Ausgang. Simuliert `iphone` per `ffmpeg`, kann aber
  auch gegen eine echte iPhone-Quelle laufen (`LICHT_TEST_REAL_IPHONE=1`).
  Bug reproduziert bisher nur zuverlässig mit echtem Gerät, nicht mit der
  simulierten Quelle.
- `makefile` überarbeitet: von drei losen Shell-Zeilen auf benannte Targets
  (`setup`, `venv`, `run`, `test`, `test-real-iphone`) mit `.PHONY` und
  Kurzbeschreibung der Aufrufe; `venv`-Target installiert jetzt auch
  `pytest`/`httpx`.

### Logging

- `server.py`: Logging läuft jetzt zusätzlich zum `StreamHandler` über einen
  `RotatingFileHandler` (`licht.log`, 10 MB × 5 Dateien), damit längere
  Testläufe nicht unbegrenzt wachsende Logs erzeugen.
- `.gitignore`: `*.log` ergänzt.
- `server.py`: `ipad`-Quelle auskommentiert (nur noch `iphone` + `berg` aktiv,
  im Rahmen der Bug-Eingrenzung).

### Dokumentation

- Neu: [ROADMAP.md](ROADMAP.md) — geplante Features (BPM-gesteuerte
  Automatik, Song-Szenen, Break-Scene-Editor, globale Video-Bibliothek,
  KI-Content-Pipeline).
- [README.md](README.md): Abschnitt "Roadmap" mit Link auf `ROADMAP.md`
  ergänzt.
- [TODO.md](TODO.md): Freeze-Bug ergänzt, Frontend-Ideen (Streamer-Liste,
  Szenen-Start mit BPM-Tap-in, Dummy-Szene), Content-Ideen (Logo-/Band-/
  Sponsor-Animationen) und Moblin-Remote-Start/Reconnect-Punkte ergänzt.
- Neu: [FREEZE_INVESTIGATION.md](FREEZE_INVESTIGATION.md) — Sitzungsprotokoll
  zum Freeze-Bug für Anschlussfähigkeit in einer neuen Session.
