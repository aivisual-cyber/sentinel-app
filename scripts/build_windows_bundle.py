#!/usr/bin/env python3
"""
Build the OFFLINE Windows bundle (copy to USB, install on an air-gapped Windows 10/11 / Server machine).

Run on any internet-connected machine (Linux or Windows) with Python 3.10+ and pip:
    python scripts/build_windows_bundle.py                    # -> build/Sentinel-Windows-x64/ and .zip

The bundle contains everything; the target machine needs NO internet, NO Python, NO CUDA, NO VC++ redist:
    python/   CPython for Windows x64 (official NuGet package) + all packages pre-installed
              + Microsoft VC++ runtime DLLs (app-local; ONNX Runtime needs msvcp140*.dll)
    app/      backend, scripts, config templates
    ui/       operator console (served by the engine at http://127.0.0.1:8765/)
    models/   fire_smoke.onnx (exported from the PyTorch model; identical detections)
    tools/    WinSW-x64.exe (Windows service wrapper, MIT licence)
    service/  service definitions;  install.ps1, uninstall.ps1, README-WINDOWS.md, MANIFEST.txt (SHA-256)

GPU: onnxruntime-directml (DirectML ships inside the wheel) -> any DirectX 12 GPU via the display driver.
"""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_VERSION = '3.12.10'                 # CPython for Windows (NuGet "python" package)
PY_TAG = '3.12'
WINSW_URL = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
NUGET_URL = 'https://api.nuget.org/v3-flatcontainer/python/{v}/python.{v}.nupkg'


def log(msg):
    print(f"==> {msg}", flush=True)


def fetch(url: str, dest: Path, cache: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / Path(url).name
    if not cached.exists():
        log(f"download {url}")
        with urllib.request.urlopen(url, timeout=300) as r, open(str(cached) + '.part', 'wb') as f:
            shutil.copyfileobj(r, f)
        os.replace(str(cached) + '.part', cached)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cached, dest)
    return dest


def pip_install_windows(target: Path, requirements: Path):
    log(f"installing Windows packages ({requirements.name}) for CPython {PY_TAG} win_amd64")
    cmd = [sys.executable, '-m', 'pip', 'install', '--quiet', '--no-compile', '--target', str(target),
           '--platform', 'win_amd64', '--python-version', PY_TAG, '--implementation', 'cp',
           '--only-binary=:all:', '-r', str(requirements)]
    subprocess.run(cmd, check=True)


def build_ui(dest: Path):
    """Always rebuild: a stale dist/ would ship an outdated console."""
    dist = ROOT / 'dist'
    npm = shutil.which('npm')
    if npm:
        log("building UI (npm run build)")
        if not (ROOT / 'node_modules').exists():
            subprocess.run([npm, 'ci'], cwd=ROOT, check=True)
        subprocess.run([npm, 'run', 'build'], cwd=ROOT, check=True)
    elif (dist / 'index.html').exists():
        log("WARNING: npm not found - using existing dist/ (make sure it is current)")
    else:
        sys.exit("npm not found: install Node.js 18+ (needed once, on the build machine)")
    shutil.copytree(dist, dest)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=str(ROOT / 'build' / 'Sentinel-Windows-x64'))
    ap.add_argument('--cache', default=str(ROOT / 'build' / 'cache'))
    ap.add_argument('--model', default=str(ROOT / 'rabahdev_best.onnx'),
                    help='ONNX model (export a .pt with scripts/export_onnx.py)')
    ap.add_argument('--no-zip', action='store_true')
    args = ap.parse_args(argv)

    out, cache = Path(args.out), Path(args.cache)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    model = Path(args.model)
    if not model.exists():
        sys.exit(f"{model} not found - run: python scripts/export_onnx.py rabahdev_best.pt")

    # 1. Python for Windows (official CPython build published on NuGet by the Python core team)
    nupkg = fetch(NUGET_URL.format(v=PY_VERSION), cache / 'tmp' / 'python.nupkg', cache)
    with zipfile.ZipFile(nupkg) as z:
        for n in z.namelist():
            if n.startswith('tools/'):
                rel = n[len('tools/'):]
                if rel:
                    p = out / 'python' / rel
                    if n.endswith('/'):
                        p.mkdir(parents=True, exist_ok=True)
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(z.read(n))
    site = out / 'python' / 'Lib' / 'site-packages'

    # 2. Runtime packages (no PyTorch: inference uses ONNX Runtime + DirectML)
    pip_install_windows(site, ROOT / 'requirements-windows.txt')

    # 3. VC++ runtime DLLs next to python.exe (app-local deployment; offline machines often lack the redist)
    wheels = cache / 'msvc'
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--quiet', '--no-deps', '--dest', str(wheels),
                    '--platform', 'win_amd64', '--python-version', PY_TAG, '--only-binary=:all:', 'msvc-runtime'],
                   check=True)
    whl = sorted(wheels.glob('msvc_runtime-*.whl'))[-1]
    with zipfile.ZipFile(whl) as z:
        for n in z.namelist():
            if n.lower().endswith('.dll') and '/scripts/' not in n.lower():
                (out / 'python' / Path(n).name).write_bytes(z.read(n))
            if n.endswith('LICENSE'):
                (out / 'python' / 'VC_RUNTIME_LICENSE.txt').write_bytes(z.read(n))
    for stale in ('__pycache__',):
        for p in site.rglob(stale):
            shutil.rmtree(p, ignore_errors=True)

    # 4. Application
    app = out / 'app'
    shutil.copytree(ROOT / 'backend', app / 'backend', ignore=shutil.ignore_patterns('__pycache__', 'hardware'))
    (app / 'scripts').mkdir()
    for s in ('watchdog.py', 'gpu_check.py', 'benchmark.py'):
        shutil.copy(ROOT / 'scripts' / s, app / 'scripts' / s)
    (app / 'config').mkdir()
    for t in ('detection_config.yaml.template', 'notifications.yaml.template', 'cameras.yaml.template'):
        shutil.copy(ROOT / 'config' / t, app / 'config' / t)
    for f in ('run_sentinel.py', 'LICENSE'):
        shutil.copy(ROOT / f, app / f)
    (app / 'docs').mkdir()
    shutil.copy(ROOT / 'docs' / 'OPERATIONS.md', app / 'docs' / 'OPERATIONS.md')
    shutil.copy(ROOT / 'docs' / 'WINDOWS_STEP_BY_STEP.md', out / 'WINDOWS_STEP_BY_STEP.md')

    # 5. UI, model, service wrapper, installer
    build_ui(out / 'ui')
    (out / 'models').mkdir()
    shutil.copy(model, out / 'models' / 'fire_smoke.onnx')
    fetch(WINSW_URL, out / 'tools' / 'WinSW-x64.exe', cache)
    (out / 'service').mkdir()
    win = ROOT / 'deploy' / 'windows'
    for f in win.glob('*.xml.template'):
        shutil.copy(f, out / 'service' / f.name)
    for f in ('install.ps1', 'uninstall.ps1', 'README-WINDOWS.md'):
        shutil.copy(win / f, out / f)
    # PowerShell 5.1 reads BOM-less scripts as ANSI: add a UTF-8 BOM so any non-ASCII text survives
    for f in ('install.ps1', 'uninstall.ps1'):
        p = out / f
        data = p.read_bytes()
        if not data.startswith(b'\xef\xbb\xbf'):
            p.write_bytes(b'\xef\xbb\xbf' + data)

    # 6. Manifest (verify the copy on the target: Get-FileHash)
    files = sorted(p for p in out.rglob('*') if p.is_file())
    pkgs = sorted(p.name.replace('.dist-info', '') for p in site.glob('*.dist-info'))
    lines = [f"Sentinel Windows bundle - Python {PY_VERSION}, packages: {', '.join(pkgs)}", '']
    for p in files:
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(out).as_posix()}")
    (out / 'MANIFEST.txt').write_text('\n'.join(lines) + '\n')
    size = sum(p.stat().st_size for p in files) / 1e6
    log(f"bundle: {out} ({len(files)} files, {size:.0f} MB)")
    (out / 'bundle.json').write_text(json.dumps({'python': PY_VERSION, 'packages': pkgs}, indent=1))

    if not args.no_zip:
        z = shutil.make_archive(str(out), 'zip', root_dir=out.parent, base_dir=out.name)
        log(f"zip: {z} ({Path(z).stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
