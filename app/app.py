#!/usr/bin/env python3
"""calsync web app: a phone-friendly page on top of calsync.py.

    python3 app.py            # uses the same env vars as calsync.py
    python3 app.py --demo     # try the interface with sample data, no keys needed

Open the printed link on your phone (same Wi-Fi). The link carries a private
access code, because this server can write to your Notion and read your email.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import socket
import tempfile
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import calsync as cs
import update
from version import VERSION

WEB = Path(__file__).parent / "web"
MAX_BODY = 80 * 1024 * 1024
CHECK_PREFIX = "[check: handwriting was unclear]"
DEMO = False
TOKEN = ""
PORT = 8766
OAUTH_STATES: set[str] = set()


# ------------------------------------------------------------ card <-> Event

def to_card(e: cs.Event, existing: set[str]) -> dict:
    notes = e.notes
    unsure = CHECK_PREFIX in notes
    notes = notes.replace(CHECK_PREFIX, "").strip()
    return {
        "key": e.key(), "source_id": e.source_id, "title": e.title, "type": e.type,
        "status": e.status, "people": e.people, "location": e.location, "notes": notes,
        "unsure": unsure, "all_day": e.all_day,
        "date": e.start.date().isoformat(),
        "end_date": e.end.date().isoformat(),
        "start_time": "" if e.all_day else e.start.strftime("%H:%M"),
        "end_time": "" if e.all_day else e.end.strftime("%H:%M"),
        "already": e.key() in existing,
    }


def from_card(c: dict) -> cs.Event:
    day = date.fromisoformat(c["date"])
    if c.get("all_day") or not c.get("start_time"):
        start = end = datetime.combine(day, datetime.min.time(), cs.TZ)
        all_day = True
    else:
        start = datetime.combine(day, datetime.strptime(c["start_time"], "%H:%M").time(), cs.TZ)
        if c.get("end_time"):
            end_day = date.fromisoformat(c.get("end_date") or c["date"])
            end = datetime.combine(end_day, datetime.strptime(c["end_time"], "%H:%M").time(), cs.TZ)
            if end <= start:
                end = datetime.combine(day, end.time(), cs.TZ) + timedelta(days=1)
        else:
            end = start + timedelta(hours=1)
        all_day = False
    return cs.Event(title=c["title"].strip() or "Untitled", start=start, end=end, type=c["type"],
                    status=c.get("status") or "Confirmed", people=c.get("people", ""),
                    location=c.get("location", ""), notes=c.get("notes", ""),
                    source_id=c.get("source_id", ""), all_day=all_day)


def notion_keys() -> set[str]:
    if DEMO or not (os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID")):
        return set()
    return cs.existing_keys(os.environ["NOTION_DATABASE_ID"])


def blocks_from_upload(files: list[dict]) -> list[dict]:
    blocks = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, f in enumerate(files):
            name = Path(f.get("name") or f"upload{i}.jpg").name
            path = Path(tmp) / f"{i}-{name}"
            path.write_bytes(base64.b64decode(f["data"]))
            blocks += [{"type": "text", "text": f"--- File: {name} ---"}, *cs.blocks_from_file(path)]
    return blocks


# ------------------------------------------------------------------ demo data

def demo_events(mode: str, only_me: bool) -> list[cs.Event]:
    time.sleep(1.8)
    tz = cs.TZ
    d = lambda m, day, h=0, mi=0: datetime(2026, m, day, h, mi, tzinfo=tz)
    if mode == "describe":
        return [cs.Event(title="Dentist", start=d(11, 17, 15), end=d(11, 17, 16), type="Family", status="Confirmed",
                         notes="Sample: this is what “Dentist next Tuesday at 3pm” becomes")]
    if mode == "work":
        return [cs.Event(title="Shift - Corner Cafe (Barista)", start=d(11, n, 16, 30),
                         end=d(11, n, 19), type="Work", status="Confirmed", location="Corner Cafe")
                for n in range(2, 7)]
    if mode == "family":
        evs = [
            cs.Event(title="Mia: Dentist", start=d(11, 12, 15, 30), end=d(11, 12, 16, 30), type="Family",
                     status="Confirmed", people="Mia", notes="No end time written, set to 1 hour"),
            cs.Event(title="Alex: Driving lesson", start=d(11, 14, 10), end=d(11, 14, 11), type="Family",
                     status="Confirmed", people="Alex", notes=f"{CHECK_PREFIX} could be 10 or 11"),
            cs.Event(title="Grandma's for dinner", start=d(11, 15, 17), end=d(11, 15, 20), type="Family",
                     status="Confirmed", people="Everyone", location="Grandma's"),
            cs.Event(title="Dad's birthday", start=d(11, 20), end=d(11, 20), type="Family",
                     status="Confirmed", people="Dad", all_day=True),
        ]
        return [e for e in evs if not only_me or e.people in ("Alex", "Everyone")]
    return [cs.Event(title="Hangout with Sam, Jo", start=d(10, 3, 19, 10), end=d(10, 3, 21, 40),
                     type="Hangout", status="Done", people="Jo, Sam", location="Austin, TX",
                     notes="14 photos in Immich", source_id="immich:demo1")]


# ----------------------------------------------------------------- endpoints

TAB_DEFAULTS = {"work": ("Work", "schedules"), "family": ("Personal", "home & friends"),
                "describe": ("Describe", "type it"), "calendar": ("Calendar", "notion")}


def api_status(_: dict) -> dict:
    has = lambda *names: DEMO or all(os.environ.get(n) for n in names)
    return {
        "tabs": {k: {"label": os.environ.get(f"CALSYNC_TAB_{k.upper()}") or d[0],
                     "sub": os.environ.get(f"CALSYNC_TAB_{k.upper()}_SUB", "") or d[1]} for k, d in TAB_DEFAULTS.items()},
        "demo": DEMO, "google_edit": cs.google_oauth_ready(), "version": VERSION, "repo": update.REPO,
        "names": os.environ.get("CALSYNC_MY_NAME", "Alex Doe, Alex" if DEMO else ""),
        "timezone": cs.TZ.key,
        "today": date.today().isoformat(),
        "connections": [
            {"name": "Notion", "ok": has("NOTION_TOKEN", "NOTION_DATABASE_ID"),
             "fix": "add the token and database in Settings"},
            {"name": f"Reader ({cs.PROVIDERS[cs.reader()]})", "ok": has(*cs.NEEDS[cs.PROVIDERS[cs.reader()]]),
             "fix": "add its key or address in Settings"},
            {"name": "Email", "ok": has("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"),
             "fix": "add the server, address and password in Settings"},
            {"name": "Immich", "ok": has("IMMICH_URL", "IMMICH_API_KEY"),
             "fix": "add the address and API key in Settings"},
        ],
    }


def api_read(body: dict) -> dict:
    mode = body.get("mode")
    only_me = bool(body.get("only_me"))
    if DEMO:
        events = demo_events(mode, only_me)
    elif mode == "work":
        blocks = blocks_from_upload(body["files"]) if body.get("files") else cs.blocks_from_email(int(body.get("days", 14)))
        if not blocks:
            return {"cards": [], "message": "No schedule emails found in that time."}
        events = cs.extract_shifts(blocks)
    elif mode == "family":
        events = cs.extract_family(blocks_from_upload(body["files"]), only_me)
    elif mode == "describe":
        events = cs.extract_text(str(body.get("text", "")))
    elif mode == "hangouts":
        since = date.fromisoformat(body["since"])
        until = date.fromisoformat(body.get("until") or date.today().isoformat())
        events = cs.cluster_hangouts(cs.immich_assets(since, until), float(body.get("gap_hours", 2)),
                                     int(body.get("min_photos", 3)), not body.get("include_solo"))
    else:
        raise ValueError("Unknown mode")
    existing = notion_keys()
    return {"cards": [to_card(e, existing) for e in sorted(events, key=lambda e: e.start)]}


def api_add(body: dict) -> dict:
    added = []
    if DEMO:
        time.sleep(1.0)
        return {"added": [c["key"] for c in body["cards"]]}
    db = cs.env("NOTION_DATABASE_ID")
    for c in body["cards"]:
        cs.create_page(db, from_card(c))
        added.append(c["key"])
    return {"added": added}


# --------------------------------------------------------------- settings
# (key, label, secret, placeholder). The page draws itself from this list.
# Each section is (title, test name, fields, reader it belongs to). A section tied to a
# reader only shows while that reader is chosen.
SECTIONS = [
    ("You", None, [
        ("CALSYNC_MY_NAME", "Names on rosters", 0, "Jane Smith, Jane"),
        ("CALSYNC_TIMEZONE", "Time zone", 0, "America/New_York")], None),
    ("Reader", None, [("CALSYNC_READER", "Which AI reads your photos and schedules", 0, "")], None),
    ("Gemini", "Gemini", [
        ("GEMINI_API_KEY", "API key", 1, "from aistudio.google.com"),
        ("GEMINI_MODEL", "Model", 0, "gemini-3.6-flash")], "gemini"),
    ("Claude", "Claude", [("ANTHROPIC_API_KEY", "API key", 1, "sk-ant-…")], "claude"),
    ("Local model", "Local", [
        ("LOCAL_URL", "Address", 0, "http://localhost:11434"),
        ("LOCAL_MODEL", "Model", 0, "qwen2.5vl:7b")], "local"),
    ("Notion", "Notion", [
        ("NOTION_TOKEN", "Integration token", 1, "ntn_… or secret_…"),
        ("NOTION_DATABASE_ID", "Database ID or link", 0, "paste the database's link")], None),
    ("Immich", "Immich", [
        ("IMMICH_URL", "Address", 0, "http://192.168.1.20:2283"),
        ("IMMICH_API_KEY", "API key", 1, "")], None),
    ("Email", "Email", [
        ("IMAP_HOST", "Server", 0, "imap.gmail.com"),
        ("IMAP_USER", "Address", 0, ""),
        ("IMAP_PASSWORD", "App password", 1, "")], None),
    ("Tab names (type - as the name to hide a tab)", None, [
        ("CALSYNC_TAB_WORK", "Tab 1 name", 0, "Work"), ("CALSYNC_TAB_WORK_SUB", "Tab 1 subtitle", 0, "schedules"),
        ("CALSYNC_TAB_FAMILY", "Tab 2 name (photo calendar and hangouts)", 0, "Personal"), ("CALSYNC_TAB_FAMILY_SUB", "Tab 2 subtitle", 0, "home & friends"),
        ("CALSYNC_TAB_DESCRIBE", "Tab 3 name", 0, "Describe"), ("CALSYNC_TAB_DESCRIBE_SUB", "Tab 3 subtitle", 0, "type it"),
        ("CALSYNC_TAB_CALENDAR", "Tab 4 name", 0, "Calendar"), ("CALSYNC_TAB_CALENDAR_SUB", "Tab 4 subtitle", 0, "notion")], None),
    ("Google Calendar (edit and drag)", "Google Calendar API", [
        ("GOOGLE_CLIENT_ID", "OAuth client ID", 0, "….apps.googleusercontent.com"),
        ("GOOGLE_CLIENT_SECRET", "OAuth client secret", 1, "GOCSPX-…")], None),
    ("Google Calendar (read-only link, simpler)", "Google Calendar", [
        ("GOOGLE_ICS_URLS", "Secret iCal address (several: separate with spaces)", 1, "https://calendar.google.com/calendar/ical/…/basic.ics")], None),
]
FIELDS = {k: s for _, _, fs, _ in SECTIONS for k, _, s, _ in fs}
CONFIG = Path(os.environ.get("CALSYNC_HOME") or Path(__file__).resolve().parent.parent) / "config" / "calsync.env"


def api_settings(_: dict) -> dict:
    def field(k, label, secret, ph):
        v = os.environ.get(k, "")
        f = {"key": k, "label": label, "secret": bool(secret), "placeholder": ph}
        if k == "CALSYNC_READER":
            return {**f, "value": cs.reader(), "choices": [[a, b] for a, b in cs.PROVIDERS.items()]}
        return {**f, "set": bool(v), "tail": v[-4:] if len(v) > 12 else ""} if secret else {**f, "value": v}
    return {"demo": DEMO, "file": str(CONFIG),
            "sections": [{"title": t, "test": test, "reader": rd, "fields": [field(*f) for f in fs]}
                         for t, test, fs, rd in SECTIONS]}


def write_env(updates: dict) -> None:
    lines = CONFIG.read_text(encoding="utf-8").splitlines() if CONFIG.exists() else []
    for k, v in updates.items():
        for i, line in enumerate(lines):
            if line.split("=", 1)[0].strip() == k and "=" in line and not line.lstrip().startswith("#"):
                lines[i] = f"{k}={v}"
                break
        else:
            lines.append(f"{k}={v}")
    CONFIG.parent.mkdir(exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG)
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass


def api_settings_save(body: dict) -> dict:
    updates = {}
    for k, v in (body.get("values") or {}).items():
        if k not in FIELDS:
            continue
        v = str(v).strip()
        if "\n" in v or "\r" in v:
            raise SystemExit(f"{k} can't contain line breaks.")
        if FIELDS[k] and not v:
            continue                      # blank secret = keep the saved one
        if k == "CALSYNC_READER" and v not in cs.PROVIDERS:
            raise SystemExit("Pick Claude, Gemini or Local.")
        if k == "NOTION_DATABASE_ID" and len(v) > 32:   # a pasted link: keep the 32-character id
            m = re.search(r"[0-9a-fA-F]{32}$", v.split("?")[0].replace("-", ""))
            if not m:
                raise SystemExit("Couldn't find a database ID in that link.")
            v = m.group().lower()
        if k == "GOOGLE_CLIENT_ID" and v:       # tolerate a pasted link; keep only the ID
            m = re.search(r"[0-9]+-[A-Za-z0-9_]+\.apps\.googleusercontent\.com", v)
            if not m:
                raise SystemExit("That isn't a Google client ID. It looks like 1234-abc….apps.googleusercontent.com")
            v = m.group()
        if k == "GOOGLE_CLIENT_SECRET" and not v.startswith("GOCSPX-"):
            raise SystemExit("That isn't a Google client secret. It starts with GOCSPX-. (The client ID goes in the box above.)")
        updates[k] = v
    if updates.get("CALSYNC_TIMEZONE"):
        try:
            tz = ZoneInfo(updates["CALSYNC_TIMEZONE"])
        except Exception:
            raise SystemExit("Unknown time zone. Use a name like America/New_York.")
        cs.TZ = tz
    elif "CALSYNC_TIMEZONE" in updates:
        updates["CALSYNC_TIMEZONE"] = "America/New_York"
        cs.TZ = ZoneInfo("America/New_York")
    write_env(updates)
    for k, v in updates.items():
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)
    return api_settings({})


def api_settings_test(body: dict) -> dict:
    name = body.get("name")
    if name not in cs.NEEDS:
        raise ValueError("Unknown service")
    try:
        return {"ok": True, "message": cs.test_connection(name)}
    except SystemExit as e:
        return {"ok": False, "message": str(e.code)}
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}


def page_card(p: dict) -> dict:
    """A Notion row as an editable card, with times shown in the app's time zone."""
    def local(s: str):
        if "T" not in s:
            return None
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(cs.TZ)
    s, e = local(p["start"]), local(p["end"]) if p["end"] else None
    return {**{k: p[k] for k in ("id", "source_id", "title", "type", "status", "people", "location", "notes")},
            "all_day": s is None,
            "date": p["start"][:10] if s is None else s.date().isoformat(),
            "start_time": s.strftime("%H:%M") if s else "",
            "end_time": e.strftime("%H:%M") if s and e else "",
            "end_date": e.date().isoformat() if e else ""}


def need_notion() -> str:
    if not (os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID")):
        raise SystemExit("Add your Notion token and database ID under Settings (gear, top right) first.")
    return os.environ["NOTION_DATABASE_ID"]


def api_cal_list(body: dict) -> dict:
    start = date.fromisoformat(body["from"])
    end = date.fromisoformat(body["to"])
    if DEMO:
        evs = [e for e in demo_events("family", False) if start <= e.start.date() < end]
        return {"events": [{**to_card(e, set()), "id": "demo-" + e.key()} for e in evs]}
    out = {"events": [page_card(p) for p in cs.list_pages(need_notion(), start, end)], "google": [], "google_error": ""}
    if cs.google_oauth_ready() or os.environ.get("GOOGLE_ICS_URLS"):
        try:
            out["google"] = cs.google_list_events(start, end) if cs.google_oauth_ready() else cs.google_events(start, end)
        except SystemExit as e:                  # a Google problem must not hide the Notion events
            out["google_error"] = str(e.code)
        except Exception as e:
            out["google_error"] = f"{type(e).__name__}: {e}"
    return out


def api_cal_save(body: dict) -> dict:
    card = body["card"]
    google = str(card.get("id", "")).startswith("gcal:") or card["type"] == "Google"
    if card["type"] not in ("Work", "Family", "Hangout") and not google:
        raise SystemExit("Type must be Work, Family or Hangout.")
    e = from_card(card)
    if DEMO:
        return {"ok": True}
    if google:
        if not cs.google_oauth_ready():
            raise SystemExit("Connect your Google account under Settings first.")
        cs.google_save(str(card.get("id", "")), e)
        return {"ok": True}
    if card.get("id"):
        cs.update_page(card["id"], e)
    else:
        e.source_id = "manual:" + secrets.token_hex(6)
        cs.create_page(need_notion(), e)
    return {"ok": True}


def api_google_connect(_: dict) -> dict:
    """Start Google sign-in: the page opens the returned address; Google sends the browser back to /oauth/google."""
    import urllib.parse
    if not (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")):
        raise SystemExit("Paste your Google OAuth client ID and secret into the two boxes first. "
                         "Open “How to get these” just above them for the steps.")
    cid = os.environ["GOOGLE_CLIENT_ID"]
    state = secrets.token_urlsafe(16)
    OAUTH_STATES.add(state)
    q = urllib.parse.urlencode({"client_id": cid, "redirect_uri": f"http://127.0.0.1:{PORT}/oauth/google",
                                "response_type": "code", "scope": cs.GOOGLE_SCOPE, "access_type": "offline",
                                "prompt": "consent", "state": state})
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + q}


def google_callback(query: str) -> tuple[int, str]:
    import urllib.parse
    q = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
    page = lambda msg: "<!doctype html><meta charset=utf-8><title>calsync</title><body style=\"font:18px system-ui;padding:40px\">" + msg
    if q.get("state") not in OAUTH_STATES:
        return 400, page("This sign-in link is not valid or has expired. Go back to calsync and press Connect again.")
    OAUTH_STATES.discard(q["state"])
    if "code" not in q:
        import html
        return 400, page("Google did not allow the sign-in: " + html.escape(q.get("error", "no code")) + ". You can close this tab.")
    try:
        r = cs.google_token_request({"code": q["code"], "client_id": cs.env("GOOGLE_CLIENT_ID"),
                                     "client_secret": cs.env("GOOGLE_CLIENT_SECRET"), "grant_type": "authorization_code",
                                     "redirect_uri": f"http://127.0.0.1:{PORT}/oauth/google"})
        if not r.get("refresh_token"):
            return 400, page("Google did not give a long-lived sign-in. Remove calsync at myaccount.google.com/permissions and press Connect again.")
        write_env({"GOOGLE_REFRESH_TOKEN": r["refresh_token"]})
        os.environ["GOOGLE_REFRESH_TOKEN"] = r["refresh_token"]
        cs._GTOK["exp"] = 0
    except SystemExit as e:
        return 400, page(str(e.code))
    return 200, page("<b>Google connected.</b> You can close this tab and go back to calsync.")


def api_cal_delete(body: dict) -> dict:
    if str(body["id"]).startswith("gcal:"):
        cs.google_delete(body["id"])
    elif not DEMO:
        cs.archive_page(body["id"])
    return {"ok": True}


def api_update_check(body: dict) -> dict:
    return update.check(fresh=bool(body.get("fresh")))


def api_update_install(_: dict) -> dict:
    if DEMO:
        return {"ok": False, "message": "Updates are off in demo mode."}
    return update.install()


ROUTES = {"/api/update/check": api_update_check, "/api/update/install": api_update_install,
          "/api/google/connect": api_google_connect, "/api/calendar": api_cal_list, "/api/calendar/save": api_cal_save, "/api/calendar/delete": api_cal_delete,
          "/api/status": api_status, "/api/read": api_read, "/api/add": api_add,
          "/api/settings": api_settings, "/api/settings/save": api_settings_save,
          "/api/settings/test": api_settings_test}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            print("  " + fmt % args)

    def send(self, code: int, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/manifest.webmanifest":
            return self.send(200, (WEB / "manifest.webmanifest").read_bytes(), "application/manifest+json")
        if path == "/oauth/google":
            code, html = google_callback(self.path.partition("?")[2])
            return self.send(code, html.encode(), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            return self.send(200, (WEB / "favicon.ico").read_bytes(), "image/x-icon")
        if path == "/icon.png":
            return self.send(200, (WEB / "icon.png").read_bytes(), "image/png")
        self.send(404, {"error": "Not found"})

    def do_POST(self):
        route = ROUTES.get(self.path)
        if not route:
            return self.send(404, {"error": "Not found"})
        if not secrets.compare_digest(self.headers.get("X-Calsync-Token", ""), TOKEN):
            return self.send(403, {"error": "This link has expired. Open the newest link printed by app.py."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self.send(413, {"error": "Those files are too big. Try fewer photos at once."})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            self.send(200, route(body))
        except SystemExit as e:          # calsync reports problems with sys.exit("message")
            self.send(400, {"error": str(e.code)})
        except Exception as e:           # keep the server up and tell the page what went wrong
            self.send(500, {"error": f"{type(e).__name__}: {e}"})


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    global DEMO, TOKEN, PORT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--demo", action="store_true", help="sample data, nothing is sent or written")
    ap.add_argument("--local-only", action="store_true", help="only this computer can open it")
    args = ap.parse_args()
    DEMO = args.demo
    TOKEN = os.environ.get("CALSYNC_APP_TOKEN") or secrets.token_urlsafe(12)
    host = "127.0.0.1" if args.local_only else "0.0.0.0"
    PORT = args.port
    update.cleanup_after_update()
    server = ThreadingHTTPServer((host, args.port), Handler)
    ip = "127.0.0.1" if args.local_only else lan_ip()
    print(f"calsync is running{' in demo mode' if DEMO else ''}.")
    print(f"  On this computer: http://127.0.0.1:{args.port}/#t={TOKEN}")
    if not args.local_only:
        print(f"  On your phone:    http://{ip}:{args.port}/#t={TOKEN}   (same Wi-Fi)")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
