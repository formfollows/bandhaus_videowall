# licht

A GStreamer-based video switcher for camera sources, controlled over a small FastAPI HTTP API. It builds a live pipeline with an `input-selector` so the active camera can be switched at runtime without restarting playback.

## How it works

- Each configured source (any URI scheme `uridecodebin` supports — RTSP, SRT, etc.) is decoded, normalized to a common format (1920x1080@30fps by default, via `videoconvert` / `videoscale` / `videorate`), and fed into a shared `input-selector`. `videorate` is required because live sources (e.g. a phone camera) often report a variable framerate (`framerate=0/1`), which otherwise fails to negotiate against the fixed output caps.
- The selector's output is split with a `tee`: one branch goes through `videoconvert` into a video sink (`autovideosink` by default), the other periodically feeds a screenshot capture branch (see below).
- Connects and disconnects on listener-mode sources (e.g. SRT) are logged, along with any GStreamer pipeline errors/warnings.
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

`VideoSettings` fields:

| Field                | Default              | Description                                                    |
| --------------------- | -------------------- | ---------------------------------------------------------------- |
| `sources`             | `{}`                 | Map of source name to URI                                       |
| `sink`                | `"autovideosink"`    | Output sink (e.g. `kmssink`, `waylandsink`, `glimagesink`)      |
| `width` / `height`    | `1920` / `1080`      | Normalized output resolution                                    |
| `framerate`           | `30`                 | Normalized output framerate                                     |
| `screenshot_path`     | `"screenshot.png"`   | Where periodic screenshots are written (overwritten in place)   |
| `screenshot_interval` | `10`                 | Seconds between screenshot captures                             |

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

## Screenshots

Every `screenshot_interval` seconds, the currently active program output is captured and written as a PNG to `screenshot_path` (the file is overwritten in place). If no source is currently streaming into the active pad, the capture is skipped and logged (`Screenshot übersprungen: kein Frame verfügbar`).

There is no HTTP endpoint to fetch the screenshot yet — see `TODO.md`.
