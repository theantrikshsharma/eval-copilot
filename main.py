"""
Eval Co-pilot Main Application Entrypoint.
Mounts the Gradio UI directly onto the FastAPI backend,
providing both REST API endpoints (/api/...) and the full interactive UI (/)
from a single unified server.
"""

import uvicorn
import gradio as gr
from api import app as fastapi_app
from ui import create_gradio_app

# Create Gradio interface
gradio_ui = create_gradio_app()

# Mount Gradio onto FastAPI under root "/"
# FastAPI handles /api routes first because they are registered prior to mounting
app = gr.mount_gradio_app(fastapi_app, gradio_ui, path="/")

if __name__ == "__main__":
    print("Starting Eval Co-pilot on http://127.0.0.1:8000...")
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
