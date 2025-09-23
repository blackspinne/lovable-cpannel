import os
import io
import re
import sys
import uuid
import json
import time
import shutil
import zipfile
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Literal, Dict, Any

from fastapi import FastAPI, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ------------------------------------------------------------
# Configuração
# ------------------------------------------------------------
PORT = int(os.getenv("PORT", "8080"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))  # limite mostrado na UI
JOBS_ROOT = Path("/tmp/jobs")           # onde guardamos cada job
JOBS_ROOT.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------
# Estados do Job (strings simples para evitar erro de schema)
# ------------------------------------------------------------
TaskState = Literal["queued", "working", "done", "error"]

class Task(BaseModel):
    id: str
    slug: str
    state: TaskState
    progress: int = 0           # 0..100
    eta_seconds: Optional[int] = None
    message: Optional[str] = None
    download_url: Optional[str] = None

# memória: fila e mapa de tarefas
TASKS: Dict[str, Task] = {}
QUEUE: "asyncio.Queue[str]" = asyncio.Queue()
WORKER_RUNNING = False

app = FastAPI()

# servir a UI
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")

@app.get("/")
def root():
    return RedirectResponse("/ui/")

# ------------------------------------------------------------
# Utilidades de build (Lovable -> cPanel)
# ------------------------------------------------------------
def unzip_all(src_zip: Path, dest_dir: Path):
    with zipfile.ZipFile(src_zip, 'r') as z:
        z.extractall(dest_dir)

def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def has_dep(pkg: str, pkgjson: dict) -> bool:
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        d = pkgjson.get(key, {})
        if pkg in d:
            return True
    return False

def find_project_root(base: Path) -> Path:
    cands = []
    for p in base.rglob("package.json"):
        if "node_modules" in p.parts:
            continue
        cands.append(p.parent)
    if not cands:
        raise FileNotFoundError("Não encontrei package.json no ZIP enviado.")
    # escolher a pasta mais “raiz” que tenha vite/next config
    def score(d: Path):
        s = 0
        if any((d/f).exists() for f in ["vite.config.ts","vite.config.js","next.config.js","next.config.mjs"]):
            s += 10
        s -= len(d.parts)
        return s
    cands.sort(key=score, reverse=True)
    return cands[0]

def detect_framework(project_root: Path) -> str:
    pkg = read_json(project_root / "package.json")
    if (project_root / "vite.config.ts").exists() or (project_root / "vite.config.js").exists() or has_dep("vite", pkg):
        return "vite"
    if has_dep("next", pkg) or (project_root / "next.config.js").exists() or (project_root / "next.config.mjs").exists():
        return "next"
    if has_dep("react-scripts", pkg):
        return "cra"
    return "unknown"

def patch_file_text(path: Path, transform, desc: str):
    if not path.exists():
        return False
    try:
        txt = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        txt = path.read_text(encoding="latin-1")
    new = transform(txt)
    if new != txt:
        path.write_text(new, encoding="utf-8")
        return True
    return False

def ensure_vite_config(project_root: Path, slug: str):
    pkg = read_json(project_root / "package.json")
    use_ts = (project_root / "tsconfig.json").exists() or any((project_root/"src").glob("**/*.ts*"))
    fname = "vite.config.ts" if use_ts else "vite.config.js"
    p = project_root / fname
    plugin_line = ""
    imports = ""
    if has_dep("@vitejs/plugin-react", pkg):
        plugin_line = "  plugins: [react()],\n"
        imports = "import react from '@vitejs/plugin-react'\n"
    if not p.exists():
        content = (
            "import { defineConfig } from 'vite'\n"
            f"{imports}\n"
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
    return patch_file_text(p, transform, "Vite base")

def patch_next(project_root: Path, slug: str):
    for fname in ("next.config.js","next.config.mjs"):
        p = project_root / fname
        if p.exists():
            def t(txt: str) -> str:
                if "basePath" in txt or "assetPrefix" in txt:
                    t1 = re.sub(r'basePath\s*:\s*["\'][^"\']*["\']', f'basePath: "/{slug}"', txt)
                    t1 = re.sub(r'assetPrefix\s*:\s*["\'][^"\']*["\']', f'assetPrefix: "/{slug}/"', t1)
                    return t1
                t1 = re.sub(r'(module\.exports\s*=\s*\{)', r'\1\n  basePath: "/%s",\n  assetPrefix: "/%s/",' % (slug, slug), txt, count=1)
                t1 = re.sub(r'(export\s+default\s*\{)', r'\1\n  basePath: "/%s",\n  assetPrefix: "/%s/",' % (slug, slug), t1, count=1)
                return t1
            patch_file_text(p, t, "Next basePath/assetPrefix")
            # garantir export para pasta dist
            pkg_path = project_root / "package.json"
            pkg = read_json(pkg_path)
            scripts = pkg.get("scripts", {})
            scripts["export"] = "next build && next export -o dist"
            pkg["scripts"] = scripts
            pkg_path.write_text(json.dumps(pkg, indent=2, ensure_ascii=False))
            return True
    return False

def patch_cra_homepage(project_root: Path, slug: str):
    pkg_path = project_root / "package.json"
    pkg = read_json(pkg_path)
    if pkg.get("homepage") != f"/{slug}":
        pkg["homepage"] = f"/{slug}"
        pkg_path.write_text(json.dumps(pkg, indent=2, ensure_ascii=False))
        return True
    return False

def ensure_hashrouter(src_dir: Path):
    for name in ["main.tsx","main.jsx","index.tsx","index.jsx","main.ts","main.js","index.ts","index.js"]:
        p = src_dir / name
        if p.exists():
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
            patch_file_text(p, transform, "HashRouter")
            return True
    return False

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

def which_npm() -> str:
    return "npm"  # no container linux

def run_cmd(cmd, cwd: Path):
    subprocess.check_call(cmd, cwd=str(cwd))

def npm_build(project_root: Path, framework: str) -> Path:
    npm = which_npm()
    pkg_lock = project_root / "package-lock.json"
    if pkg_lock.exists():
        try:
            run_cmd([npm, "ci"], project_root)
        except subprocess.CalledProcessError:
            run_cmd([npm, "install"], project_root)
    else:
        run_cmd([npm, "install"], project_root)

    if framework == "next":
        run_cmd([npm, "run", "export"], project_root)
        out = project_root / "dist"
    else:
        run_cmd([npm, "run", "build"], project_root)
        out = None
        for cand in ["dist","build","out"]:
            if (project_root / cand).exists():
                out = project_root / cand
                break
        if out is None:
            raise RuntimeError("Build não gerou pasta dist/build/out.")
    return out

def sanity_html_css(dist_dir: Path):
    # remove / inicial em src/href/url(...)
    for htmlp in dist_dir.rglob("*.html"):
        s = htmlp.read_text(encoding="utf-8", errors="ignore")
        def repl_attr(m):
            attr, q, url = m.group(1), m.group(2), m.group(3)
            if url and not re.match(r'^(https?:)?//|data:|mailto:|tel:', url) and url.startswith("/"):
                url = url.lstrip("/")
            return f'{attr}={q}{url}{q}'
        s2 = re.sub(r'(src|href)\s*=\s*(")([^"]+)(")', repl_attr, s, flags=re.I)
        s2 = re.sub(r'(src|href)\s*=\s*(\')([^\']+)(\')', repl_attr, s2, flags=re.I)
        if s2 != s:
            htmlp.write_text(s2, encoding="utf-8")
    for cssp in dist_dir.rglob("*.css"):
        s = cssp.read_text(encoding="utf-8", errors="ignore")
        def repl_url(m):
            inner = m.group(1).strip().strip('"').strip("'")
            if inner and not re.match(r'^(https?:)?//|data:', inner) and inner.startswith("/"):
                inner = inner.lstrip("/")
            if '"' in m.group(1):
                return f'url("{inner}")'
            if "'" in m.group(1):
                return f"url('{inner}')"
            return f'url({inner})'
        s2 = re.sub(r'url\(([^)]+)\)', repl_url, s, flags=re.I)
        if s2 != s:
            cssp.write_text(s2, encoding="utf-8")

def zip_with_perms(src_dir: Path, out_zip: Path):
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # garantir entradas de diretório (0755)
        for d in sorted([p for p in src_dir.rglob("*") if p.is_dir()]):
            rel = d.relative_to(src_dir).as_posix().rstrip("/") + "/"
            zi = zipfile.ZipInfo(rel)
            zi.external_attr = (0o755 & 0xFFFF) << 16
            z.writestr(zi, b"")
        # arquivos (0644)
        for f in src_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src_dir).as_posix()
                zi = zipfile.ZipInfo(rel)
                zi.external_attr = (0o644 & 0xFFFF) << 16
                with open(f, "rb") as fh:
                    z.writestr(zi, fh.read())

# ------------------------------------------------------------
# Conversão principal (um job)
# ------------------------------------------------------------
def convert_lovable_zip(input_zip: Path, slug: str, work_dir: Path) -> Path:
    """
    Recebe o ZIP do Lovable, faz patch/build e retorna caminho para site.zip
    Tudo que for temporário fica dentro de work_dir.
    """
    src_dir = work_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    unzip_all(input_zip, src_dir)

    project_root = find_project_root(src_dir)
    fw = detect_framework(project_root)

    # patches
    if fw == "vite":
        ensure_vite_config(project_root, slug)
    elif fw == "next":
        patch_next(project_root, slug)
    elif fw == "cra":
        patch_cra_homepage(project_root, slug)

    # Ajustes de roteamento (HashRouter)
    src_folder = None
    for cand in ["src","app","frontend/src"]:
        p = project_root / cand
        if p.exists():
            src_folder = p
            break
    if src_folder:
        ensure_hashrouter(src_folder)

    # build
    dist_dir = npm_build(project_root, fw)

    # pós-build
    sanity_html_css(dist_dir)
    write_htaccess(dist_dir, slug)

    # zip final
    out_zip = work_dir / "site.zip"
    zip_with_perms(dist_dir, out_zip)
    return out_zip

# ------------------------------------------------------------
# Worker: processa a fila
# ------------------------------------------------------------
async def worker_loop():
    global WORKER_RUNNING
    WORKER_RUNNING = True
    try:
        while True:
            task_id = await QUEUE.get()
            task = TASKS.get(task_id)
            if not task:
                QUEUE.task_done()
                continue

            job_dir = JOBS_ROOT / task_id
            input_zip = job_dir / "input.zip"
            out_zip  = job_dir / "site.zip"

            try:
                task.state = "working"
                task.progress = 10
                task.message = "Validando e preparando projeto..."

                # processar
                t0 = time.time()
                with tempfile.TemporaryDirectory(dir=job_dir) as tmp:
                    tmp_path = Path(tmp)
                    task.progress = 30
                    task.message = "Aplicando ajustes (slug/roteamento)..."

                    # conversão
                    result_zip = convert_lovable_zip(input_zip, task.slug, tmp_path)

                    task.progress = 80
                    task.message  = "Gerando site.zip..."

                    # mover para pasta persistente do job
                    out_zip.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(result_zip), str(out_zip))

                task.progress = 100
                task.state = "done"
                task.message = f"Pronto! ({int(out_zip.stat().st_size/1024)} KB)"
                task.download_url = f"/download/{task_id}"
                task.eta_seconds = 0

            except subprocess.CalledProcessError as e:
                task.state = "error"
                task.message = f"Falha no build (npm). Saída: {e}"
                task.progress = 100
            except Exception as e:
                task.state = "error"
                task.message = f"{type(e).__name__}: {e}"
                task.progress = 100

            QUEUE.task_done()
    finally:
        WORKER_RUNNING = False

# inicializa um worker
@app.on_event("startup")
async def _startup():
    asyncio.create_task(worker_loop())

# ------------------------------------------------------------
# API
# ------------------------------------------------------------
@app.post("/tasks")
async def enqueue(slug: str = Form(...), file: UploadFile = Form(...)):
    # tamanho (se o deploy expuser content-length)
    if file.size and file.size > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Arquivo maior que {MAX_UPLOAD_MB} MB")

    task_id = str(uuid.uuid4())
    job_dir = JOBS_ROOT / task_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # salvar upload
    input_zip = job_dir / "input.zip"
    with open(input_zip, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)

    # cria task e enfileira
    task = Task(id=task_id, slug=slug.strip(), state="queued", progress=0, eta_seconds=None)
    TASKS[task_id] = task
    await QUEUE.put(task_id)

    return {"task_id": task_id}

@app.get("/tasks/{task_id}")
async def status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "Task não encontrada")
    # ETL simplificado: se working, chute proporcional
    if task.state == "queued":
        position = 0
        try:
            # contar quantos na fila à frente (estimativa simples)
            position = max(0, QUEUE.qsize() - 1)
        except Exception:
            pass
        est = 60 * (position + 1)
        task.eta_seconds = est
    elif task.state == "working" and (task.eta_seconds is None or task.eta_seconds > 10):
        task.eta_seconds = max(10, int((100 - task.progress) * 1.2))
    return JSONResponse(task.model_dump())

@app.get("/download/{task_id}")
async def download(task_id: str):
    job_dir = JOBS_ROOT / task_id
    out_zip = job_dir / "site.zip"
    if not out_zip.exists():
        raise HTTPException(404, "site.zip não encontrado (o job terminou com erro ou foi limpo).")
    filename = "site.zip"
    return FileResponse(path=str(out_zip), media_type="application/zip", filename=filename)

# ------------------------------------------------------------
# Uvicorn em container
# ------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT)
