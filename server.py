from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

import gi
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

gi.require_version("Gst", "1.0")
from gi.repository import GLib

from video_switcher import VideoSettings, VideoSwitcher


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

VIDEO_SETTINGS = VideoSettings(
    sources={
        # latency= is the receiver's requested SRT TSBPD delay in ms; the
        # effective delay is the max of this and whatever the sender (the
        # iPhone streaming app) requests, so lowering this only helps if the
        # sender isn't already asking for more.
        "iphone": "srt://0.0.0.0:6001?mode=listener&latency=120",
        "ipad": "srt://0.0.0.0:6002?mode=listener&latency=120",
    },
    # Direct KMS/DRM output; avoids autovideosink guessing a GL/Wayland sink
    # that isn't available on a headless console.
    sink="kmssink",
)


class SwitchRequest(BaseModel):
    source: str


switcher = VideoSwitcher(VIDEO_SETTINGS)
glib_loop = GLib.MainLoop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop_thread = threading.Thread(target=glib_loop.run, daemon=True)
    loop_thread.start()
    switcher.start()

    yield

    switcher.stop()
    glib_loop.quit()


app = FastAPI(lifespan=lifespan)


@app.get("/sources")
def list_sources() -> dict:
    return {"sources": list(VIDEO_SETTINGS.sources)}


@app.put("/program")
def select_program(request: SwitchRequest) -> dict:
    try:
        switcher.switch(request.source)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unbekannte Videoquelle")

    return {
        "status": "ok",
        "active_source": request.source,
    }
    
    
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
    )