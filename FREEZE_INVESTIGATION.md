# Freeze-Bug: Live → Datei → Live — Sitzungsprotokoll

Stand: 2026-07-18, Ende der Sitzung. **Bug ist noch nicht behoben.** Dieses Dokument
fasst zusammen, was in dieser Session untersucht, geändert und getestet wurde, mit
Ergebnis, damit eine neue Session direkt anknüpfen kann, ohne von vorne zu suchen.

## Fehlerbild (Ausgangspunkt)

- Zwei Live-Streams (SRT, `iphone`/`ipad`) laufen flüssig, Umschalten zwischen ihnen
  funktioniert zuverlässig.
- Lokales Video (`berg`, Datei-Quelle) spielt ab.
- Nach dem Ablauf **Live → Datei → Live** friert der zurückgeschaltete Live-Stream
  teilweise/komplett ein.
- `ipad` wurde vom Nutzer inzwischen in `server.py` auskommentiert (nur noch `iphone`
  + `berg` aktiv), Bug bleibt bestehen.

## Aufgebaute Test-Infrastruktur (funktioniert, bleibt nützlich)

### `test_switch_repro.py` (Repo-Root)

Pytest-Reproduktionstest, treibt die echte `VideoSwitcher`-Pipeline über den echten
`/program`-REST-Endpunkt (FastAPI `TestClient`, ASGI-intern, kein echter Socket).

- Simuliert `iphone` per `ffmpeg` (streamt `videos/berg_h265.mp4` per SRT), damit kein
  echtes Handy nötig ist — **aber**: der Bug reproduziert sich mit dieser simulierten
  Quelle bislang **nicht zuverlässig / gar nicht** in Mehrfach-Zyklen-Tests. Nur mit dem
  echten iPhone reproduziert er sich zuverlässig.
- `BufferMonitor`-Klasse zählt Buffer am `tee`-Sink-Pad (gemeinsamer Ausgang) — das ist
  die zentrale Messgröße für "läuft noch Bild" unabhängig vom Sink.
- Env-Var-Konfiguration:
  - `LICHT_TEST_SINK` (Default `kmssink`) — `fakesink` für headless/lokale Checks
  - `LICHT_TEST_CYCLES` (Default `1`) — Anzahl Live→Datei→Live-Zyklen
  - `LICHT_TEST_SRT_PORT` (Default `16001`) — auf `6001` setzen für echtes iPhone
  - `LICHT_TEST_REAL_IPHONE` — wenn gesetzt, startet KEIN ffmpeg, wartet auf echte
    Verbindung
  - `LICHT_TEST_CONNECT_TIMEOUT_S` (Default `15`) — Timeout fürs SRT-Handshake

**Befehl fürs echte Gerät (das, was den Bug zuverlässig zeigt):**

```bash
LICHT_TEST_REAL_IPHONE=1 LICHT_TEST_SRT_PORT=6001 LICHT_TEST_SINK=kmssink \
LICHT_TEST_CYCLES=10 .venv/bin/pytest -s test_switch_repro.py
```

Wichtig: dabei darf `server.py` nicht gleichzeitig laufen (Port 6001/8000-Konflikt).

### `makefile`

War vorher kein gültiges Make-Syntax (`make -f makefile` schlug mit "missing
separator" fehl). Jetzt saubere Targets: `setup` (Systemabhängigkeiten, Default),
`venv` (venv + Python-Pakete inkl. `pytest`/`httpx`), `run` (`server.py`), `test`
(simulierte Quelle), `test-real-iphone` (echtes iPhone, 10 Zyklen, `kmssink`).

### Logging

`server.py` schreibt jetzt zusätzlich zur Konsole nach `licht.log`
(`RotatingFileHandler`, 10MB×5). `test_switch_repro.py` nutzt dieselbe Datei. Das ist
die erste Anlaufstelle nach jedem Testlauf.

## Diagnose-Historie

### 1. Erste statische Analyse (vor jedem Test)

Fünf Hypothesen in Prioritätsreihenfolge identifiziert (Details siehe Git-Historie /
Konversation, hier nur Kurzfassung):

1. Wiederholtes Hardware-Decoder-Churn (`v4l2slh265dec`) beim Umschalten auf/von der
   Datei-Quelle — **später widerlegt** (siehe GST_DEBUG-Befund unten: Decoder läuft
   während des gesamten Freezes ungestört weiter).
2. Geleakte `fakesink`-"Drain"-Elemente bei jeder Datei-Neuauswahl (nur relevant bei
   Audio-Spur in der Datei) — **bestätigt und gefixt** (siehe unten).
3. Fehlendes `pad-removed`-Handling im Datei-Branch (Asymmetrie zum SRT-Branch) —
   im Zuge von Punkt 2 mitgefixt.
4. Keine Queue direkt vor den Selector-Sink-Pads — **gefixt** (siehe unten), war aber
   nicht die Hauptursache.
5. Pipeline-weite Flushes durch den Loop-Seek der Datei-Quelle — **das ist die
   tatsächliche Hauptursache**, siehe GST_DEBUG-Befund weiter unten.

### 2. Angewendete Fixes, die stehen bleiben sollten (keine Regression, sinnvoll)

Alle in [`video_switcher.py`](video_switcher.py):

- **Queue vor jedem Selector-Pad** (`selector_queue`, `leaky=downstream`,
  `max-size-buffers=5`) — sowohl im SRT- als auch im Datei-Branch, direkt vor
  `request_pad_simple("sink_%u")`. GStreamer-Best-Practice für `input-selector`.
- **Drain-Leak-Fix**: `on_pad_removed`-Handler im Datei-Branch, trackt und entfernt
  `fakesink`-Drains für Nicht-Video-Pads (Audio) pro Zyklus. Verifiziert mit
  `videos/berg.MP4` (hat Audio-Spur): Elementanzahl blieb über 12 Zyklen konstant
  statt zu wachsen.
- **`_set_state_checked()`-Helper**: prüft `set_state()`-Rückgabewerte, wartet bei
  `NULL`-Übergängen per `get_state(5*Gst.SECOND)` auf tatsächlichen Abschluss statt
  dem Rückgabewert blind zu vertrauen. Sollte helfen, falls der Decoder-Teardown
  asynchron nicht wirklich fertig ist, bevor der nächste Zyklus startet.

Diese drei Fixes lösen den Haupt-Freeze **nicht** (weiterhin reproduzierbar danach),
sind aber unabhängig sinnvoll und sollten bleiben.

### 3. `sync-streams=False` auf dem Selector — getestet, keine Wirkung

`self.selector.set_property("sync-streams", False)` in `__init__` gesetzt (Zeile ~44).
Hypothese: `input-selector`s Default-Verhalten (inaktive Pads auf Running-Time
synchronisieren) verursacht den Block. **Test mit echtem iPhone: Freeze exakt
unverändert.** Property ist noch gesetzt (aktueller Code-Stand), könnte man
zurücksetzen, macht aber vermutlich keinen Unterschied in beide Richtungen.

### 4. GST_DEBUG-Tiefenanalyse — DER Befund

Mit `GST_DEBUG=v4l2codecs*:6,GST_STATES:4` gegen echtes iPhone:

- **Der Hardware-Decoder (`decoder-iphone`) läuft während des gesamten Freezes
  komplett normal weiter** (durchgehend `Output picture`-Log-Zeilen, keine Lücke,
  über die komplette Freeze-Dauer). Decoder-Ressourcenknappheit ist damit als Ursache
  ausgeschlossen.

Mit `GST_DEBUG=input-selector:6,queue:7,videorate:5,videoconvert:5,GST_EVENT:5,
GST_SEGMENT:5,GST_STATES:4` gegen echtes iPhone:

- `selector:sink_0` (iphones Pad) wird nach der Rückschaltung noch **~5-7 mal**
  erfolgreich bedient (`Forwarded ... result=0`), dann **nie wieder** — passt exakt
  zur Kapazität der neuen `selector-queue-iphone` (`max-size-buffers=5`).
- **Gleichzeitig verstummen `queue-iphone` UND `selector-queue-iphone`** (beide,
  gleichzeitig) komplett — keine Chain-Aufrufe, keine QOS-Events, nichts, bis zum
  Pipeline-Abbau am Testende.
- **Der eigentliche Mechanismus** (mit Zeitstempel-Korrelation nachgewiesen): Der
  Loop-Seek von `berg` (`decodebin.seek_simple(FLUSH|KEY_UNIT, 0)` in
  `on_eos_probe`/`do_seek`) sendet `FLUSH_START`, das — während `berg` gerade der
  **aktive** Selector-Pad ist — durch `selector:sink_1` bis zum **gemeinsamen
  Ausgang** durchläuft: `output-convert`, `output-tee`, `display-queue`,
  `video-output` (kmssink) werden alle geflusht, `FLUSH_STOP` räumt kurz danach
  wieder auf. Das ist für sich genommen normales `input-selector`-Verhalten (Flush
  des aktiven Pads soll durchschlagen).
  - **Aber**: `iphone`s eigener Zweig (`queue-iphone`, `convert-iphone`, ...) wird
    dabei **nicht** mitgeflusht (nur der gerade aktive Pad wird geflusht). Schaltet
    die Pipeline direkt danach auf `iphone` um, steht dessen Zweig gegenüber dem
    frisch resetteten gemeinsamen Ausgang inkonsistent da — vermutete Folge: Buffer
    werden ab dann lautlos verworfen.
  - Beobachtung, die diese Theorie stützt: in dem Lauf, in dem das am klarsten zu
    sehen war, fielen Loop-Seek und Rückschaltung praktisch zeitgleich zusammen
    (Log-Zeitstempel innerhalb 1ms).

### 5. Fix-Versuch #1: FLUSH aus dem Loop-Seek entfernen — falscher Ansatz

Änderung: `Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT` → nur `Gst.SeekFlags.KEY_UNIT`.

**Test mit echtem iPhone**: iphone-Freeze war weg (kurzes Stocken, dann wieder
flüssig) — **aber** `berg` blieb ab dem 2./3. Zyklus dauerhaft "zu Ende" (spielte nie
wieder). Ohne FLUSH setzt decodebin seinen internen EOS-Zustand offenbar nicht
zuverlässig zurück. Verworfen.

### 6. Fix-Versuch #2: FLUSH behalten, an Pad-Probe abfangen — Bug in der Implementierung

Idee: FLUSH im Seek behalten (intern in `berg`s Zweig nötig), aber `FLUSH_START`/
`FLUSH_STOP` an der bestehenden `on_eos_probe`-Pad-Probe (auf `source_pad`, direkt vor
dem Selector) abfangen und verwerfen, bevor sie den Selector erreichen.

**Bug**: Probe wurde nur mit `Gst.PadProbeType.EVENT_DOWNSTREAM` registriert. Laut
GStreamer-Doku schließt `EVENT_DOWNSTREAM` Flush-Events explizit aus ("events that are
not flushing") — dafür gibt es die separate Maske `EVENT_FLUSH`. Der Abfang war damit
**totes Coder**, griff nie. Test mit echtem iPhone entsprechend: **exakt gleiches
Verhalten wie vorher** (Freeze weiterhin da), und zusätzlich die "spielt nie von
null"-Beobachtung des Nutzers (vermutlich derselbe unkontrollierte Flush-Mechanismus,
nur weiter unbehandelt).

### 7. Fix-Versuch #3: Probe-Maske korrigiert — aktueller Stand, reicht nicht

Korrektur: `Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.EVENT_FLUSH`.

**Isolierter Test** (simulierte iPhone-Quelle, `fakesink`, ein sauberer Loop-Zyklus
über die volle Dateidauer von 22s + Rückschaltung): funktioniert einwandfrei — per
`GST_DEBUG` bestätigt, dass `selector:sink_1` das Flush-Start-Event diesmal **nie**
sieht (0 Treffer), iphone bleibt danach 270/270 möglichen Buffern lang flüssig.

**Test mit echtem iPhone (10 Zyklen)**: Nutzer berichtet "selber Fehler,
unverändert". Das zuletzt eingesehene Log-Fragment (`licht.log`, Lauf ab
`12:58:55,999`) zeigt allerdings ein gemischtes Bild, das **noch nicht mit dem Nutzer
besprochen/analysiert wurde**:

```
Zyklus 1 Ergebnis: 29.9 Buffer/s (kein Freeze, kein Loop in diesem Zyklus)
Zyklus 2: Loop nach nur 0.69s (!) - "aktuelle Position: 22.000s" beim Seek,
          d.h. berg startete NICHT bei 0, sondern nahe dem Dateiende (~21.3s)
          Flush korrekt abgefangen (beide Log-Zeilen "abgefangen" + seek_simple=True)
          Zyklus 2 Ergebnis: 30.2 Buffer/s (KEIN Freeze!)
Zyklus 3: Loop wieder nach ~0.7s, gleiches Muster, Flush wieder korrekt abgefangen
          -> KEIN "Wechsel zurueck zu iphone" mehr geloggt, danach direkt
             "Source iphone disconnected"
```

Zyklus 1 und 2 zeigen laut Buffer-Monitor **keinen** Freeze mehr. Zyklus 3 bricht ab,
bevor die Rückschaltung überhaupt geloggt wurde — unklar ob das derselbe Freeze in
neuer Form ist, ein Hänger an anderer Stelle, oder das reale iPhone/die App die
Verbindung von sich aus beendet hat (z. B. weil der Nutzer den sichtbaren Freeze
gesehen und die App/den Stream angehalten hat). **Das ist der nächste
Untersuchungspunkt.**

## Bestätigter Nebenbefund (separates Problem, nicht der Freeze selbst)

`berg` startet beim Neu-Auswählen **nicht zuverlässig bei Position 0** — mehrfach
beobachtet, dass der Loop schon nach 0.6–0.7s (statt nach voller Spieldauer von 22s)
erneut auslöst, mit `query_position()` kurz vor dem Seek bei ~22.0s (= Dateiende).
Das bedeutet: die Datei-Quelle startete nahe ihrem eigenen Ende statt bei 0. Laut
Code-Kommentar/README sollte das durch den vollständigen `NULL`→`PLAYING`-Rebuild von
`decodebin` bei jeder Auswahl ausgeschlossen sein ("kein expliziter seek_simple
nötig"). Ist es aber offenbar nicht zuverlässig. **Noch nicht isoliert untersucht,
ob das eine Folge des ungelösten Flush-Themas ist oder ein eigenständiger Bug.**

## Nebenbefund: Speicher/Swap (gelöst, unabhängig vom Freeze)

`free -h` zeigte 810MB Swap belegt und nur ~380MB "available" RAM, **bevor**
`server.py`/`pytest` überhaupt liefen. Ursache: VSCode-Pylance-Sprachserver belegte
791MB RSS auf diesem 2GB-Pi (gleichzeitig zur Video-Pipeline laufend, weil der Nutzer
per VSCode-Remote-SSH direkt auf diesem Gerät entwickelt). Nutzer hat Pylance
deinstalliert → Swap auf 78MB runter, 1GB statt 380MB verfügbar. **Behebt den
Freeze nicht** (der ist reine Logik, kein Speicherproblem), ist aber eine echte
Verbesserung für die allgemeine Systemstabilität und weniger Rauschen bei künftigen
Tests.

Zeitzonen-Unterschied Pi/iPhone als Ursache wurde angefragt und ausgeschlossen:
GStreamer/SRT arbeiten mit relativer Running-Time/monotoner Uhr, nicht mit
Wanduhrzeit — Zeitzone kann hier strukturell keine Rolle spielen.

## Nächste Schritte für eine neue Session

1. **Zuerst**: das Log-Fragment aus Abschnitt 7 oben (`licht.log`, ab
   `12:58:55,999`) gemeinsam mit dem Nutzer durchgehen — insbesondere warum Zyklus 3
   ohne "Wechsel zurueck"-Log und ohne Zyklus-Ergebnis abbricht. Ggf. `licht.log`
   direkt einsehen (liegt im Repo-Root, wächst mit jedem Lauf weiter).
2. Die "startet nicht bei Position 0"-Beobachtung gezielt isolieren: eigener Test,
   der nur wiederholt zwischen zwei Datei-Quellen (oder Datei ↔ Datei) umschaltet,
   ohne Live-Quelle, und nach jeder Auswahl `query_position()` direkt nach dem
   `pad-added`/"spielt"-Log prüft. Klärt, ob das ein eigenständiger Bug in
   `_apply_switch`/`decodebin`-Rebuild ist oder eine Folge des Flush-Themas.
3. Prüfen, ob `_apply_switch`s eigener Teardown (`previous_element.set_state(NULL)`
   beim Wegschalten von `berg`) **selbst** auch Flush-artige Events erzeugt, die
   denselben Mechanismus auslösen können — bisher wurde nur der Loop-Seek als
   Flush-Quelle untersucht, nicht der reguläre Wegschalt-Teardown.
4. Falls der Freeze doch noch auftritt: mit der jetzt vorhandenen Diagnose-Logging
   (`"Seek auf 0 wird ausgefuehrt"`, `"an Selector-Grenze abgefangen"`,
   `"seek_simple() ergab"`) plus einem frischen `GST_DEBUG=input-selector:6,
   queue:7,GST_EVENT:5,GST_SEGMENT:5,GST_STATES:4`-Mitschnitt exakt den Moment
   nach dem letzten erfolgreichen Zyklus (3+) einfangen.
5. Erwägen, ob der Loop-Seek grundsätzlich anders gelöst werden sollte (z. B.
   Flush nur lokal im Datei-Zweig durch gezieltes Pad-Blocking statt durch
   Event-Interception an einer einzelnen Probe-Stelle).

## Betroffene Dateien (aktueller Stand, alle ungetestet-committed)

- [`video_switcher.py`](video_switcher.py) — alle oben beschriebenen Fixes
- [`server.py`](server.py) — File-Logging (`RotatingFileHandler` → `licht.log`)
- [`test_switch_repro.py`](test_switch_repro.py) — Reproduktions-Testharness
- [`makefile`](makefile) — repariert, neue Targets
- `licht.log` — läuft mit, nicht committed (`.gitignore`)
