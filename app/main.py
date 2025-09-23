# app/main.py
import asyncio
import io
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ===== Configs =====
MAX_UPLOAD_MB = 100                        # limite amigável (proxy do Render costuma aceitar ~100MB)
KEEP_TASK_MINUTES = 30                     # quanto tempo manter artefatos pra download
PUBLIC_BASE_URL = ""                       # deixe vazio; o front usa caminho relativo

# ===== App & static =====
app = FastAPI(title="Lovable → cPanel (Slug)")
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")

# ===== Modelos/status =====
class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"

class Task(BaseModel):
    id: str
    status: TaskStatus
    progress: int = 0              # 0..100
    eta: Optional[str] = None      # texto amigável
    message: Optional[str] = None  # erro ou detalhe
    slug: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    download_path: Optional[str] = None    # path local pro site.zip

# ===== Memória: fila + tarefas =====
TASKS: Dict[str, Task] = {}
QUEUE: asyncio.Queue[str] = asyncio.Queue()
WORKER_LOCK = asyncio.Lock()   # garante 1 execução por vez

def _eta_text(start: datetime, pct: int) -> Optional[str]:
    if pct <= 0 or pct >= 100: return None
    elapsed = datetime.utcnow() - start
    try:
        total = elapsed / (pct / 100.0)
        left = total - elapsed
        # arredonda pro mais amigável
        secs = max(1, int(left.total_seconds()))
        if secs < 60: return f"{secs}s"
        mins = secs // 60
        secs2 = secs % 60
        if mins < 60: return f"{mins}m {secs2}s"
        hours = mins // 60
        mins2 = mins % 60
        return f"{hours}h {mins2}m"
    except Exception:
        return None

# ===== Util: tamanho do UploadFile sem carregar tudo em memória =====
def file_size_bytes(upload_file: UploadFile) -> int:
    pos = upload_file.file.tell()
    upload_file.file.seek(0, 2)
    size = upload_file.file.tell()
    upload_file.file.seek(pos, 0)
    return size

# ====== Conversor principal ======
def write_htaccess(target_dir: Path, slug: str):
    ht = target_dir / ".htaccess"
    rules = (
        "DirectoryIndex index.html\n"
        "RewriteEngine On\n"
        f"RewriteBase /{slug}/\n\n"
        "RewriteCond %{REQUEST_FILENAME} -f [OR]\n"
        "RewriteCond %{REQUEST_FILENAME} -d\n"
        "RewriteRule ^ - [L]\n\n"
        "RewriteRule . index.html [L]\n"
    )
    ht.write_text(rules, encoding="utf-8")

def sanity_fix_html_css(dist_dir: Path):
    # Correções simples de caminhos locais em .html/.css (evita links com "/" absoluto)
    for htmlp in dist_dir.rglob("*.html"):
        s = htmlp.read_text(encoding="utf-8", errors="ignore")
        s2 = (
            s.replace('href="/assets/', 'href="assets/')
             .replace('src="/assets/', 'src="assets/')
        )
        if s2 != s:
            htmlp.write_text(s2, encoding="utf-8")
    for cssp in dist_dir.rglob("*.css"):
        s = cssp.read_text(encoding="utf-8", errors="ignore")
        s2 = s.replace('url(/', 'url(')
        if s2 != s:
            cssp.write_text(s2, encoding="utf-8")

def zip_dir_contents(src_dir: Path, out_zip: Path):
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in src_dir.rglob("*"):
            if f.is_file():
                z.write(f, arcname=f.relative_to(src_dir).as_posix())

def convert_lovable_zip(in_zip: Path, slug: str, progress_cb):
    """
    Conversão minimalista e robusta:
    - Extrai ZIP enviado
    - *Se já vier um build* (pasta dist/build/out), empacota ele
    - Caso contrário, empacota todo conteúdo (pass-through) e gera .htaccess
    - Aplica correções simples de caminhos (/assets → assets etc.)
    Obs.: esta versão NÃO roda npm build (limite do plano free). Para a maioria
    dos casos exportados pelo Lovable, funciona porque o ZIP já vem pronto.
    """
    progress_cb(5, "Extraindo projeto…")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "src"
        src.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(in_zip, "r") as z:
            z.extractall(src)

        # procura build pronto
        progress_cb(20, "Procurando build existente…")
        dist = None
        for cand in ("dist", "build", "out"):
            for p in src.glob(f"**/{cand}"):
                if (p / "index.html").exists():
                    dist = p
                    break
            if dist: break

        if not dist:
            # Pass-through (melhor do que falhar): pega a raiz que contém index.html
            progress_cb(35, "Build não encontrado — usando conteúdo do ZIP…")
            for p in src.glob("**/index.html"):
                dist = p.parent
                break
            if not dist:
                # fallback final: usa o zip inteiro
                dist = src

        # ajustes finais
        progress_cb(60, "Gerando .htaccess e correções…")
        write_htaccess(dist, slug)
        sanity_fix_html_css(dist)

        # zip final
        progress_cb(85, "Compactando site.zip…")
        out_zip = tmpdir / "site.zip"
        zip_dir_contents(dist, out_zip)

        progress_cb(100, "Pronto.")
        # retorna caminho temporário do zip (o caller move pra pasta definitiva)
        return out_zip

# ===== Worker que processa a fila =====
async def worker():
    while True:
        task_id = await QUEUE.get()
        t = TASKS.get(task_id)
        if not t:
            QUEUE.task_done()
            continue

        async with WORKER_LOCK:
            t.status = TaskStatus.running
            t.started_at = datetime.utcnow()
            t.progress = 1
            t.eta = _eta_text(t.started_at, t.progress)

            def step(pct, msg):
                t.progress = max(t.progress, int(pct))
                t.message = msg
                t.eta = _eta_text(t.started_at, t.progress)

            try:
                # salva upload num arquivo temporário
                step(3, "Preparando arquivo…")
                tmp_in = Path(tempfile.mkdtemp()) / "input.zip"
                shutil.copyfile(t.download_path, tmp_in)  # no enqueue usei esse campo p/ guardar upload

                # roda conversor
                tmp_out = convert_lovable_zip(tmp_in, t.slug or "site", step)

                # move para pasta definitiva de downloads
                downloads_dir = Path("/tmp/sitezips")
                downloads_dir.mkdir(parents=True, exist_ok=True)
                final_zip = downloads_dir / f"{t.slug or 'site'}-{t.id}.zip"
                shutil.move(str(tmp_out), final_zip)

                t.download_path = str(final_zip)
                t.status = TaskStatus.done
                t.progress = 100
                t.eta = None
                t.message = "OK"
                t.finished_at = datetime.utcnow()
            except Exception as e:
                t.status = TaskStatus.error
                t.message = f"Erro: {e}"
                t.finished_at = datetime.utcnow()
            finally:
                QUEUE.task_done()

# inicia o worker no startup
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(worker())

# limpeza simples de tarefas antigas
def gc_old_tasks():
    cutoff = datetime.utcnow() - timedelta(minutes=KEEP_TASK_MINUTES)
    to_del = []
    for tid, t in TASKS.items():
        if t.finished_at and t.finished_at < cutoff:
            # tenta apagar arquivo
            if t.download_path and os.path.exists(t.download_path):
                try: os.remove(t.download_path)
                except Exception: pass
            to_del.append(tid)
    for tid in to_del:
        TASKS.pop(tid, None)

# ===== Endpoints =====
@app.post("/tasks")
async def enqueue(
    slug: str = Form(...),
    file: UploadFile = Form(...)
):
    # validações amigáveis
    slug = (slug or "").strip()
    if not slug:
        raise HTTPException(status_code=400, detail="Slug é obrigatória.")

    size = file_size_bytes(file)
    if size > MAX_UPLOAD_MB * 1024 * 1024:
        mb = round(size / (1024*1024), 2)
        raise HTTPException(status_code=413, detail=f"ZIP muito grande ({mb} MB). Envie até {MAX_UPLOAD_MB} MB.")

    # cria task
    task_id = str(uuid.uuid4())
    t = Task(
        id=task_id,
        status=TaskStatus.queued,
        created_at=datetime.utcnow(),
        progress=0,
        slug=slug,
        message="Na fila…",
        download_path=None,
    )

    # salva upload em /tmp para o worker usar
    upload_dir = Path("/tmp/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    up_path = upload_dir / f"upload-{task_id}.zip"
    # stream pra não estourar memória
    with open(up_path, "wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk: break
            out.write(chunk)
    t.download_path = str(up_path)   # reaproveito o campo

    TASKS[task_id] = t
    await QUEUE.put(task_id)

    # posição na fila (aproximada)
    qsize = QUEUE.qsize()
    pos = max(0, qsize - 0)  # este acabou de entrar
    msg_pos = f"Na fila (pos. ~{pos+1})…" if pos > 0 else "Aguardando processar…"
    return {"task_id": task_id, "queue_position": pos+1, "message": msg_pos}

@app.get("/tasks/{task_id}")
def status(task_id: str):
    gc_old_tasks()
    t = TASKS.get(task_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada.")
    data = {
        "id": t.id,
        "status": t.status,
        "progress": t.progress,
        "eta": t.eta,
        "message": t.message,
    }
    if t.status == TaskStatus.done and t.download_path:
        # URL de download
        data["download_url"] = f"{PUBLIC_BASE_URL}/download/{t.id}"
    return JSONResponse(data)

@app.get("/download/{task_id}")
def download(task_id: str):
    t = TASKS.get(task_id)
    if not t or t.status != TaskStatus.done or not t.download_path:
        raise HTTPException(status_code=404, detail="Arquivo não disponível.")
    filename = f"site.zip"  # nome amigável pro usuário
    return FileResponse(
        path=t.download_path,
        filename=filename,
        media_type="application/zip"
    )
