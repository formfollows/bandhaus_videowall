# licht

A GStreamer-based video switcher for SRT camera sources (e.g. an iPhone streaming app), controlled over a small FastAPI HTTP API. It builds a live pipeline with an `input-selector` so the active camera can be switched at runtime without restarting playback.

## How it works

- Each configured source is an SRT listener with a fixed decode chain — `srtsrc` (listener mode) → `tsdemux` → `h265parse` → `v4l2slh265dec` (hardware-accelerated stateless HEVC decode) — rather than `uridecodebin`, so it never falls back to a software H265 decoder.
- The decoded frame is normalized to a common format (1280x720@30fps by default, via `videoconvert` / `videoscale` / `videorate`) and fed into a shared `input-selector`. `videorate` is required because live sources (e.g. a phone camera) often report a variable framerate (`framerate=0/1`), which otherwise fails to negotiate against the fixed output caps.
- The selector's output is split with a `tee`: one branch goes through `videoconvert` into a video sink (`kmssink` by default, for direct KMS/DRM output on a headless console), the other feeds an `appsink`-based screenshot capture branch (see below). `sync` is forced off on the sink, since a live SRT source can drift enough against the pipeline clock that `kmssink`'s default `sync=true` never releases a frame.
- All queues are low-latency (`leaky=downstream`, small `max-size-buffers`) so a network hiccup or decoder stall gets shed instead of building up an ever-growing backlog.
- Connects/disconnects on each SRT listener source, per-source SRT stats (every 5s), pipeline latency recalculations, and any GStreamer pipeline errors/warnings are all logged.
- A background GLib main loop drives the GStreamer pipeline while FastAPI serves API requests on the main thread.

Cameras and pipeline settings are defined in `server.py` via `VideoSettings`:

```python
VIDEO_SETTINGS = VideoSettings(
    sources={
        "iphone": "srt://0.0.0.0:6001?mode=listener&latency=120",
    },
    sink="kmssink",
)
```

`VideoSettings` fields:

| Field                | Default              | Description                                                              |
| --------------------- | -------------------- | -------------------------------------------------------------------------- |
| `sources`             | `{}`                 | Map of source name to SRT URI (`srt://...?mode=listener&...`)             |
| `sink`                | `"kmssink"`           | Output sink (e.g. `kmssink`, `waylandsink`, `glimagesink`)                |
| `width` / `height`    | `1280` / `720`        | Normalized output resolution                                             |
| `framerate`           | `30`                 | Normalized output framerate                                              |
| `screenshot_path`     | `"screenshot.png"`   | Where periodic screenshots are written (overwritten in place)             |
| `screenshot_interval` | `None`               | Seconds between screenshot captures; `None` disables periodic capture     |

## Requirements

System packages (installed via `makefile`):

- `python3`, `python3-gi`, `python3-gst-1.0`
- GStreamer 1.0 core, tools, and plugin sets (`base`, `good`, `bad`, `ugly`, `libav`)
- A `v4l2slh265dec` element (stateless V4L2 HEVC decode) — on a Raspberry Pi this needs a kernel/driver with V4L2 request-API HEVC support; the pipeline will fail to build without it

Python packages (install separately, not yet pinned in a manifest):

- `fastapi`
- `pydantic`
- `uvicorn`
- `PyGObject` (provides the `gi` module, usually installed via `python3-gi`)

## Setup

Install system dependencies and verify the GStreamer install:

```bash
make -f makefile
```

This installs the required apt packages, prints the `gst-launch-1.0` version, and checks that the `Gst` GObject Introspection bindings import correctly.

Then install the Python dependencies:

```bash
pip install fastapi pydantic uvicorn
```

## Running

```bash
python3 server.py
```

The API listens on `0.0.0.0:8000`.

> The default sink is `kmssink` for direct KMS/DRM output on a headless Raspberry Pi console. On a Wayland session, set `VideoSettings(sink=...)` to a sink appropriate for the display instead (e.g. `waylandsink` or `glimagesink`).
>
> There's a known issue where `v4l2slh265dec` → `kmssink` with DMA-BUF can produce a corrupted image on some systems; routing through an extra `videoconvert` before `kmssink` avoids it (see `TODO.md`).

## API

### `GET /sources`

List the configured camera source names.

```bash
curl http://localhost:8000/sources
```

```json
{"sources": ["cam1", "cam2", "cam3"]}
```

### `PUT /program`

Switch the active (program) video source.

```bash
curl -X PUT http://localhost:8000/program \
  -H "Content-Type: application/json" \
  -d '{"source": "cam2"}'
```

```json
{"status": "ok", "active_source": "cam2"}
```

Returns `404` if the requested source name isn't configured.

## Screenshots

Periodic screenshots are off by default (`screenshot_interval=None`). When set to a number of seconds, the currently active program output is captured on that interval and written as a PNG to `screenshot_path` (the file is overwritten in place). If no source is currently streaming into the active pad, the capture is skipped and logged (`Screenshot übersprungen: kein Frame verfügbar (Quelle aktiv?)`).

There is no HTTP endpoint to fetch the screenshot yet.
