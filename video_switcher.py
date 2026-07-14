from __future__ import annotations

import logging

import gi

gi.require_version("Gst", "1.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib, Gst


logger = logging.getLogger(__name__)

Gst.init(None)


class VideoSwitcher:
    def __init__(self, sources: dict[str, str]) -> None:
        self.pipeline = Gst.Pipeline.new("video-switcher")
        self.selector = Gst.ElementFactory.make("input-selector", "selector")
        self.convert = Gst.ElementFactory.make("videoconvert", "output-convert")

        # Auf einem Raspberry Pi oder Wayland-System gegebenenfalls ersetzen,
        # z. B. durch kmssink, waylandsink oder glimagesink.
        self.sink = Gst.ElementFactory.make("autovideosink", "video-output")

        if not all((self.pipeline, self.selector, self.convert, self.sink)):
            raise RuntimeError("Benötigte GStreamer-Elemente fehlen")

        self.pipeline.add(self.selector)
        self.pipeline.add(self.convert)
        self.pipeline.add(self.sink)

        if not self.selector.link(self.convert):
            raise RuntimeError("Selector konnte nicht verbunden werden")

        if not self.convert.link(self.sink):
            raise RuntimeError("Videoausgabe konnte nicht verbunden werden")

        self.selector_pads: dict[str, Gst.Pad] = {}

        for name, url in sources.items():
            self._add_source(name, url)

    def _add_source(self, name: str, url: str) -> None:
        source = Gst.ElementFactory.make("uridecodebin", f"source-{name}")
        queue = Gst.ElementFactory.make("queue", f"queue-{name}")
        convert = Gst.ElementFactory.make("videoconvert", f"convert-{name}")
        scale = Gst.ElementFactory.make("videoscale", f"scale-{name}")
        capsfilter = Gst.ElementFactory.make("capsfilter", f"caps-{name}")

        if not all((source, queue, convert, scale, capsfilter)):
            raise RuntimeError(f"Elemente für {name} konnten nicht erstellt werden")

        source.set_property("uri", url)

        # Alle Eingänge werden auf dasselbe Format normalisiert.
        capsfilter.set_property(
            "caps",
            Gst.Caps.from_string(
                "video/x-raw,width=1920,height=1080,framerate=30/1"
            ),
        )

        for element in (source, queue, convert, scale, capsfilter):
            self.pipeline.add(element)

        queue.link(convert)
        convert.link(scale)
        scale.link(capsfilter)

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

    def start(self) -> None:
        result = self.pipeline.set_state(Gst.State.PLAYING)

        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer-Pipeline konnte nicht starten")

    def stop(self) -> None:
        self.pipeline.set_state(Gst.State.NULL)

    def switch(self, name: str) -> None:
        pad = self.selector_pads.get(name)

        if pad is None:
            raise KeyError(name)

        # GStreamer-Objekte sollten aus dem GLib-Kontext verändert werden.
        GLib.idle_add(self.selector.set_property, "active-pad", pad)
