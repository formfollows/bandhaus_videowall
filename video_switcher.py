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

    # Auf einem Raspberry Pi oder Wayland-System gegebenenfalls ersetzen,
    # z. B. durch kmssink, waylandsink oder glimagesink.
    sink: str = "autovideosink"
    width: int = 1920
    height: int = 1080
    framerate: int = 30

    screenshot_path: str = "screenshot.png"
    screenshot_interval: int = 10

    @property
    def caps(self) -> Gst.Caps:
        return Gst.Caps.from_string(
            f"video/x-raw,width={self.width},height={self.height},"
            f"framerate={self.framerate}/1"
        )


class VideoSwitcher:
    def __init__(self, settings: VideoSettings | None = None) -> None:
        self.settings = settings or VideoSettings()

        self.pipeline = Gst.Pipeline.new("video-switcher")
        self.selector = Gst.ElementFactory.make("input-selector", "selector")
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

        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)
        bus.connect("message::warning", self._on_bus_warning)

        for name, url in self.settings.sources.items():
            self._add_source(name, url)

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

    def _add_source(self, name: str, url: str) -> None:
        source = Gst.ElementFactory.make("uridecodebin", f"source-{name}")
        queue = Gst.ElementFactory.make("queue", f"queue-{name}")
        convert = Gst.ElementFactory.make("videoconvert", f"convert-{name}")
        scale = Gst.ElementFactory.make("videoscale", f"scale-{name}")
        rate = Gst.ElementFactory.make("videorate", f"rate-{name}")
        capsfilter = Gst.ElementFactory.make("capsfilter", f"caps-{name}")

        if not all((source, queue, convert, scale, rate, capsfilter)):
            raise RuntimeError(f"Elemente für {name} konnten nicht erstellt werden")

        source.set_property("uri", url)
        self._make_low_latency(queue)

        # Alle Eingänge werden auf dasselbe Format normalisiert.
        # videorate wird benötigt, da Quellen mit variabler Framerate (z. B.
        # framerate=0/1 von einer iPhone-Kamera) sonst nicht auf die feste
        # framerate der Ziel-Caps negotiaten können.
        capsfilter.set_property("caps", self.settings.caps)

        for element in (source, queue, convert, scale, rate, capsfilter):
            self.pipeline.add(element)

        queue.link(convert)
        convert.link(scale)
        scale.link(rate)
        rate.link(capsfilter)

        selector_pad = self.selector.request_pad_simple("sink_%u")
        source_pad = capsfilter.get_static_pad("src")

        if selector_pad is None or source_pad is None:
            raise RuntimeError(f"Selector-Pad für {name} fehlt")

        if source_pad.link(selector_pad) != Gst.PadLinkReturn.OK:
            raise RuntimeError(f"{name} konnte nicht mit Selector verbunden werden")

        self.selector_pads[name] = selector_pad

        def on_caller_added(_element: Gst.Element, _sock_id: int, address) -> None:
            host = address.get_address().to_string()
            logger.info("Source '%s' connected from %s:%d", name, host, address.get_port())

        def on_caller_removed(_element: Gst.Element, _sock_id: int, address) -> None:
            host = address.get_address().to_string()
            logger.info("Source '%s' disconnected from %s:%d", name, host, address.get_port())

        def on_source_setup(_element: Gst.Element, source_element: Gst.Element) -> None:
            try:
                source_element.connect("caller-added", on_caller_added)
                source_element.connect("caller-removed", on_caller_removed)
            except TypeError:
                pass

        def on_deep_element_added(
            _bin: Gst.Bin, _sub_bin: Gst.Bin, element: Gst.Element
        ) -> None:
            factory = element.get_factory()

            if factory is None or "Codec/Decoder/Video" not in factory.get_klass():
                return

            factory_name = factory.get_name()
            backend = "hardware" if factory_name.startswith("v4l2sl") else "software"
            logger.info("Source '%s' using %s decoder: %s", name, backend, factory_name)

        def on_pad_added(element: Gst.Element, pad: Gst.Pad) -> None:
            sink_pad = queue.get_static_pad("sink")

            if sink_pad is None or sink_pad.is_linked():
                return

            caps = pad.get_current_caps() or pad.query_caps(None)

            if caps.to_string().startswith("video/"):
                logger.info("Source '%s' streaming (%s)", name, caps.to_string())
                pad.link(sink_pad)

        def on_pad_removed(element: Gst.Element, pad: Gst.Pad) -> None:
            sink_pad = queue.get_static_pad("sink")

            if sink_pad is not None and sink_pad.get_peer() == pad:
                pad.unlink(sink_pad)

        source.connect("source-setup", on_source_setup)
        source.connect("pad-added", on_pad_added)
        source.connect("pad-removed", on_pad_removed)
        source.connect("deep-element-added", on_deep_element_added)

    def start(self) -> None:
        result = self.pipeline.set_state(Gst.State.PLAYING)

        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer-Pipeline konnte nicht starten")

        self._screenshot_timer_id = GLib.timeout_add_seconds(
            self.settings.screenshot_interval, self._capture_screenshot
        )

    def stop(self) -> None:
        if self._screenshot_timer_id is not None:
            GLib.source_remove(self._screenshot_timer_id)
            self._screenshot_timer_id = None

        self.pipeline.set_state(Gst.State.NULL)

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
        GLib.idle_add(self.selector.set_property, "active-pad", pad)
