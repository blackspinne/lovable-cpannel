import os
import io
import re
import uuid
import json
import shutil
import zipfile
import asyncio
import tempfile
import subprocess
from pathlib import Path
from typing import Optional, Literal, Dict

from fastapi import FastAPI, UploadFile, Form, File, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ----------------- CONFIG -----------------
PORT = int(os.getenv("PORT", "8080"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "100"))

JOBS_ROOT = Path("/tmp/jobs")
JOBS_ROOT.mkdir(parents=True, exist_ok=True)

TaskState = Literal["queued", "working", "done", "error"]

class Task(BaseModel):
    id: str
    slug: str
    state: TaskState
    progress: int = 0
    eta_seconds: Optional[int] = None
    message: Optional[str] = None
    download_url: Optional[str] = None

TASKS: Dict[str, Task] = {}
QUEUE: "asyncio.Queue[str]" = asyncio.Queue()

app = FastAPI()
app.mount("/ui", StaticFiles(directory="static", html=True), name="ui")


@app.get("/")
def root():
    return RedirectResponse("/ui/")

# ----------------- UTILS -----------------
import json as _json

def read_json(p: Path) -> dict:
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def unzip_all(src_zip: Path, dest_dir: Path):
    with zipfile.ZipFile(src_zip, "r") as z:
        z.extractall(dest_dir)

def has_dep(pkg: str, pkgjson: dict) -> bool:
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        if pkg in pkgjson.get(key, {}):
            return True
    return False

def find_project_root(base: Path) -> Path:
    cands = [p.parent for p in base.rglob("package.json") if "node_modules" not in p.parts]
    if not cands:
        raise FileNotFoundError("Não encontrei package.json no ZIP enviado.")
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

def patch_file_text(path: Path, transform):
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
    imports = "import react from '@vitejs/plugin-react'\n" if has_dep("@vitejs/plugin-react", pkg) else ""
    plugin = "  plugins: [react()],\n" if imports else ""
    if not p.exists():
        p.write_text(
            "import { defineConfig } from 'vite'\n"
            f"{imports}\n"
            "export default defineConfig({\n"
            f"{plugin}"
            f"  base: '/{slug}/',\n"
            "})\n",
            encoding="utf-8",
        )
        return True

    desired = f'"/{slug}/"'
    def transform(txt: str) -> str:
        out, n = re.subn(r'base\s*:\s*["\']\/[^"\']*\/["\']', f'base: {desired}', txt)
        if n == 0:
            out = re.sub(r'(defineConfig\(\s*(?:\(\s*\w+\s*\)\s*=>\s*)?\{\s*)',
                         r'\1base: ' + desired + ', ', txt, count=1)
        return out
    return patch_file_text(p, transform)

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
            patch_file_text(p, t)
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
            patch_file_text(p, transform)
            return True
    return False

def write_htaccess(target_dir: Path, slug: str):
    rules = (
        "DirectoryIndex index.html\n"
        "RewriteEngine On\n"
        f"RewriteBase /{slug}/\n\n"
        "RewriteCond %{REQUEST_FILENAME} -f [OR]\n"
        "RewriteCond %{REQUEST_FILENAME} -d\n"
        "RewriteRule ^ - [L]\n\n"
        "RewriteRule . index.html [L]\n"
    )
    (target_dir / ".htaccess").write_text(rules, encoding="utf-8")

def run_cmd(cmd, cwd: Path):
    subprocess.check_call(cmd, cwd=str(cwd))

def npm_build(project_root: Path, framework: str) -> Path:
    npm = "npm"
    if (project_root / "package-lock.json").exists():
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

def _normalize_url(url: str) -> str:
    """
    - Mantém absolutos válidos (http, data, mailto, tel).
    - Converte '/<qualquerCoisa>/assets/...' -> 'assets/...'
    - Converte '/<qualquerCoisa>/(favicon.ico|robots.txt|manifest.json...)' -> arquivo relativo
    - Remove uma única '/' inicial restante.
    """
    if re.match(r'^(https?:)?//|data:|mailto:|tel:', url or ""):
        return url
    if not url:
        return url

    # /AAA/assets/xxx -> assets/xxx
    m = re.match(r"^/[^/]+/(assets/.*)$", url)
    if m:
        return m.group(1)

    # /AAA/favicon.ico -> favicon.ico (idem p/ manifest e similares)
    m = re.match(r"^/[^/]+/((?:favicon\.ico|robots\.txt|site\.webmanifest|manifest\.json).*)$", url)
    if m:
        return m.group(1)

    # /AAA/index.html -> index.html (fallback seguro)
    m = re.match(r"^/[^/]+/(.*)$", url)
    if m:
        return m.group(1)

    # apenas remove uma / inicial se sobrar
    if url.startswith("/"):
        return url.lstrip("/")

    return url

def sanity_html_css(dist_dir: Path):
    # HTML (src/href)
    for htmlp in dist_dir.rglob("*.html"):
        s = htmlp.read_text(encoding="utf-8", errors="ignore")

        def repl_attr(m):
            attr, q, url = m.group(1), m.group(2), m.group(3)
            new_url = _normalize_url(url)
            return f'{attr}={q}{new_url}{q}'

        s2 = re.sub(r'(src|href)\s*=\s*(")([^"]+)(")', repl_attr, s, flags=re.I)
        s2 = re.sub(r'(src|href)\s*=\s*(\')([^\']+)(\')', repl_attr, s2, flags=re.I)
        if s2 != s:
            htmlp.write_text(s2, encoding="utf-8")

    # CSS url(...)
    for cssp in dist_dir.rglob("*.css"):
        s = cssp.read_text(encoding="utf-8", errors="ignore")

        def repl_url(m):
            inner_raw = m.group(1)
            inner = inner_raw.strip().strip('"').strip("'")
            new_inner = _normalize_url(inner)
            # preserve aspas
            if '"' in inner_raw:
                return f'url("{new_inner}")'
            if "'" in inner_raw:
                return f"url('{new_inner}')"
            return f'url({new_inner})'

        s2 = re.sub(r'url\(([^)]+)\)', repl_url, s, flags=re.I)
        if s2 != s:
            cssp.write_text(s2, encoding="utf-8")

def zip_with_perms(src_dir: Path, out_zip: Path):
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # pastas
        for d in sorted([p for p in src_dir.rglob("*") if p.is_dir()]):
            rel = d.relative_to(src_dir).as_posix().rstrip("/") + "/"
            zi = zipfile.ZipInfo(rel)
            zi.external_attr = (0o755 & 0xFFFF) << 16
            z.writestr(zi, b"")
        # arquivos
        for f in src_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(src_dir).as_posix()
                zi = zipfile.ZipInfo(rel)
                zi.external_attr = (0o644 & 0xFFFF) << 16
                with open(f, "rb") as fh:
                    z.writestr(zi, fh.read())

def convert_lovable_zip(input_zip: Path, slug: str, work_dir: Path) -> Path:
    src_dir = work_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    unzip_all(input_zip, src_dir)

    # Tipo 1: projeto (tem package.json) -> rodar build e aplicar patches
    # Tipo 2: já é dist estático -> só sanitizar caminhos
    try:
        project_root = find_project_root(src_dir)
        fw = detect_framework(project_root)
        if fw == "vite":
            ensure_vite_config(project_root, slug)
        elif fw == "next":
            patch_next(project_root, slug)
        elif fw == "cra":
            patch_cra_homepage(project_root, slug)

        # garantir HashRouter em apps com react-router
        for cand in ["src","app","frontend/src"]:
            p = project_root / cand
            if p.exists():
                ensure_hashrouter(p)
                break

        dist_dir = npm_build(project_root, fw)

    except FileNotFoundError:
        # Não há package.json: assumir que já veio "dist".
        # Usar o próprio src_dir como "dist_dir"
        dist_dir = src_dir

    # Normaliza caminhos e escreve .htaccess
    sanity_html_css(dist_dir)
    write_htaccess(dist_dir, slug)

    out_zip = work_dir / "site.zip"
    zip_with_perms(dist_dir, out_zip)
    return out_zip

# ----------------- WORKER -----------------
async def worker_loop():
    while True:
        task_id = await QUEUE.get()
        task = TASKS.get(task_id)
        if not task:
            QUEUE.task_done()
            continue

        job_dir = JOBS_ROOT / task_id
        input_zip = job_dir / "input.zip"
        out_zip = job_dir / "site.zip"

        try:
            task.state = "working"; task.progress = 10; task.message = "Validando e preparando projeto..."
            await asyncio.sleep(0)  # libera loop

            with tempfile.TemporaryDirectory(dir=job_dir) as tmp:
                tmp_path = Path(tmp)
                task.progress = 35; task.message = "Aplicando ajustes (slug/roteamento/caminhos)..."
                await asyncio.sleep(0)

                result_zip = convert_lovable_zip(input_zip, task.slug, tmp_path)

                task.progress = 85; task.message = "Gerando site.zip..."
                await asyncio.sleep(0)

                out_zip.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(result_zip), str(out_zip))

            task.progress = 100
            task.state = "done"
            task.message = f"Pronto! ({int(out_zip.stat().st_size/1024)} KB)"
            task.download_url = f"/download/{task_id}"
            task.eta_seconds = 0

        except subprocess.CalledProcessError as e:
            task.state = "error"; task.message = f"Falha no build (npm). Saída: {e}"; task.progress = 100
        except Exception as e:
            task.state = "error"; task.message = f"{type(e).__name__}: {e}"; task.progress = 100

        QUEUE.task_done()

@app.on_event("startup")
async def _startup():
    asyncio.create_task(worker_loop())

# ----------------- API -----------------
@app.post("/tasks")
async def enqueue(slug: str = Form(...), file: UploadFile = File(...)):
    if not slug.strip():
        raise HTTPException(400, "Slug inválida.")

    task_id = str(uuid.uuid4())
    job_dir = JOBS_ROOT / task_id
    job_dir.mkdir(parents=True, exist_ok=True)
    input_zip = job_dir / "input.zip"

    # salva upload em disco (streaming)
    total = 0
    with open(input_zip, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            f.write(chunk)

    if total > MAX_UPLOAD_MB * 1024 * 1024:
        try:
            input_zip.unlink()
        finally:
            pass
        raise HTTPException(413, f"Arquivo maior que {MAX_UPLOAD_MB} MB")

    task = Task(id=task_id, slug=slug.strip(), state="queued", progress=0, eta_seconds=60)
    TASKS[task_id] = task
    await QUEUE.put(task_id)
    return {"task_id": task_id}

@app.get("/tasks/{task_id}")
async def status(task_id: str):
    task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "Task não encontrada")

    if task.state == "queued":
        # estimativa simples: 60s por item na fila
        try:
            position = max(0, QUEUE.qsize() - 1)
        except Exception:
            position = 0
        task.eta_seconds = 60 * (position + 1)
    elif task.state == "working":
        # ETA baseada no progresso
        remaining = max(0, 100 - (task.progress or 0))
        task.eta_seconds = max(3, int(remaining * 1.2))

    return JSONResponse(task.model_dump())

@app.get("/download/{task_id}")
async def download(task_id: str):
    job_dir = JOBS_ROOT / task_id
    out_zip = job_dir / "site.zip"
    if not out_zip.exists():
        raise HTTPException(404, "site.zip não encontrado (o job terminou com erro ou foi limpo).")
    return FileResponse(path=str(out_zip), media_type="application/zip", filename="site.zip")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
