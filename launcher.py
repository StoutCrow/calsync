"""Start calsync from a flash drive (or any folder) on Windows, macOS or Linux.

Everything stays in this folder: the program, its Python packages (one set per
kind of computer, installed here the first time you use that kind), and your
settings. Nothing is installed on the laptop itself.

  python launcher.py            open calsync in this laptop's browser
  python launcher.py --phone    also allow phones on the same Wi-Fi
  python launcher.py --demo     sample data, nothing is sent or saved
"""
import os
import importlib.util
import platform
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)     # True inside the .exe build
# In the .exe, settings live next to the .exe; otherwise next to this file.
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
os.environ["CALSYNC_HOME"] = str(HERE)
APP = HERE / "app"
# import name -> pip package. tzdata: Windows has no built-in time zone list.
PACKAGES = {"anthropic": "anthropic", "pydantic": "pydantic", "PIL": "pillow", "tzdata": "tzdata",
            "icalendar": "icalendar", "recurring_ical_events": "recurring-ical-events"}


def app_browser() -> str | None:
    """A Chromium browser to show calsync in its own window (Windows: Edge, Chrome or Brave)."""
    if sys.platform != "win32":
        return None
    roots = [os.environ.get(v, "") for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    for rel in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe",
                "BraveSoftware/Brave-Browser/Application/brave.exe"):
        for root in roots:
            if root and (Path(root) / rel).exists():
                return str(Path(root) / rel)
    return None


def set_window_icon():
    """Windows: give the calsync window our own sharp icon (Chromium otherwise shows a tiny favicon)."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    ico = Path(getattr(sys, "_MEIPASS", HERE)) / "calsync.ico"
    if not ico.exists():
        return
    u = ctypes.windll.user32
    u.LoadImageW.restype = ctypes.c_void_p
    u.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p]
    sm, lg = u.GetSystemMetrics(49), u.GetSystemMetrics(11)     # title-bar and taskbar sizes at this screen's scaling
    big = u.LoadImageW(None, str(ico), 1, lg, lg, 0x10)         # IMAGE_ICON, LR_LOADFROMFILE
    small = u.LoadImageW(None, str(ico), 1, sm, sm, 0x10)
    Enum = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        if u.IsWindowVisible(hwnd):
            cls, title = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
            u.GetClassNameW(hwnd, cls, 64); u.GetWindowTextW(hwnd, title, 64)
            if cls.value == "Chrome_WidgetWin_1" and title.value == "calsync":
                u.SendMessageW(hwnd, 0x80, 1, big)               # WM_SETICON, ICON_BIG
                u.SendMessageW(hwnd, 0x80, 0, small)             # ICON_SMALL
        return True
    for _ in range(40):                                            # re-apply: Chromium resets it on load
        u.EnumWindows(Enum(each), 0)
        time.sleep(1)


def open_tab(url: str) -> None:
    """A new tab in your running browser (Edge, Chrome or Brave on Windows), else the system default."""
    exe = app_browser()
    if exe:
        subprocess.Popen([exe, url])
    else:
        webbrowser.open(url)


def calsync_window_open() -> bool:
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    hit = []
    Enum = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def each(hwnd, _):
        cls, title = ctypes.create_unicode_buffer(64), ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64); u.GetWindowTextW(hwnd, title, 64)
        if cls.value == "Chrome_WidgetWin_1" and title.value == "calsync":
            hit.append(hwnd)
        return True
    u.EnumWindows(Enum(each), 0)
    return bool(hit)


def say(msg=""):
    print(msg, flush=True)


def check_python():
    if sys.version_info < (3, 10):
        say(f"calsync needs Python 3.10 or newer; this laptop has {platform.python_version()}.")
        say("Install a newer one from https://www.python.org/downloads/ and try again.")
        sys.exit(1)


def load_packages():
    """Packages are stored per OS/CPU/Python version, so one drive works everywhere."""
    if FROZEN:
        return                      # the .exe already contains everything
    tag = f"{sys.platform}-{platform.machine().lower()}-py{sys.version_info.major}{sys.version_info.minor}"
    lib = HERE / "lib" / tag
    sys.path.insert(0, str(lib))
    missing = [pkg for mod, pkg in PACKAGES.items() if importlib.util.find_spec(mod) is None]
    if missing:
        say(f"Setting up calsync for this kind of computer ({tag}): {', '.join(missing)}")
        say("Installing calsync's packages onto the drive (about 70 MB).")
        say("You'll see each download below. The last step, installing 'anthropic', writes")
        say("thousands of small files: on a flash drive that can take 2-5 minutes. Please wait.")
        lib.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "pip", "install", "--timeout", "30", "--retries", "3", "--no-compile", "--disable-pip-version-check",
               "--no-warn-script-location", "--upgrade", "--target", str(lib), *missing]
        if subprocess.call(cmd) != 0:
            say("Couldn't install the packages. Check the internet connection and try again.")
            say("(If it says 'No module named pip', reinstall Python from python.org.)")
            sys.exit(1)
        importlib.invalidate_caches()
        say("Done.")
    sys.path.insert(0, str(APP))


def load_settings() -> Path | None:
    """Settings file on the drive first, then the laptop's own (~/.config/calsync.env)."""
    for path in (HERE / "config" / "calsync.env", Path.home() / ".config" / "calsync.env"):
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    value = value.strip().strip('"').strip("'")
                    if value:
                        os.environ.setdefault(key.strip(), value)
            return path
    return None


def main():
    check_python()
    if FROZEN and sys.stdout is None:          # windowed .exe has no console: keep messages in a log
        sys.stdout = sys.stderr = open(HERE / "calsync.log", "a", encoding="utf-8", buffering=1)
    args = sys.argv[1:]
    load_packages()
    settings = load_settings()

    if settings:
        say(f"Using settings from {settings}")
    elif "--demo" not in args:
        say("No settings found, so starting in demo mode.")
        say(f"To use your accounts, copy {HERE / 'config' / 'calsync.env.example'}")
        say(f"to {HERE / 'config' / 'calsync.env'} and fill it in.")
        args.append("--demo")

    # A laptop on café or hotel Wi-Fi should not be reachable by others, so the
    # default is this laptop only. --phone opens it to the local network.
    phone = "--phone" in args
    nobrowser = "--no-open" in args or (sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))
    # Open in a tab of your normal browser (and keep running when the tab closes) instead of
    # calsync's own window. Set once with CALSYNC_OPEN=tab in the settings file, or use --tab.
    tab = "--tab" in args or os.environ.get("CALSYNC_OPEN", "").lower() == "tab"
    args = [a for a in args if a not in ("--phone", "--tab", "--no-open")]
    fixed = bool(os.environ.get("CALSYNC_APP_TOKEN"))
    port = int(os.environ.get("CALSYNC_PORT", "8766"))
    if fixed and not nobrowser:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        except OSError:
            pass
        else:                                  # calsync is already running: just open it and leave
            open_tab(f"http://127.0.0.1:{port}/#t={os.environ['CALSYNC_APP_TOKEN']}")
            return
    if not phone:
        args.append("--local-only")
    os.environ.setdefault("CALSYNC_APP_TOKEN", secrets.token_urlsafe(12))
    port = int(os.environ.get("CALSYNC_PORT", "8766"))
    args += ["--port", str(port)]

    url = f"http://127.0.0.1:{port}/#t={os.environ['CALSYNC_APP_TOKEN']}"
    def open_when_ready():
        for _ in range(100):           # wait up to 20 s for the server to start
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            return
        exe = app_browser()
        if not exe or tab:
            open_tab(url)
            return
        # Own window with no tabs or address bar. Closing it stops calsync.
        win = subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={HERE / 'window'}",
                                "--no-first-run", "--disable-background-mode", "--no-default-browser-check", "--window-size=1100,900"])
        threading.Thread(target=set_window_icon, daemon=True).start()
        if sys.platform != "win32":
            win.wait()
            os._exit(0)
        # Watch for the window itself, not the browser process: a browser that is already
        # running can hand the window to another process and exit at once.
        seen, gone, t0 = False, 0, time.time()
        while True:
            time.sleep(2)
            if calsync_window_open():
                seen, gone = True, 0
            else:
                gone += 1
            if (seen and gone >= 3) or (not seen and win.poll() is not None and time.time() - t0 > 30):
                os._exit(0)

    if not nobrowser:                          # a server (no screen, or --no-open) just keeps running
        threading.Thread(target=open_when_ready, daemon=True).start()

    import app
    sys.argv = ["app.py", *args]
    say("Close this window (or press Ctrl+C) to stop calsync.")
    app.main()


if __name__ == "__main__":
    main()
