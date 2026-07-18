from __future__ import annotations

import logging
import os

import gi
from pydantic import BaseModel

gi.require_version("Gst", "1.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib, Gst


logger = logging.getLogger(__name__)

Gst.init(None)

class VideoSettings(BaseModel):
    sources: dict[str, str] = {}

    sink: str = "kmssink"
    width: int = 1280
    height: int = 720
    framerate: int = 30

    screenshot_path: str = "screenshot.png"
    screenshot_interval: int | None = None

    @property
    def caps(self) -> Gst.Caps:
        return Gst.Caps.from_string(
            f"video/x-raw,format=I420,width={self.width},height={self.height},"
            f"framerate={self.framerate}/1"
        )


class VideoSwitcher:
    def __init__(self, settings: VideoSettings | None = None) -> None:
        self.settings = settings or VideoSettings()

        self.pipeline = Gst.Pipeline.new("video-switcher")
        self.selector = Gst.ElementFactory.make("input-selector", "selector")
        # Default (true) synchronisiert inaktive Pads auf die Running-Time
        # des aktiven Streams. Die Datei-Quelle loopt per Flushing-Seek
        # (siehe _add_file_source/on_eos_probe) und verwirrt dabei
        # offenbar die interne Segment-/Running-Time-Buchhaltung des
        # Selectors so, dass ein danach reaktivierter Live-Pad dauerhaft
        # blockiert, statt Buffer durchzulassen (Symptom: Decoder liefert
        # weiter Bilder, aber am Selector-Ausgang kommt nichts mehr an).
        self.selector.set_property("sync-streams", False)
        self.convert = Gst.ElementFactory.make("videoconvert", "output-convert")
        self.tee = Gst.ElementFactory.make("tee", "output-tee")
        self.display_queue = Gst.ElementFactory.make("queue", "display-queue")
        self.sink = Gst.ElementFactory.make(self.settings.sink, "video-output")

        self.screenshot_queue = Gst.ElementFactory.make("queue", "screenshot-queue")
        self.screenshot_convert = Gst.ElementFactory.make("videoconvert", "screenshot-convert")
        self.screenshot_capsfilter = Gst.ElementFactory.make("capsfilter", "screenshot-caps")
        self.screenshot_sink = Gst.ElementFactory.make("appsink", "screenshot-sink")

        elements = (
            self.pipeline,
            self.selector,
            self.convert,
            self.tee,
            self.display_queue,
            self.sink,
            self.screenshot_queue,
            self.screenshot_convert,
            self.screenshot_capsfilter,
            self.screenshot_sink,
        )

        if not all(elements):
            raise RuntimeError("Benötigte GStreamer-Elemente fehlen")

        # kmssink defaults to sync=true, which waits for buffers to reach
        # their pipeline-clock timestamp before displaying them. With a live
        # SRT source that clock can drift/jitter enough that frames are
        # never released, so nothing appears on HDMI even though the
        # pipeline otherwise runs fine.
        if self.sink.find_property("sync") is not None:
            self.sink.set_property("sync", False)

        self.screenshot_capsfilter.set_property(
            "caps", Gst.Caps.from_string("video/x-raw,format=RGB")
        )
        self.screenshot_sink.set_property("emit-signals", False)
        self.screenshot_sink.set_property("sync", False)
        self.screenshot_sink.set_property("max-buffers", 1)
        self.screenshot_sink.set_property("drop", True)

        # Default queues are leaky=no (blocking, never drops): if the
        # pipeline ever briefly falls behind live (network hiccup, decoder
        # stall), the backlog is never shed and playback keeps lagging by
        # that amount indefinitely. leaky=downstream drops the oldest
        # buffered frame instead, so the queue catches back up to live.
        self._make_low_latency(self.display_queue)

        for element in elements[1:]:
            self.pipeline.add(element)

        if not self.selector.link(self.convert):
            raise RuntimeError("Selector konnte nicht verbunden werden")

        if not self.convert.link(self.tee):
            raise RuntimeError("Tee konnte nicht verbunden werden")

        if not self.tee.link(self.display_queue) or not self.display_queue.link(self.sink):
            raise RuntimeError("Videoausgabe konnte nicht verbunden werden")

        if (
            not self.tee.link(self.screenshot_queue)
            or not self.screenshot_queue.link(self.screenshot_convert)
            or not self.screenshot_convert.link(self.screenshot_capsfilter)
            or not self.screenshot_capsfilter.link(self.screenshot_sink)
        ):
            raise RuntimeError("Screenshot-Zweig konnte nicht verbunden werden")

        self.selector_pads: dict[str, Gst.Pad] = {}
        self._screenshot_timer_id: int | None = None
        self._srt_sources: dict[str, Gst.Element] = {}
        self._file_sources: dict[str, Gst.Element] = {}
        self._active_source: str | None = None
        self._stats_timer_id: int | None = None

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::warning", self._on_bus_warning)
        bus.connect("message::latency", self._on_bus_latency)

        for name, url in self.settings.sources.items():
            self._add_source(name, url)

        # input-selector wählt ohne explizite Auswahl automatisch den zuerst
        # angeforderten Pad als aktiv. Datei-Quellen sollen aber nur laufen,
        # während sie ausgewählt sind (siehe _add_file_source) - falls eine
        # davon dieser initiale Pad ist, muss sie entsprechend freigegeben
        # und gestartet werden, statt pausiert zu bleiben.
        active_pad = self.selector.get_property("active-pad")

        for name, pad in self.selector_pads.items():
            if pad == active_pad:
                self._active_source = name
                element = self._file_sources.get(name)

                if element is not None:
                    element.set_locked_state(False)
                    element.set_state(Gst.State.PLAYING)

                break

    @staticmethod
    def _make_low_latency(queue: Gst.Element) -> None:
        queue.set_property("leaky", 2)  # GST_QUEUE_LEAK_DOWNSTREAM
        queue.set_property("max-size-buffers", 5)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("max-size-time", 0)

    @staticmethod
    def _on_bus_error(_bus: Gst.Bus, message: Gst.Message) -> None:
        err, debug = message.parse_error()
        logger.error("GStreamer-Fehler von %s: %s (%s)", message.src.get_name(), err.message, debug)

    @staticmethod
    def _on_bus_warning(_bus: Gst.Bus, message: Gst.Message) -> None:
        warn, debug = message.parse_warning()
        logger.warning("GStreamer-Warnung von %s: %s (%s)", message.src.get_name(), warn.message, debug)

    def _on_bus_latency(self, _bus: Gst.Bus, _message: Gst.Message) -> None:
        query = Gst.Query.new_latency()

        if not self.pipeline.query(query):
            return

        live, min_latency, max_latency = query.parse_latency()
        logger.info(
            "Pipeline-Latenz neu berechnet: live=%s min=%.1fms max=%s",
            live,
            min_latency / Gst.MSECOND,
            f"{max_latency / Gst.MSECOND:.1f}ms" if max_latency != Gst.CLOCK_TIME_NONE else "unbegrenzt",
        )

    def _add_source(self, name: str, url: str) -> None:
        if url.startswith("srt://"):
            self._add_srt_source(name, url)
        else:
            self._add_file_source(name, url)

    def _add_srt_source(self, name: str, url: str) -> None:
        # Feste Pipeline statt uridecodebin: uridecodebin würde autoplugin
        # und dabei ggf. einen Software-H265-Decoder wählen. Erzwingt
        # stattdessen genau die Kette, die sich im Test als funktionierend
        # erwiesen hat (srtsrc -> tsdemux -> h265parse -> v4l2slh265dec).
        source = Gst.ElementFactory.make("srtsrc", f"source-{name}")
        demux = Gst.ElementFactory.make("tsdemux", f"demux-{name}")
        parse = Gst.ElementFactory.make("h265parse", f"parse-{name}")
        decoder = Gst.ElementFactory.make("v4l2slh265dec", f"decoder-{name}")
        queue = Gst.ElementFactory.make("queue", f"queue-{name}")
        convert = Gst.ElementFactory.make("videoconvert", f"convert-{name}")
        scale = Gst.ElementFactory.make("videoscale", f"scale-{name}")
        rate = Gst.ElementFactory.make("videorate", f"rate-{name}")
        capsfilter = Gst.ElementFactory.make("capsfilter", f"caps-{name}")
        # input-selector erwartet laut GStreamer-Dokumentation eine eigene
        # Queue direkt vor jedem Sink-Pad, um den Branch-Thread vom
        # Pad-Switch/Flush-Handling des Selectors zu entkoppeln.
        selector_queue = Gst.ElementFactory.make("queue", f"selector-queue-{name}")

        elements = (source, demux, parse, decoder, queue, convert, scale, rate, capsfilter, selector_queue)

        if not all(elements):
            raise RuntimeError(f"Elemente für {name} konnten nicht erstellt werden")

        source.set_property("uri", url)
        source.set_property("keep-listening", True)
        demux.set_property("latency", 0)
        self._make_low_latency(queue)
        self._make_low_latency(selector_queue)

        # Alle Eingänge werden auf dasselbe Format normalisiert.
        # videorate wird benötigt, da Quellen mit variabler Framerate (z. B.
        # framerate=0/1 von einer iPhone-Kamera) sonst nicht auf die feste
        # framerate der Ziel-Caps negotiaten können.
        capsfilter.set_property("caps", self.settings.caps)

        for element in elements:
            self.pipeline.add(element)

        if not source.link(demux):
            raise RuntimeError(f"{name} konnte nicht mit tsdemux verbunden werden")

        parse.link(decoder)
        decoder.link(queue)
        queue.link(convert)
        convert.link(scale)
        scale.link(rate)
        rate.link(capsfilter)
        capsfilter.link(selector_queue)

        selector_pad = self.selector.request_pad_simple("sink_%u")
        source_pad = selector_queue.get_static_pad("src")

        if selector_pad is None or source_pad is None:
            raise RuntimeError(f"Selector-Pad für {name} fehlt")

        if source_pad.link(selector_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"{name} konnte nicht mit Selector verbunden werden")

        self.selector_pads[name] = selector_pad
        self._srt_sources[name] = source

        def on_caller_added(_element: Gst.Element, _sock_id: int, address) -> None:
            host = address.get_address().to_string()
            logger.info("Source '%s' connected from %s:%d", name, host, address.get_port())

        def on_caller_removed(_element: Gst.Element, _sock_id: int, address) -> None:
            host = address.get_address().to_string()
            logger.info("Source '%s' disconnected from %s:%d", name, host, address.get_port())

        def on_pad_added(_element: Gst.Element, pad: Gst.Pad) -> None:
            sink_pad = parse.get_static_pad("sink")

            if sink_pad is None or sink_pad.is_linked():
                return

            caps = pad.get_current_caps() or pad.query_caps(None)

            if caps.to_string().startswith("video/"):
                logger.info("Source '%s' streaming (%s)", name, caps.to_string())
                pad.link(sink_pad)

        def on_pad_removed(_element: Gst.Element, pad: Gst.Pad) -> None:
            sink_pad = parse.get_static_pad("sink")

            if sink_pad is not None and sink_pad.get_peer() == pad:
                pad.unlink(sink_pad)

        source.connect("caller-added", on_caller_added)
        source.connect("caller-removed", on_caller_removed)
        demux.connect("pad-added", on_pad_added)
        demux.connect("pad-removed", on_pad_removed)

    def _add_file_source(self, name: str, path: str) -> None:
        # uridecodebin statt fester Demux/Decoder-Kette: anders als bei den
        # SRT-Quellen ist der Codec einer beliebigen mp4-Datei nicht
        # vorhersehbar, daher wird hier bewusst autogeplugt.
        uri = path if "://" in path else Gst.filename_to_uri(os.path.abspath(path))

        decodebin = Gst.ElementFactory.make("uridecodebin", f"source-{name}")
        convert = Gst.ElementFactory.make("videoconvert", f"convert-{name}")
        scale = Gst.ElementFactory.make("videoscale", f"scale-{name}")
        rate = Gst.ElementFactory.make("videorate", f"rate-{name}")
        capsfilter = Gst.ElementFactory.make("capsfilter", f"caps-{name}")
        # Der gemeinsame Sink läuft mit sync=false (siehe __init__, wegen der
        # SRT-Quellen), daher würde eine Datei-Quelle sonst ungebremst so
        # schnell abgespielt, wie decodebin Frames liefern kann. identity mit
        # sync=true bremst diesen Zweig unabhängig davon auf Echtzeit runter,
        # anhand der eigenen Buffer-Timestamps und der Pipeline-Clock.
        pacer = Gst.ElementFactory.make("identity", f"pacer-{name}")
        # input-selector erwartet laut GStreamer-Dokumentation eine eigene
        # Queue direkt vor jedem Sink-Pad, um den Branch-Thread vom
        # Pad-Switch/Flush-Handling des Selectors zu entkoppeln.
        selector_queue = Gst.ElementFactory.make("queue", f"selector-queue-{name}")

        elements = (decodebin, convert, scale, rate, capsfilter, pacer, selector_queue)

        if not all(elements):
            raise RuntimeError(f"Elemente für {name} konnten nicht erstellt werden")

        decodebin.set_property("uri", uri)
        capsfilter.set_property("caps", self.settings.caps)
        pacer.set_property("sync", True)
        self._make_low_latency(selector_queue)

        for element in elements:
            self.pipeline.add(element)

        convert.link(scale)
        scale.link(rate)
        rate.link(capsfilter)
        capsfilter.link(pacer)
        pacer.link(selector_queue)

        selector_pad = self.selector.request_pad_simple("sink_%u")
        source_pad = selector_queue.get_static_pad("src")

        if selector_pad is None or source_pad is None:
            raise RuntimeError(f"Selector-Pad für {name} fehlt")

        if source_pad.link(selector_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"{name} konnte nicht mit Selector verbunden werden")

        self.selector_pads[name] = selector_pad
        self._file_sources[name] = decodebin

        # Datei-Quellen sollen nur laufen, während sie als Programm-Quelle
        # ausgewählt sind. Der Zweig bleibt deshalb standardmäßig in NULL
        # (keine geöffneten Geräte/Puffer im Speicher, kein belegter
        # Hardware-Decoder-Slot) und von automatischen Pipeline-weiten
        # Zustandswechseln abgekoppelt (locked-state) - switch() übernimmt
        # Start/Stop beim Umschalten. Falls diese Quelle der initial aktive
        # Selector-Pad ist, wird das am Ende von __init__ wieder rückgängig
        # gemacht.
        decodebin.set_locked_state(True)

        # Datei-Quellen sind endlich: statt die Wiedergabe am EOS enden zu
        # lassen (was, wenn diese Quelle gerade aktiv ist, über den Selector
        # bis zu den Sinks durchschlägt und die ganze Pipeline auf EOS
        # setzt), wird das EOS-Event direkt an dieser Pad-Probe abgefangen
        # und verworfen, und die Quelle stattdessen zum Anfang zurückgesetzt.
        # Das funktioniert unabhängig davon, ob die Quelle gerade aktiv
        # (sichtbar) oder nur im Hintergrund am Loopen ist.
        def do_seek() -> bool:
            ok, position = decodebin.query_position(Gst.Format.TIME)
            logger.info(
                "Datei-Quelle '%s': Seek auf 0 wird ausgefuehrt (aktuelle Position: %s)",
                name,
                f"{position / Gst.SECOND:.3f}s" if ok else "unbekannt",
            )
            result = decodebin.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                0,
            )
            logger.info("Datei-Quelle '%s': seek_simple() ergab %s", name, result)
            return GLib.SOURCE_REMOVE

        def on_eos_probe(_pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
            event = info.get_event()

            # FLUSH_START/FLUSH_STOP verwerfen: der Seek in do_seek() braucht
            # FLUSH, damit decodebin intern (Demuxer/Decoder) seinen
            # EOS-Zustand sauber zuruecksetzt - ohne FLUSH bleibt die Quelle
            # nach dem ersten Loop dauerhaft "zu Ende" und liefert nie wieder
            # Bilder. Laesst man das FLUSH_START/STOP-Paar aber bis zum
            # Selector durchlaufen, waehrend dieser Zweig gerade der aktive
            # Pad ist, reisst es den gemeinsamen Ausgang (output-convert/
            # tee/sink) mit. Schaltet die Pipeline kurz danach auf eine
            # Live-Quelle zurueck, bleibt deren eigener, nie geflushter
            # Zweig gegenueber dem frisch geflushten gemeinsamen Ausgang
            # inkonsistent - ihre Buffer werden ab dann dauerhaft und ohne
            # Fehlermeldung verworfen (beobachtet: Live-Bild friert nach dem
            # Zurueckschalten komplett ein). Der Flush bleibt hier deshalb
            # auf diesen Zweig beschraenkt: intern (oberhalb dieser Probe)
            # laeuft er normal durch und setzt decodebin zurueck, unterhalb
            # (Richtung Selector) wird er abgefangen.
            if event.type in (Gst.EventType.FLUSH_START, Gst.EventType.FLUSH_STOP):
                logger.info(
                    "Datei-Quelle '%s': %s an Selector-Grenze abgefangen",
                    name,
                    event.type.value_nick,
                )
                return Gst.PadProbeReturn.DROP

            if event.type != Gst.EventType.EOS:
                return Gst.PadProbeReturn.OK

            logger.info("Datei-Quelle '%s' beendet, starte Loop neu", name)
            # Der Seek darf nicht synchron aus der Probe heraus aufgerufen
            # werden: die Probe läuft im Streaming-Thread der Quelle, und
            # genau dieser Thread muss den durch den Seek ausgelösten
            # Flush verarbeiten. Ein blockierender Aufruf hier würde sich
            # selbst blockieren (die Datei würde nur einmal abgespielt).
            GLib.idle_add(do_seek)
            return Gst.PadProbeReturn.DROP

        # EVENT_DOWNSTREAM allein faengt laut GStreamer keine
        # Flush-Events ab ("events that are not flushing") - dafuer
        # extra EVENT_FLUSH, sonst laeuft der FLUSH_START/STOP-Abfang
        # oben ins Leere und die Events erreichen ungebremst den Selector.
        source_pad.add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM | Gst.PadProbeType.EVENT_FLUSH,
            on_eos_probe,
        )

        # decodebin baut bei jeder Auswahl (NULL -> PLAYING) seine interne
        # Decode-Kette komplett neu auf, pad-added feuert dabei jedes Mal
        # erneut. Drains für Nicht-Video-Pads (z. B. Audio) werden deshalb
        # pro Pad verfolgt und beim zugehörigen pad-removed wieder
        # abgebaut - sonst sammelt sich bei jedem Umschaltzyklus ein
        # weiteres, nie freigegebenes fakesink-Element in der Pipeline an.
        drains: dict[str, Gst.Element] = {}

        def on_pad_added(_element: Gst.Element, pad: Gst.Pad) -> None:
            caps = pad.get_current_caps() or pad.query_caps(None)
            sink_pad = convert.get_static_pad("sink")

            if caps.to_string().startswith("video/") and not sink_pad.is_linked():
                logger.info("Datei-Quelle '%s' spielt (%s)", name, caps.to_string())
                pad.link(sink_pad)
                return

            # Andere Streams (z. B. Audio) müssen abgeführt werden, sonst
            # blockiert decodebin intern auf dem ungenutzten Pad.
            drain = Gst.ElementFactory.make("fakesink", f"drain-{name}-{pad.get_name()}")
            drain.set_property("sync", False)
            drain.set_property("async", False)
            self.pipeline.add(drain)
            drain.sync_state_with_parent()
            pad.link(drain.get_static_pad("sink"))
            drains[pad.get_name()] = drain

        def on_pad_removed(_element: Gst.Element, pad: Gst.Pad) -> None:
            sink_pad = convert.get_static_pad("sink")

            if sink_pad is not None and sink_pad.get_peer() == pad:
                pad.unlink(sink_pad)
                return

            drain = drains.pop(pad.get_name(), None)

            if drain is not None:
                drain.set_state(Gst.State.NULL)
                self.pipeline.remove(drain)

        decodebin.connect("pad-added", on_pad_added)
        decodebin.connect("pad-removed", on_pad_removed)

    def start(self) -> None:
        result = self.pipeline.set_state(Gst.State.PLAYING)

        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer-Pipeline konnte nicht starten")

        if self.settings.screenshot_interval is not None:
            self._screenshot_timer_id = GLib.timeout_add_seconds(
                self.settings.screenshot_interval, self._capture_screenshot
            )

        self._stats_timer_id = GLib.timeout_add_seconds(5, self._log_srt_stats)

    def stop(self) -> None:
        if self._screenshot_timer_id is not None:
            GLib.source_remove(self._screenshot_timer_id)
            self._screenshot_timer_id = None

        if self._stats_timer_id is not None:
            GLib.source_remove(self._stats_timer_id)
            self._stats_timer_id = None

        self.pipeline.set_state(Gst.State.NULL)

    def _log_srt_stats(self) -> bool:
        for name, source_element in self._srt_sources.items():
            stats = source_element.get_property("stats")
            logger.info("Source '%s' SRT stats: %s", name, stats.to_string())

        return True

    def _capture_screenshot(self) -> bool:
        sample = self.screenshot_sink.emit("try-pull-sample", Gst.SECOND)

        if sample is None:
            logger.info("Screenshot übersprungen: kein Frame verfügbar (Quelle aktiv?)")
            return True

        path = os.path.abspath(self.settings.screenshot_path)

        try:
            self._write_png(sample, path)
        except Exception:
            logger.exception("Screenshot konnte nicht geschrieben werden: %s", path)
        else:
            logger.info("Screenshot gespeichert unter %s", path)

        return True

    @staticmethod
    def _write_png(sample: Gst.Sample, path: str) -> None:
        encode_pipeline = Gst.parse_launch(
            "appsrc name=src format=time ! pngenc ! filesink name=sink"
        )
        appsrc = encode_pipeline.get_by_name("src")
        filesink = encode_pipeline.get_by_name("sink")

        appsrc.set_property("caps", sample.get_caps())
        filesink.set_property("location", path)

        encode_pipeline.set_state(Gst.State.PLAYING)
        appsrc.emit("push-buffer", sample.get_buffer())
        appsrc.emit("end-of-stream")

        bus = encode_pipeline.get_bus()
        bus.timed_pop_filtered(
            Gst.CLOCK_TIME_NONE, Gst.MessageType.EOS | Gst.MessageType.ERROR
        )
        encode_pipeline.set_state(Gst.State.NULL)

    def switch(self, name: str) -> None:
        pad = self.selector_pads.get(name)

        if pad is None:
            raise KeyError(name)

        # GStreamer-Objekte sollten aus dem GLib-Kontext verändert werden.
        GLib.idle_add(self._apply_switch, name, pad)

    def _apply_switch(self, name: str, pad: Gst.Pad) -> bool:
        previous = self._active_source
        self._active_source = name
        self.selector.set_property("active-pad", pad)

        if previous != name:
            previous_element = self._file_sources.get(previous) if previous else None

            if previous_element is not None:
                # NULL statt nur PAUSED: schließt das v4l2-Decoder-Gerät und
                # gibt dessen Puffer wieder frei, statt sie nur ruhen zu
                # lassen. Wichtig auf diesem Pi, da RAM knapp ist und die
                # Hardware ohnehin nur begrenzt viele gleichzeitige
                # HEVC-Decode-Sessions erlaubt (die beiden SRT-Kameras
                # belegen bereits je eine).
                self._set_state_checked(previous_element, Gst.State.NULL, previous)
                previous_element.set_locked_state(True)

            new_element = self._file_sources.get(name)

            if new_element is not None:
                # Kein expliziter seek_simple nötig: aus NULL heraus baut
                # decodebin die Decode-Kette komplett neu auf und beginnt
                # ohnehin bei Position 0.
                new_element.set_locked_state(False)
                self._set_state_checked(new_element, Gst.State.PLAYING, name)

        return GLib.SOURCE_REMOVE

    @staticmethod
    def _set_state_checked(element: Gst.Element, state: Gst.State, label: str) -> None:
        result = element.set_state(state)

        if result == Gst.StateChangeReturn.FAILURE:
            logger.error("Zustandswechsel für '%s' auf %s fehlgeschlagen", label, state.value_nick)
            return

        if state != Gst.State.NULL:
            return

        # set_state(NULL) sollte laut GStreamer-Semantik synchron
        # abschließen, aber erst get_state() bestätigt das wirklich. Relevant
        # hier, weil der v4l2-Hardware-Decoder sein Gerät und seine Puffer
        # erst nach vollständigem Abschluss freigibt und sich beide
        # Live-Quellen denselben begrenzten Pool gleichzeitiger
        # HEVC-Decode-Sessions teilen - ein unvollständiger Teardown einer
        # Datei-Quelle kann sich damit auf die Live-Quellen auswirken.
        change_return, _, _ = element.get_state(5 * Gst.SECOND)

        if change_return != Gst.StateChangeReturn.SUCCESS:
            logger.warning(
                "Zustandswechsel für '%s' auf NULL nicht sauber abgeschlossen (%s)",
                label,
                change_return.value_nick,
            )
