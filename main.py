"""
Eval Co-pilot Main Application Entrypoint.
Serves from a single server:
- /          the web UI (web/index.html, plain HTML/CSS/JS calling the REST API)
- /api/...   the REST API
- /classic   the original Gradio UI, kept as a fallback
"""

from pathlib import Path

import uvicorn
import gradio as gr
from fastapi import HTTPException
from fastapi.responses import FileResponse

from api import app as fastapi_app
from ui import create_gradio_app

WEB_DIR = Path(__file__).resolve().parent / "web"
# Explicit allowlist, so tests and anything else in web/ are never published
WEB_ASSETS = {"app.js", "lib.js", "styles.css"}


@fastapi_app.get("/", include_in_schema=False)
def web_ui():
    return FileResponse(WEB_DIR / "index.html")


@fastapi_app.get("/assets/{name}", include_in_schema=False)
def web_asset(name: str):
    if name not in WEB_ASSETS:
        raise HTTPException(status_code=404)
    return FileResponse(WEB_DIR / name)


app = gr.mount_gradio_app(fastapi_app, create_gradio_app(), path="/classic")

if __name__ == "__main__":
    print("Starting Eval Co-pilot on http://127.0.0.1:8000...")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
