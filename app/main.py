# app/main.py
import asyncio
import io
import os
import re
import time
import json
import zipfile
import tempfile
import subprocess
import uuid
from enum import Enum
from collections import deque
from typing import Optional, Dict, Deque, Callable
from pathlib import Path

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# =========================
#   APP + FILA (1 por vez)
# =========================
app = FastAPI()

class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"

class Task(BaseModel):
    id: str
    slug: str
    filename: str
    enqueued_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    status: TaskStatus = TaskStatus.queued
    progress: float = 0.0
    message: str = ""
    result: Optional[bytes] = None
    error_detail: Optional[str] = None

PENDING: Deque[str] = deque()
TASKS: Dict[str, Task] = {}
QUEUE: "asyncio.Queue[str]" = asyncio.Queue()
DURATIONS: Deque[float] = deque(maxlen=20)
DEFAULT_AVG = 180.0  # 3 min (chute inicial para ETA)
TASK_LOCK = asyncio.Lock()

def _now() -> float:
    return time.time()

def _avg_duration() -> float:
    return sum(DURATIONS)/len(DURATIONS) if DURATIONS else DEFAULT_AVG

def _queue_position(task_id: str) -> int:
    task = TASKS.get(task_id)
    if not task:
        return -1
    if task.status == TaskStatus.running:
        return 0
    try:
        idx = list(PENDING).index(task_id)
    except ValueError:
        idx = 0
    any_running = any(t.status == TaskStatus.running for t in TASKS.values())
    return idx + (1 if any_running else 0)

def _eta_for(task_id: str) -> float:
    task = TASKS.get(task_id)
    if not task:
        return 0.0
    avg = _avg_duration()
    if task.status == TaskStatus.running and task.started_at:
        elapsed = _now() - task.started_at
        pct = max(0.0, min(99.0, task.progress))
        if pct <= 0.1:
            return max(5.0, avg - elapsed)
        total_est = elapsed * (100.0 / pct)
        return max(1.0, total_est - elapsed)
    if task.status == TaskStatus.queued:
        pos = _queue_position(task_id)
        return max(5.0, pos * avg)
    return 0.0

async def worker():
    while True:
        task_id = await QUEUE.get()
        task = TASKS.get(task_id)
        if not task:
            QUEUE.task_done()
            continue
        async with TASK_LOCK:
            task.status = TaskStatus.running
            task.started_at = _now()
            task.progress = 0.0
            task.message = "Iniciando…"
        t0 = _now()
        try:
            data: bytes = getattr(task, "_upload_bytes", None)
            if not data:
                raise RuntimeError("Arquivo ausente.")
            async def set_progress(p: float, msg: str = ""):
                async with TASK_LOCK:
                    task.progress = max(0.0, min(100.0, float(p)))
                    if msg:
                        task.message = msg
            zip_bytes = await run_build_pipeline(task.slug, data, set_progress)
            async with TASK_LOCK:
                task.result = zip_bytes
                task.progress = 100.0
                task.message = "Concluído"
                task.status = TaskStatus.done
                task.finished_at = _now()
                DURATIONS.append(task.finished_at - t0)
        except Exception as e:
            async with TASK_LOCK:
                task.status = TaskStatus.error
                task.error_detail = str(e)
                task.finished_at = _now()
                task.message = "Erro no processamento"
                DURATIONS.append(task.finished_at - t0)
        finally:
            if hasattr(task, "_upload_bytes"):
                delattr(task, "_upload_bytes")
            try:
                PENDING.remove(task.id)
            except ValueError:
                pass
            QUEUE.task_done()

@app.on_event("startup")
async def startup():
    asyncio.create_task(worker())

# =======================================
#   PIPELINE COMPLETA (LOVABLE → site.zip)
# =======================================

def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _has_dep(pkg: str, pkgjson: dict) -> bool:
    for key in ("dependencies","devDependencies","peerDependencies"):
        d = pkgjson.get(key, {})
        if pkg in d:
            return True
    return False

def _find_project_root(base: Path) -> Path:
    candidates = []
    for p in base.rglob("package.json"):
        if "node_modules" in p.parts:
            continue
        candidates.append(p.parent)
    if not candidates:
        raise FileNotFoundError("Não encontrei package.json no ZIP.")
    def score(dirp: Path):
        s = 0
        if any((dirp / f).exists() for f in ["vite.config.ts","vite.config.js","next.config.js","next.config.mjs"]):
            s += 10
        s -= len(dirp.parts)
        return s
    candidates.sort(key=score, reverse=True)
    return candidates[0]

def _detect_framework(project_root: Path) -> str:
    pkg = _read_json(project_root / "package.json")
    if (project_root / "vite.config.ts").exists() or (project_root / "vite.config.js").exists() or _has_dep("vite", pkg):
        return "vite"
    if _has_dep("next", pkg) or (project_root / "next.config.js").exists() or (project_root / "next.config.mjs").exists():
        return "next"
    if _has_dep("react-scripts", pkg):
        return "cra"
    return "unknown"

def _patch_file_text(path: Path, transform, desc: str):
    if not path.exists(): return False
    try:
        txt = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        txt = path.read_text(encoding="latin-1")
    new = transform(txt)
    if new != txt:
        path.write_text(new, encoding="utf-8")
        # print(f"[patch] {desc}: {path.name}")
        return True
    return False

def _ensure_vite_config(project_root: Path, slug: str):
    pkg = _read_json(project_root / "package.json")
    use_ts = (project_root / "tsconfig.json").exists() or any((project_root/"src").glob("**/*.ts*"))
    fname = "vite.config.ts" if use_ts else "vite.config.js"
    p = project_root / fname
    plugin_line = ""
    imports = ""
    if _has_dep("@vitejs/plugin-react", pkg):
        plugin_line = "  plugins: [react()],\n"
        imports = "import react from '@vitejs/plugin-react'\n"
    if not p.exists():
        content = (
            "import { defineConfig } from 'vite'\n"
            f"{imports}"
            "\n"
            "export default defineConfig({\n"
            f"{plugin_line}"
            f"  base: '/{slug}/',\n"
            "})\n"
        )
        p.write_text(content, encoding="utf-8")
        return True
    desired = f'"/{slug}/"'
    def transform(txt: str) -> str:
        out, n = re.subn(r'base\s*:\s*["\']\/[^"\']*\/["\']', f'base: {desired}', txt)
        if n == 0:
            out = re.sub(r'(defineConfig\(\s*(?:\(\s*\w+\s*\)\s*=>\s*)?\{\s*)',
                         r'\1base: ' + desired + ', ',
                         txt, count=1)
        return out
    return _patch_file_text(p, transform, f'Vite base -> /{slug}/')

def _vite_post_build_sanity(dist_dir: Path):
    idx = dist_dir / "index.html"
    if not idx.exists():
        return
    html = idx.read_text(encoding="utf-8", errors="ignore")
    new = html.replace('href="/assets/', 'href="assets/').replace('src="/assets/', 'src="assets/')
    if new != html:
        idx.write_text(new, encoding="utf-8")

SAFE_PREFIXES = ("http://","https://","//","data:","mailto:","tel:")
def _is_safe_url(u: str) -> bool:
    tu = (u or "").strip().lower()
    return tu.startswith(SAFE_PREFIXES)
def _strip_leading_slash_if_local(u: str) -> str:
    if not u: return u
    if _is_safe_url(u): return u
    if u.startswith("/"): return u.lstrip("/")
    return u
def _sanity_fix_html_css(dist_dir: Path):
    # .html
    for htmlp in dist_dir.rglob("*.html"):
        s = htmlp.read_text(encoding="utf-8", errors="ignore")
        def repl_attr(m):
            attr, quote, url, quote2 = m.group(1), m.group(2), m.group(3), m.group(4)
            fixed = _strip_leading_slash_if_local(url)
            return f'{attr}={quote}{fixed}{quote2}'
        s2 = re.sub(r'(src|href)\s*=\s*(")([^"]+)(")', repl_attr, s, flags=re.I)
        s2 = re.sub(r'(src|href)\s*=\s*(\')([^\']+)(\')', repl_attr, s2, flags=re.I)
        if s2 != s:
            htmlp.write_text(s2, encoding="utf-8")
    # .css url(...)
    for cssp in dist_dir.rglob("*.css"):
        s = cssp.read_text(encoding="utf-8", errors="ignore")
        def repl_url(m):
            inner = m.group(1).strip().strip('"').strip("'")
            fixed = _strip_leading_slash_if_local(inner)
            if '"' in m.group(1):   return f'url("{fixed}")'
            if "'" in m.group(1):   return f"url('{fixed}')"
            return f'url({fixed})'
        s2 = re.sub(r'url\(([^)]+)\)', repl_url, s, flags=re.I)
        if s2 != s:
            cssp.write_text(s2, encoding="utf-8")

def _patch_next_config(project_root: Path, slug: str):
    for fname in ("next.config.js","next.config.mjs"):
        p = project_root / fname
        if not p.exists(): continue
        def transform(txt: str) -> str:
            if "basePath" in txt or "assetPrefix" in txt:
                t = re.sub(r'basePath\s*:\s*["\'][^"\']*["\']', f'basePath: "/{slug}"', txt)
                t = re.sub(r'assetPrefix\s*:\s*["\'][^"\']*["\']', f'assetPrefix: "/{slug}/"', t)
                return t
            t = re.sub(r'(module\.exports\s*=\s*\{)', r'\1\n  basePath: "/%s",\n  assetPrefix: "/%s/",' % (slug, slug), txt, count=1)
            t = re.sub(r'(export\s+default\s*\{)', r'\1\n  basePath: "/%s",\n  assetPrefix: "/%s/",' % (slug, slug), t, count=1)
            return t
        if _patch_file_text(p, transform, f'Next basePath/assetPrefix -> /{slug}'):
            pkg_path = project_root / "package.json"
            pkg = _read_json(pkg_path)
            scripts = pkg.get("scripts", {})
            scripts["export"] = "next build && next export -o dist"
            pkg["scripts"] = scripts
            pkg_path.write_text(json.dumps(pkg, indent=2, ensure_ascii=False))
            return True
    return False

def _patch_cra_homepage(project_root: Path, slug: str):
    pkg_path = project_root / "package.json"
    pkg = _read_json(pkg_path)
    old = pkg.get("homepage", "")
    desired = f"/{slug}"
    if old != desired:
        pkg["homepage"] = desired
        pkg_path.write_text(json.dumps(pkg, indent=2, ensure_ascii=False))
        return True
    return False

def _locate_src_dir(project_root: Path) -> Path:
    for cand in ["src","app","frontend/src"]:
        p = project_root / cand
        if p.exists(): return p
    return project_root / "src"

def _ensure_hashrouter(src_dir: Path):
    for name in ["main.tsx","main.jsx","index.tsx","index.jsx","main.ts","main.js","index.ts","index.js"]:
        p = src_dir / name
        if p.exists():
            target = p; break
    else:
        return False
    def transform(txt: str) -> str:
        t = txt
        if re.search(r'from\s+[\'"]react-router-dom[\'"]', t) and "HashRouter" not in t:
            t = re.sub(r'import\s*{\s*', 'import { HashRouter, ', t, count=1)
        elif "react-router-dom" not in t:
            t = re.sub(r'(^\s*import[^\n]*\n)', r'\1import { HashRouter } from "react-router-dom";\n', t, count=1, flags=re.M)
        t = t.replace("BrowserRouter", "HashRouter")
        t = re.sub(r'(<App\s*/>)', r'<HashRouter>\1</HashRouter>', t)
        t = re.sub(r'(<App\s*>\s*</App\s*>)', r'<HashRouter>\1</HashRouter>', t)
        return t
    return _patch_file_text(target, transform, "usar HashRouter no main")

def _remove_browserrouter_in_app(src_dir: Path):
    for name in ["App.tsx","App.jsx","App.ts","App.js"]:
        p = src_dir / name
        if not p.exists(): continue
        def transform(txt: str) -> str:
            t = txt
            t = re.sub(r'import\s*{\s*BrowserRouter\s*(?:,\s*)?', 'import { ', t)
            t = re.sub(r',\s*BrowserRouter\s*}', '}', t)
            t = re.sub(r'import\s+BrowserRouter\s+from\s+[\'"]react-router-dom[\'"]\s*;?\s*', '', t)
            t = re.sub(r'<\s*BrowserRouter[^>]*>', '<>', t)
            t = re.sub(r'</\s*BrowserRouter\s*>', '</>', t)
            return t
        if _patch_file_text(p, transform, "remover BrowserRouter do App"):
            return True
    return False

def _write_htaccess(target_dir: Path, slug: str):
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

def _which_npm_cmd() -> str:
    # Em Linux (Render) é "npm"
    return "npm"

def _run_cmd(cmd, cwd: Path):
    subprocess.check_call(cmd, cwd=str(cwd))

def _npm_build(project_root: Path, framework: str) -> str:
    npm_cmd = _which_npm_cmd()
    pkg_lock = project_root / "package-lock.json"
    if framework == "next":
        _run_cmd([npm_cmd,"install"], project_root)
        _run_cmd([npm_cmd,"run","export"], project_root)
        return "dist"
    if pkg_lock.exists():
        try:
            _run_cmd([npm_cmd,"ci"], project_root)
        except subprocess.CalledProcessError:
            _run_cmd([npm_cmd,"install"], project_root)
    else:
        _run_cmd([npm_cmd,"install"], project_root)
    _run_cmd([npm_cmd,"run","build"], project_root)
    for cand in ["dist","build","out"]:
        if (project_root / cand).exists():
            return cand
    return "dist"

def _zip_dir_to_bytes(src_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # add dirs first (perm 0755)
        for d in sorted([p for p in src_dir.rglob("*") if p.is_dir()]):
            rel = d.relative_to(src_dir).as_posix()
            if not rel.endswith("/"): rel += "/"
            zi = zipfile.ZipInfo(rel); zi.external_attr = (0o755 & 0xFFFF) << 16
            z.writestr(zi, b"")
        # add files (perm 0644)
        for f in src_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src_dir).as_posix()
                zi = zipfile.ZipInfo(rel); zi.external_attr = (0o644 & 0xFFFF) << 16
                with open(f, "rb") as fh:
                    z.writestr(zi, fh.read())
    buf.seek(0)
    return buf.read()

async def run_build_pipeline(
    slug: str,
    zip_bytes: bytes,
    progress: Callable[[float, str], "asyncio.Future | None"]
) -> bytes:
    # 1) Extrair o ZIP enviado
    await progress(5, "Descompactando ZIP…")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        src_dir = tmpdir / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as z:
            z.extractall(src_dir)

        # 2) Encontrar raiz do projeto (onde está o package.json)
        await progress(15, "Localizando projeto…")
        project_root = _find_project_root(src_dir)

        # 3) Detectar framework
        await progress(20, "Detectando framework…")
        fw = _detect_framework(project_root)

        # 4) Ajustar configs de base/path
        await progress(35, "Aplicando ajustes…")
        if fw == "vite":
            _ensure_vite_config(project_root, slug)
        elif fw == "next":
            _patch_next_config(project_root, slug)
        elif fw == "cra":
            _patch_cra_homepage(project_root, slug)
        # Router (SPA)
        sdir = _locate_src_dir(project_root)
        if sdir.exists():
            _ensure_hashrouter(sdir)
            _remove_browserrouter_in_app(sdir)

        # 5) Build (npm install + build)
        await progress(65, "Instalando dependências e build… (pode demorar)")
        dist_name = _npm_build(project_root, fw)
        dist_dir = project_root / dist_name

        # 6) Pós-build: correções e .htaccess
        await progress(85, "Ajustes finais…")
        if fw == "vite":
            _vite_post_build_sanity(dist_dir)
        _sanity_fix_html_css(dist_dir)
        _write_htaccess(dist_dir, slug)

        # 7) Zipar conteúdo do build e devolver bytes
        await progress(95, "Compactando…")
        out_bytes = _zip_dir_to_bytes(dist_dir)

    await progress(100, "Concluído")
    return out_bytes

# =========================
#   API: fila + status
# =========================
class EnqueueResponse(BaseModel):
    id: str
    status: TaskStatus
    position: int
    eta_seconds: float

class StatusResponse(BaseModel):
    id: str
    status: TaskStatus
    position: int
    progress: float
    message: str
    eta_seconds: float

@app.post("/tasks", response_model=EnqueueResponse)
async def enqueue(slug: str = Form(...), file: UploadFile = Form(...)):
    if not slug or not file:
        raise HTTPException(status_code=400, detail="slug e file são obrigatórios")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Arquivo vazio")
    task_id = str(uuid.uuid4())
    task = Task(id=task_id, slug=slug.strip(), filename=file.filename, enqueued_at=_now())
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

@app.get("/tasks/{task_id}/status", response_model=StatusResponse)
async def task_status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task não encontrada")
    return StatusResponse(
        id=task.id,
        status=task.status,
        position=_queue_position(task_id),
        progress=task.progress if task.status == TaskStatus.running else 0.0,
        message=task.message,
        eta_seconds=_eta_for(task_id),
    )

@app.get("/tasks/{task_id}/download")
async def task_download(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task não encontrada")
    if task.status == TaskStatus.error:
        raise HTTPException(status_code=500, detail=task.error_detail or "Erro")
    if task.status != TaskStatus.done or not task.result:
        raise HTTPException(status_code=409, detail="Ainda não concluído")
    return StreamingResponse(
        io.BytesIO(task.result),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{task.slug}-site.zip"'},
    )

# =========================
#   FRONT-END em /ui
# =========================
@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/ui/")

app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")
