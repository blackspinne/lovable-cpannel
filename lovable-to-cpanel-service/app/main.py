# app/main.py
from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from pathlib import Path
import io, re

from .processor import process_lovable_zip

app = FastAPI(title="Lovable → cPanel Slug Builder", version="v1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

INDEX_HTML = (Path(__file__).parent.parent/"static"/"index.html").read_text(encoding="utf-8")

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

@app.post("/build")
async def build(slug: str = Form(...), file: UploadFile = Form(...)):
    if not re.fullmatch(r"[a-z0-9][a-z0-9/_-]*", slug or ""):
        raise HTTPException(400, "Slug inválida.")
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Envie um .zip do Lovable.")
    data = await file.read()
    try:
        out_bytes = process_lovable_zip(data, slug)
    except Exception as e:
        raise HTTPException(400, f"Falha no processamento: {e}")
    buf = io.BytesIO(out_bytes); buf.seek(0)
    headers = {"Content-Disposition": f'attachment; filename="{slug}-site.zip"'}
    return StreamingResponse(buf, media_type="application/zip", headers=headers,
                             background=BackgroundTask(buf.close))
