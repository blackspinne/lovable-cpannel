# app/processor.py
import os, re, json, zipfile, tempfile, subprocess, shutil
from pathlib import Path

SAFE_PREFIXES = ("http://", "https://", "//", "data:", "mailto:", "tel:")

def _read_json(p: Path):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}

def _has_dep(pkg: str, pkgjson: dict) -> bool:
    for k in ("dependencies","devDependencies","peerDependencies"):
        if pkg in pkgjson.get(k, {}): return True
    return False

def _find_project_root(base: Path) -> Path:
    candidates = [p.parent for p in base.rglob("package.json") if "node_modules" not in p.parts]
    if not candidates: raise FileNotFoundError("package.json não encontrado no ZIP.")
    def score(p: Path):
        s = 0
        if any((p/f).exists() for f in ["vite.config.ts","vite.config.js","next.config.js","next.config.mjs"]): s += 10
        return s - len(p.parts)
    return sorted(candidates, key=score, reverse=True)[0]

def _detect_framework(root: Path) -> str:
    pkg = _read_json(root/"package.json")
    if (root/"vite.config.ts").exists() or (root/"vite.config.js").exists() or _has_dep("vite", pkg): return "vite"
    if _has_dep("next", pkg) or (root/"next.config.js").exists() or (root/"next.config.mjs").exists(): return "next"
    if _has_dep("react-scripts", pkg): return "cra"
    return "unknown"

def _patch_file_text(p: Path, transform):
    if not p.exists(): return False
    try: txt = p.read_text(encoding="utf-8")
    except UnicodeDecodeError: txt = p.read_text(encoding="latin-1")
    new = transform(txt)
    if new != txt:
        p.write_text(new, encoding="utf-8"); return True
    return False

def _ensure_vite_config(root: Path, slug: str):
    pkg = _read_json(root/"package.json")
    use_ts = (root/"tsconfig.json").exists() or any((root/"src").glob("**/*.ts*"))
    fname = "vite.config.ts" if use_ts else "vite.config.js"; p = root/fname
    imports = "import react from '@vitejs/plugin-react'\n" if _has_dep("@vitejs/plugin-react", pkg) else ""
    plugin = "  plugins: [react()],\n" if imports else ""
    if not p.exists():
        p.write_text(
            "import { defineConfig } from 'vite'\n"
            f"{imports}\n"
            "export default defineConfig({\n"
            f"{plugin}  base: '/" + slug + "/',\n"
            "})\n", encoding="utf-8")
        return True
    desired = f'"/{slug}/"'
    def tf(txt:str):
        out, n = re.subn(r'base\s*:\s*["\']/[^"\']*/["\']', f'base: {desired}', txt)
        if n==0:
            out = re.sub(r'(defineConfig\(\s*(?:\(\s*\w+\s*\)\s*=>\s*)?\{\s*)', r'\1base: '+desired+', ', txt, count=1)
        return out
    return _patch_file_text(p, tf)

def _patch_next(root: Path, slug:str):
    for fn in ("next.config.js","next.config.mjs"):
        p=root/fn
        if not p.exists(): continue
        def tf(txt:str):
            if "basePath" in txt or "assetPrefix" in txt:
                t = re.sub(r'basePath\s*:\s*["\'][^"\']*["\']', f'basePath: "/{slug}"', txt)
                t = re.sub(r'assetPrefix\s*:\s*["\'][^"\']*["\']', f'assetPrefix: "/{slug}/"', t)
                return t
            t = re.sub(r'(module\.exports\s*=\s*\{)', r'\1\n  basePath: "/%s",\n  assetPrefix: "/%s/",' % (slug,slug), txt, count=1)
            t = re.sub(r'(export\s+default\s*\{)', r'\1\n  basePath: "/%s",\n  assetPrefix: "/%s/",' % (slug,slug), t, count=1)
            return t
        if _patch_file_text(p, tf):
            pkg = _read_json(root/"package.json"); sc=pkg.get("scripts", {})
            sc["export"]="next build && next export -o dist"; pkg["scripts"]=sc
            (root/"package.json").write_text(json.dumps(pkg,indent=2,ensure_ascii=False))
            return True
    return False

def _patch_cra(root: Path, slug:str):
    pkgp=root/"package.json"; pkg=_read_json(pkgp)
    if pkg.get("homepage") != f"/{slug}":
        pkg["homepage"]=f"/{slug}"
        pkgp.write_text(json.dumps(pkg,indent=2,ensure_ascii=False)); return True
    return False

def _locate_src(root:Path)->Path:
    for d in ("src","app","frontend/src"):
        p=root/d
        if p.exists(): return p
    return root/"src"

def _ensure_hashrouter(src:Path):
    target=None
    for n in ("main.tsx","main.jsx","index.tsx","index.jsx","main.ts","main.js","index.ts","index.js"):
        p=src/n
        if p.exists(): target=p; break
    if not target: return False
    def tf(txt:str):
        t=txt
        if re.search(r'from\s+[\'"]react-router-dom[\'"]', t) and "HashRouter" not in t:
            t=re.sub(r'import\s*{\s*', 'import { HashRouter, ', t, count=1)
        elif "react-router-dom" not in t:
            t=re.sub(r'(^\s*import[^\n]*\n)', r'\1import { HashRouter } from "react-router-dom";\n', t, count=1, flags=re.M)
        t=t.replace("BrowserRouter","HashRouter")
        t=re.sub(r'(<App\s*/>)', r'<HashRouter>\1</HashRouter>', t)
        t=re.sub(r'(<App\s*>\s*</App\s*>)', r'<HashRouter>\1</HashRouter>', t)
        return t
    return _patch_file_text(target, tf)

def _remove_browserrouter(src:Path):
    changed=False
    for n in ("App.tsx","App.jsx","App.ts","App.js"):
        p=src/n
        if not p.exists(): continue
        def tf(txt:str):
            t=txt
            t=re.sub(r'import\s*{\s*BrowserRouter\s*(?:,\s*)?', 'import { ', t)
            t=re.sub(r',\s*BrowserRouter\s*}', '}', t)
            t=re.sub(r'import\s+BrowserRouter\s+from\s+[\'"]react-router-dom[\'"]\s*;?\s*', '', t)
            t=re.sub(r'<\s*BrowserRouter[^>]*>', '<>', t)
            t=re.sub(r'</\s*BrowserRouter\s*>', '</>', t)
            return t
        changed = _patch_file_text(p, tf) or changed
    return changed

def _vite_post_build_sanity(dist:Path):
    idx=dist/"index.html"
    if idx.exists():
        s=idx.read_text(encoding="utf-8")
        s2=s.replace('href="/assets/','href="assets/').replace('src="/assets/','src="assets/')
        if s2!=s: idx.write_text(s2,encoding="utf-8")

def _is_safe(u:str)->bool: return u.strip().lower().startswith(SAFE_PREFIXES)
def _strip_leading(u:str)->str:
    if not u or _is_safe(u): return u
    return u.lstrip("/") if u.startswith("/") else u

def _sanity_html_css(dist:Path):
    for hp in dist.rglob("*.html"):
        s=hp.read_text(encoding="utf-8",errors="ignore")
        def repl(m):
            attr,q,u=m.group(1),m.group(2),m.group(3)
            return f'{attr}={q}{_strip_leading(u)}{q}'
        s2=re.sub(r'(src|href)\s*=\s*(")([^"]+)(")', repl, s, flags=re.I)
        s2=re.sub(r'(src|href)\s*=\s*(\')([^\']+)(\')', repl, s2, flags=re.I)
        if s2!=s: hp.write_text(s2,encoding="utf-8")
    for cp in dist.rglob("*.css"):
        s=cp.read_text(encoding="utf-8",errors="ignore")
        def repl(m):
            inner=m.group(1).strip().strip('"').strip("'")
            fixed=_strip_leading(inner)
            if '"' in m.group(1): return f'url("{fixed}")'
            if "'" in m.group(1): return f"url('{fixed}')"
            return f'url({fixed})'
        s2=re.sub(r'url\(([^)]+)\)', repl, s, flags=re.I)
        if s2!=s: cp.write_text(s2,encoding="utf-8")

def _write_htaccess(dist:Path, slug:str):
    (dist/".htaccess").write_text(
        "DirectoryIndex index.html\n"
        "RewriteEngine On\n"
        f"RewriteBase /{slug}/\n\n"
        "RewriteCond %{REQUEST_FILENAME} -f [OR]\n"
        "RewriteCond %{REQUEST_FILENAME} -d\n"
        "RewriteRule ^ - [L]\n\n"
        "RewriteRule . index.html [L]\n", encoding="utf-8")

def _run(cmd, cwd:Path, timeout:int=600):
    subprocess.check_call(cmd, cwd=str(cwd), timeout=timeout)

def _which_npm_cmd()->str:
    if os.name=="nt":
        for c in (r"C:\Program Files\nodejs\npm.cmd", r"C:\Program Files (x86)\nodejs\npm.cmd", "npm.cmd"):
            if shutil.which(c) or Path(c).exists(): return c
        return "npm.cmd"
    for c in ("/opt/homebrew/bin/npm", "/usr/local/bin/npm"):
        if Path(c).exists(): return c
    return shutil.which("npm") or "npm"

def _build(root:Path, fw:str)->Path:
    npm=_which_npm_cmd()
    if fw=="next":
        _run([npm,"install"], root); _run([npm,"run","export"], root); return root/"dist"
    if (root/"package-lock.json").exists():
        try: _run([npm,"ci"], root)
        except subprocess.CalledProcessError:
            _run([npm,"install"], root)
    else:
        _run([npm,"install"], root)
    _run([npm,"run","build"], root)
    for c in ("dist","build","out"):
        if (root/c).exists(): return root/c
    return root/"dist"

def _zip_dir_with_perms(dirp:Path, out_zip:Path):
    def add_dir(z, rel:str):
        if not rel.endswith("/"): rel+="/"
        zi=zipfile.ZipInfo(rel); zi.external_attr=(0o755&0xFFFF)<<16; z.writestr(zi,b"")
    with zipfile.ZipFile(out_zip,"w",compression=zipfile.ZIP_DEFLATED) as z:
        for d in sorted([p for p in dirp.rglob("*") if p.is_dir()]): add_dir(z, d.relative_to(dirp).as_posix())
        for f in dirp.rglob("*"):
            if f.is_file():
                rel=f.relative_to(dirp).as_posix()
                zi=zipfile.ZipInfo(rel); zi.external_attr=(0o644&0xFFFF)<<16
                with open(f,"rb") as fh: z.writestr(zi, fh.read())

def process_lovable_zip(zip_bytes: bytes, slug: str, max_unpack_mb: int = 500) -> bytes:
    if not re.fullmatch(r"[a-z0-9][a-z0-9/_-]*", slug):
        raise ValueError("Slug inválida. Use apenas letras/números, -, _ e /.")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir=Path(tmp)
        srczip=tmpdir/"src.zip"; srczip.write_bytes(zip_bytes)
        if srczip.stat().st_size > max_unpack_mb*1024*1024: raise ValueError("ZIP muito grande.")
        with zipfile.ZipFile(srczip) as z: z.extractall(tmpdir/"src")
        root=_find_project_root(tmpdir/"src"); fw=_detect_framework(root)
        if fw=="vite": _ensure_vite_config(root, slug)
        elif fw=="next": _patch_next(root, slug)
        elif fw=="cra": _patch_cra(root, slug)
        src=_locate_src(root)
        if src.exists(): _ensure_hashrouter(src); _remove_browserrouter(src)
        dist=_build(root, fw)
        if fw=="vite": _vite_post_build_sanity(dist)
        _sanity_html_css(dist); _write_htaccess(dist, slug)
        outzip=tmpdir/"site.zip"; _zip_dir_with_perms(dist, outzip)
        return outzip.read_bytes()
