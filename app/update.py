"""Update calsync from its GitHub releases.

Rules this file keeps, on purpose:
  * It only ever *asks* GitHub what the newest release is (read-only, no login, no data sent).
  * Nothing is downloaded or replaced until you press "Update now" in the page.
  * Downloads must come from this project's own release page over https, and the new .exe
    must match the SHA-256 listed in the release's SHA256SUMS file before it is used.
  * Your settings (config/), packages (lib/) and browser profile (window/) are never touched.

Honest limit: the checksum comes from the same release as the file, so it catches a damaged
or truncated download, not a hijacked GitHub account. Read the release notes you trust.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from version import VERSION

REPO = os.environ.get("CALSYNC_REPO", "StoutCrow/calsync")
API = os.environ.get("CALSYNC_UPDATE_API", "https://api.github.com").rstrip("/")   # overridden only by tests
CHECK_EVERY = 6 * 3600
_cache: dict = {"at": 0.0, "rel": None, "err": ""}

# Program files an update may replace when calsync runs from source. Everything else is left alone.
SOURCE_FILES = ("app", "launcher.py", "check.py", "install-server.sh", "start-calsync.sh", "requirements.txt",
                "README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md")


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _get(url: str, accept: str = "application/vnd.github+json", timeout: int = 10) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": f"calsync/{VERSION}", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest(fresh: bool = False) -> dict | None:
    """The newest GitHub release, cached for a few hours. None if it can't be fetched."""
    if os.environ.get("CALSYNC_UPDATE_CHECK", "").lower() == "off":     # a way to never contact GitHub
        _cache.update(at=time.time(), rel=None, err="update checks are switched off")
        return None
    if not fresh and _cache["rel"] is not None and time.time() - _cache["at"] < CHECK_EVERY:
        return _cache["rel"]
    try:
        rel = json.loads(_get(f"{API}/repos/{REPO}/releases/latest"))
        _cache.update(at=time.time(), rel=rel, err="")
        return rel
    except Exception as e:                       # offline, rate-limited, private or missing repo: not an error to the user
        _cache.update(at=time.time(), rel=None, err=f"{type(e).__name__}: {e}")
        return None


def check(fresh: bool = False) -> dict:
    rel = latest(fresh)
    out = {"current": VERSION, "repo": REPO, "newer": False, "latest": "", "notes": "", "url": f"https://github.com/{REPO}/releases",
           "can_install": False, "error": _cache["err"] if rel is None else ""}
    if rel:
        tag = rel.get("tag_name", "")
        page = rel.get("html_url", "")
        out.update(latest=tag.lstrip("v"), notes=(rel.get("body") or "")[:1500],
                   url=page if page.startswith(("https://github.com/", "http://127.0.0.1")) else out["url"],
                   newer=parse(tag) > parse(VERSION))
        out["can_install"] = out["newer"] and (_asset(rel, "calsync.exe") is not None if is_frozen() else bool(rel.get("zipball_url")))
    return out


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _asset(rel: dict, name: str) -> str | None:
    for a in rel.get("assets", []):
        if a.get("name") == name:
            return a.get("browser_download_url")
    return None


def _trusted(url: str) -> bool:
    """Only this project's own release downloads (or the test server) over https."""
    ok = (f"https://github.com/{REPO}/releases/download/", f"{API}/repos/{REPO}/")
    return url.startswith(ok) or (API != "https://api.github.com" and url.startswith(API))


def _download(url: str) -> bytes:
    if not _trusted(url):
        raise RuntimeError("Refusing to download from an unexpected address: " + url)
    return _get(url, accept="application/octet-stream", timeout=120)


def _wanted_sha(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0].lower()
    raise RuntimeError(f"The release has no checksum for {name}, so I won't install it.")


def install() -> dict:
    """Install the newest release. Returns a message; the .exe case then restarts calsync."""
    rel = latest(fresh=True)
    if not rel or parse(rel.get("tag_name", "")) <= parse(VERSION):
        return {"ok": False, "message": "You already have the newest version."}
    return _install_exe(rel) if is_frozen() else _install_source(rel)


def _install_exe(rel: dict) -> dict:
    if sys.platform != "win32":
        return {"ok": False, "message": "Self-update of the packaged app is Windows only. Download the new release instead."}
    url, sums = _asset(rel, "calsync.exe"), _asset(rel, "SHA256SUMS")
    if not url or not sums:
        return {"ok": False, "message": "This release has no calsync.exe or SHA256SUMS file."}
    data = _download(url)
    want = _wanted_sha(_download(sums).decode("utf-8", "replace"), "calsync.exe")
    if hashlib.sha256(data).hexdigest() != want:
        return {"ok": False, "message": "The download did not match its checksum, so it was thrown away. Nothing changed."}
    exe = Path(sys.executable).resolve()
    new, old = exe.with_name("calsync.new.exe"), exe.with_name("calsync.old.exe")
    new.write_bytes(data)
    if old.exists():
        old.unlink()
    exe.rename(old)               # Windows lets a running .exe be renamed, not overwritten
    try:
        new.rename(exe)
    except OSError:
        old.rename(exe)           # put the working copy back
        raise
    args = " ".join(a for a in os.environ.get("CALSYNC_LAUNCH_ARGS", "").split() if re.fullmatch(r"--[a-z-]+", a))
    # Start the new copy a moment after this one exits, so the port is free. PyInstaller one-file apps pass
    # their temp folder to child processes; telling the new copy to reset its environment makes it unpack its own.
    import subprocess
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen(f'cmd /c "ping -n 4 127.0.0.1 >nul & start "" "{exe}" {args}"', creationflags=0x08000000,
                     close_fds=True, env=env)
    threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"ok": True, "restart": True, "message": f"Updated to {rel['tag_name'].lstrip('v')}. calsync is restarting."}


def _install_source(rel: dict) -> dict:
    root = Path(os.environ.get("CALSYNC_HOME") or Path(__file__).resolve().parent.parent)
    z = zipfile.ZipFile(io.BytesIO(_download(rel["zipball_url"])))
    with tempfile.TemporaryDirectory() as tmp:
        z.extractall(tmp)
        top = next(Path(tmp).iterdir())          # GitHub puts everything in one top folder
        for name in SOURCE_FILES:
            src = top / name
            if not src.exists():
                continue
            dst = root / name
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, dst)
    return {"ok": True, "restart": False, "message": f"Updated to {rel['tag_name'].lstrip('v')}. Close calsync and start it again to use it."}


def cleanup_after_update() -> None:
    """Remove the previous .exe left behind by an update (it can't be deleted while it was running)."""
    if is_frozen():
        for name in ("calsync.old.exe", "calsync.new.exe"):
            try:
                (Path(sys.executable).resolve().with_name(name)).unlink()
            except OSError:
                pass
