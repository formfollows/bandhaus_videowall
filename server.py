from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

import gi
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

gi.require_version("Gst", "1.0")
from gi.repository import GLib

from video_switcher import VideoSwitcher


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CAMERAS = {
    "iphone": "srt://0.0.0.0:6001?mode=listener",
}


class SwitchRequest(BaseModel):
    source: str


switcher = VideoSwitcher(CAMERAS)
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
    return {"sources": list(CAMERAS)}


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