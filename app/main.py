# app/main.py
import asyncio
import io
import time
import uuid
from collections import deque
from typing import Optional, Dict, Deque, Callable

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI()

# Servir / (frontend estático)
app.mount("/", StaticFiles(directory="static", html=True), name="static")

# --------------------------
# MODELO DE TAREFA / ESTADO
# --------------------------

class TaskStatus(str):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"

class Task(BaseModel):
    id: str
    slug: str
    filename: str
    enqueued_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None

    status: TaskStatus = TaskStatus.QUEUED
    progress: float = 0.0            # 0..100 (quando RUNNING)
    message: str = ""
    # resultado em memória (por simplicidade). TTL curto é recomendado em prod.
    result: Optional[bytes] = None
    error_detail: Optional[str] = None

# --------------------------
# FILA (apenas 1 simultâneo)
# --------------------------
PENDING: Deque[str] = deque()            # IDs aguardando
TASKS: Dict[str, Task] = {}              # id -> Task
QUEUE: "asyncio.Queue[str]" = asyncio.Queue()

# Estimativa de duração média (segundos)
DURATIONS: Deque[float] = deque(maxlen=20)
DEFAULT_AVG = 120.0   # chute inicial (2 min)

# Lock para atualizar progresso/mensagens com segurança
TASK_LOCK = asyncio.Lock()

async def worker():
    """Consome fila e processa 1 tarefa por vez."""
    while True:
        task_id = await QUEUE.get()
        task = TASKS.get(task_id)
        if not task:
            continue

        # Marca início
        async with TASK_LOCK:
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            task.progress = 0.0
            task.message = "Iniciando…"

        t0 = time.time()
        try:
            # Recupera bytes do upload guardados no objeto Task (vamos anexar no enqueue)
            data: bytes = getattr(task, "_upload_bytes", None)
            if not data:
                raise RuntimeError("Arquivo ausente em memória.")

            # Executa pipeline de build com callback de progresso
            async def set_progress(p: float, msg: str = ""):
                async with TASK_LOCK:
                    task.progress = max(0.0, min(100.0, float(p)))
                    if msg:
                        task.message = msg

            zip_bytes = await run_build_pipeline(task.slug, data, set_progress)  # <<< sua lógica aqui

            # Salva resultado
            async with TASK_LOCK:
                task.result = zip_bytes
                task.progress = 100.0
                task.message = "Concluído"
                task.status = TaskStatus.DONE
                task.finished_at = time.time()
                DURATIONS.append(task.finished_at - t0)

        except Exception as e:
            async with TASK_LOCK:
                task.status = TaskStatus.ERROR
                task.error_detail = str(e)
                task.finished_at = time.time()
                task.message = "Erro no processamento"
                DURATIONS.append(task.finished_at - t0)

        finally:
            # Libera referência aos bytes para economizar RAM
            if hasattr(task, "_upload_bytes"):
                delattr(task, "_upload_bytes")

            # Remove da fila pendente
            try:
                PENDING.remove(task.id)
            except ValueError:
                pass

            QUEUE.task_done()

# Inicia o worker ao subir o app
@app.on_event("startup")
async def startup():
    asyncio.create_task(worker())

# ----------------------------------
# PIPELINE: COLOQUE SUA LÓGICA AQUI
# ----------------------------------
async def run_build_pipeline(
    slug: str,
    zip_bytes: bytes,
    progress: Callable[[float, str], "asyncio.Future | None"]
) -> bytes:
    """
    Execute aqui o MESMO processo que você já tinha na /build:
    - unzip
    - detectar framework
    - patch vite/next/cra para base '/<slug>/'
    - forçar HashRouter (quando aplicável)
    - npm install / npm run build (ou usar build prévio)
    - pós-build: sanity fixes e .htaccess
    - zip do conteúdo do build

    Use 'await progress(percent, "mensagem")' entre etapas.
    Retorne os bytes do ZIP final.
    """
    # -------------- EXEMPLO DE ESTRUTURA --------------
    # Você provavelmente já tem funções prontas. Aqui é só
    # um esqueleto de chamadas com progresso.
    await progress(5, "Descompactando ZIP…")
    # unzip_to(tempdir, zip_bytes)

    await progress(15, "Analisando projeto…")
    # fw = detect_framework(tempdir)

    await progress(25, "Aplicando patches…")
    # ensure_vite_config(...) / patch_next_config(...) / patch_cra_homepage(...)
    # ensure_hashrouter(...) / remove_browserrouter_in_app(...)

    await progress(65, "Instalando dependências e build (npm)…")
    # npm_install_and_build(...) -> dist_dir

    await progress(85, "Ajustes finais…")
    # vite_post_build_sanity(dist_dir); sanity_fix_html_css(dist_dir); write_htaccess(dist_dir, slug)

    await progress(95, "Compactando…")
    # zip_bytes = zip_with_perms_to_bytes(dist_dir)

    # ----------- PLACEHOLDER -----------
    # Para a resposta ser auto-contida, retorno algo mínimo.
    # SUBSTITUA pelo seu zip real conforme o comentário acima.
    dummy = io.BytesIO()
    with io.BytesIO(b"fake") as _:
        pass
    await progress(100, "Concluído")
    # Retorne de fato os bytes do ZIP construído:
    return b"FAKE-ZIP-BYTES"  # <<< TROQUE por bytes reais do seu zip
    # -----------------------------------

# ----------------------------------
#   API: ENFILEIRAR / STATUS / BAIXAR
# ----------------------------------

class EnqueueResponse(BaseModel):
    id: str
    status: TaskStatus
    position: int
    eta_seconds: float

def _avg_duration() -> float:
    if DURATIONS:
        return sum(DURATIONS) / len(DURATIONS)
    return DEFAULT_AVG

def _queue_position(task_id: str) -> int:
    """0 = em execução; 1 = primeiro da fila; 2 = segundo…"""
    task = TASKS.get(task_id)
    if not task:
        return -1
    if task.status == TaskStatus.RUNNING:
        return 0
    # contar quantos estão à frente
    try:
        idx = list(PENDING).index(task_id)
    except ValueError:
        idx = 0
    # se há um rodando, todos aumentam posição em +1
    any_running = any(t.status == TaskStatus.RUNNING for t in TASKS.values())
    return idx + (1 if any_running else 0)

def _eta_for(task_id: str) -> float:
    """
    ETA simples:
    - Se RUNNING: baseado no progresso e tempo decorrido
    - Se QUEUED: avg_duration * posição (considerando 1 em execução)
    """
    task = TASKS.get(task_id)
    if not task:
        return 0.0

    avg = _avg_duration()

    if task.status == TaskStatus.RUNNING and task.started_at:
        elapsed = time.time() - task.started_at
        pct = max(0.0, min(99.0, task.progress))
        if pct <= 0.1:
            # sem progresso ainda: chute = avg - elapsed
            return max(5.0, avg - elapsed)
        total_est = elapsed * (100.0 / pct)
        remain = max(1.0, total_est - elapsed)
        return remain

    if task.status == TaskStatus.QUEUED:
        pos = _queue_position(task_id)
        # se alguém está rodando: posição 1 -> aguarda ~avg; posição 2 -> ~2*avg, etc.
        return max(5.0, pos * avg)

    return 0.0

@app.post("/tasks", response_model=EnqueueResponse)
async def enqueue(slug: str = Form(...), file: UploadFile = Form(...)):
    if not slug or not file:
        raise HTTPException(status_code=400, detail="slug e file são obrigatórios")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Arquivo vazio")

    task_id = str(uuid.uuid4())
    task = Task(
        id=task_id,
        slug=slug.strip(),
        filename=file.filename,
        enqueued_at=time.time(),
    )
    # anexa bytes (em memória) só pro worker
    setattr(task, "_upload_bytes", data)

    TASKS[task_id] = task
    PENDING.append(task_id)
    await QUEUE.put(task_id)

    return EnqueueResponse(
        id=task_id,
        status=task.status,
        position=_queue_position(task_id),
        eta_seconds=_eta_for(task_id),
    )

class StatusResponse(BaseModel):
    id: str
    status: TaskStatus
    position: int
    progress: float
    message: str
    eta_seconds: float

@app.get("/tasks/{task_id}/status", response_model=StatusResponse)
async def task_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task não encontrada")
    return StatusResponse(
        id=task.id,
        status=task.status,
        position=_queue_position(task_id),
        progress=task.progress if task.status == TaskStatus.RUNNING else 0.0,
        message=task.message,
        eta_seconds=_eta_for(task_id),
    )

@app.get("/tasks/{task_id}/download")
async def task_download(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task não encontrada")
    if task.status == TaskStatus.ERROR:
        raise HTTPException(status_code=500, detail=task.error_detail or "Erro")
    if task.status != TaskStatus.DONE or not task.result:
        raise HTTPException(status_code=409, detail="Ainda não concluído")
    # envia em memória
    return StreamingResponse(
        io.BytesIO(task.result),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{task.slug}-site.zip"'},
    )
