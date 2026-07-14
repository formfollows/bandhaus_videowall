# licht

A GStreamer-based video switcher for RTSP camera sources, controlled over a small FastAPI HTTP API. It builds a live pipeline with an `input-selector` so the active camera can be switched at runtime without restarting playback.

## How it works

- Each configured RTSP source is decoded (`uridecodebin`), normalized to a common format (1920x1080@30fps), and fed into a shared `input-selector`.
- The selector's output goes through `videoconvert` into a video sink (`autovideosink` by default).
- A background GLib main loop drives the GStreamer pipeline while FastAPI serves API requests on the main thread.

Cameras and pipeline settings are defined in `server.py` via `VideoSettings`:

```python
VIDEO_SETTINGS = VideoSettings(
    sources={
        "cam1": "rtsp://127.0.0.1:8554/cam1",
        "cam2": "rtsp://127.0.0.1:8554/cam2",
        "cam3": "rtsp://127.0.0.1:8554/cam3",
    },
)
```

## Requirements

System packages (installed via `makefile`):

- `python3`, `python3-gi`, `python3-gst-1.0`
- GStreamer 1.0 core, tools, and plugin sets (`base`, `good`, `bad`, `ugly`, `libav`)

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

> On a Raspberry Pi or a Wayland session, set `VideoSettings(sink=...)` to a sink appropriate for the display (e.g. `kmssink`, `waylandsink`, or `glimagesink`).

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
