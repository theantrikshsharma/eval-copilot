"""
MANTIS Main Application Entrypoint.
Serves from a single server:
- /          the web UI (web/index.html, plain HTML/CSS/JS calling the REST API)
- /api/...   the REST API
- /classic   the original Gradio UI, kept as an optional local fallback

This module's `app` is also the Vercel entry point (see vercel.json).
"""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import db
from api import app as fastapi_app

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


def _with_classic_view(app: FastAPI) -> FastAPI:
    """
    Mounts the legacy Gradio view at /classic when it is installed (requirements-classic.txt) and not disabled
    with MANTIS_CLASSIC_UI=0. It calls the evaluator and database directly, so it cannot honour the access token:
    it is never mounted on a hosted deployment or when MANTIS_ADMIN_TOKEN is set.
    """
    if os.getenv("MANTIS_CLASSIC_UI", "1") == "0" or db.is_hosted() or os.getenv("MANTIS_ADMIN_TOKEN"):
        return app
    try:
        import gradio as gr
        from ui import create_gradio_app
    except ImportError:
        print("[MANTIS] Classic view at /classic is off: install requirements-classic.txt to enable it.")
        return app
    return gr.mount_gradio_app(app, create_gradio_app(), path="/classic")


app = _with_classic_view(fastapi_app)

if __name__ == "__main__":
    import uvicorn

    print("Starting MANTIS on http://127.0.0.1:8000...")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
