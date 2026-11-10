#!/usr/bin/env python3
"""calsync: put work shifts and past hangouts into Notion (and so Notion Calendar).

Commands
  shifts --file schedule.pdf|png|jpg|txt   read a schedule you provide
  shifts --email [--days 14]               search your inbox for schedule emails
  family --file fridge.jpg [--only-me]     read a photo of a hand-drawn family calendar
  hangouts --since 2026-10-01 [--until ..]  find past hangouts in Immich photos

Nothing is written until you confirm (or pass --yes). Items already in Notion
are skipped, so re-running is safe. Configuration comes from env vars; see README.md.
"""
from __future__ import annotations

import argparse
import base64
import email
import hashlib
import imaplib
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel

try:
    TZ = ZoneInfo(os.environ.get("CALSYNC_TIMEZONE", "America/New_York"))
except Exception:
    sys.exit(f"Unknown time zone {os.environ.get('CALSYNC_TIMEZONE', 'America/New_York')!r}. "
             "Use a name like America/New_York, and on Windows run: pip install tzdata")
NOTION_VERSION = "2022-06-28"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name} (see README.md).")
    return value


def http_json(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 60,
              retries: int = 4):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(2 * 2 ** attempt)  # 2s, 4s, 8s: busy servers usually recover
                continue
            sys.exit(f"{method} {url} failed: {e.code} {e.read().decode(errors='replace')[:500]}")
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(f"Could not reach {url}: {getattr(e, 'reason', e)}")


# ---------------------------------------------------------------- event model

class Event(BaseModel):
    title: str
    start: datetime
    end: datetime
    type: str            # "Work" or "Hangout"
    status: str          # "Confirmed" / "Done"
    people: str = ""
    location: str = ""
    notes: str = ""
    source_id: str = ""  # used to skip duplicates
    all_day: bool = False

    def key(self) -> str:
        return self.source_id or hashlib.sha1(
            f"{self.type}|{self.title}|{self.start.isoformat()}|{self.end.isoformat()}".encode()
        ).hexdigest()[:16]


# ---------------------------------------------------- shifts: Claude extraction

class Shift(BaseModel):
    title: str          # e.g. "Shift - Front desk"
    start: str          # local ISO 8601, e.g. 2026-11-12T09:00:00
    end: str
    location: str
    notes: str


class ShiftList(BaseModel):
    shifts: list[Shift]


EXTRACT_PROMPT = """Extract work shifts from the schedule(s) above. There may be several
schedules from different jobs, in any format: app screenshots, decorated roster
graphics, emails, PDFs or plain text.
Today is {today} ({weekday}). Timezone is {tz}.
The person goes by any of these names: {me}.

Whose shifts:
- If a schedule is the person's own view (e.g. "My Schedule", or every shift is
  labelled with their name), return all of its shifts.
- If it is a team roster listing several people, return only shifts where one
  of the person's names appears (first name alone is enough). If none of their
  names appear in a roster, return nothing from it - never guess.

Dates and times:
- Return local ISO 8601 times without an offset (YYYY-MM-DDTHH:MM:SS).
- If the year is missing, use the year that puts the date closest to today
  (schedules can be for past, current or upcoming weeks).
- Combine the day number with the month shown in the header; check it against
  the weekday column when both are shown.
- If am/pm is missing, infer it from context (e.g. "2:45 - 6pm" is afternoon).
- If an end time is missing or garbled, use the end time of the same shift slot
  on other days and say so in notes.
- If a shift ends after midnight, its end date is the next day.
- Ignore days off, blank days, totals, open/available shifts and anything that
  is not an assigned shift.

Fields:
- title: "Shift - <employer or site>" and the role if shown, e.g.
  "Shift - Corner Cafe (Barista)". Use the schedule's header or logo
  text for the employer.
- location: the site or address if shown, else "".
- notes: co-workers on the same shift (rosters only) and anything uncertain, else "".
- Return an empty list if there are no shifts for this person."""


def fill_prompt(template: str) -> str:
    today = date.today()
    return template.format(today=today.isoformat(), weekday=f"{today:%A}", tz=TZ.key,
                           me=os.environ.get("CALSYNC_MY_NAME", "the reader"))


PROVIDERS = {"claude": "Claude", "gemini": "Gemini", "local": "Local"}


def reader() -> str:
    """Which model reads the schedules and photos: claude, gemini or local (Ollama)."""
    r = os.environ.get("CALSYNC_READER", "claude").strip().lower()
    return r if r in PROVIDERS else "claude"


def ask_model(content_blocks: list[dict], prompt: str, schema: type[BaseModel]):
    return {"gemini": ask_gemini, "local": ask_local}.get(reader(), ask_claude)(content_blocks, prompt, schema)


def _parse(raw: str, schema: type[BaseModel], who: str):
    try:
        return schema.model_validate_json(raw)
    except ValueError:
        sys.exit(f"{who} did not return a usable answer. Try again, or use a clearer photo.")


def ask_gemini(content_blocks: list[dict], prompt: str, schema: type[BaseModel]):
    # Your chosen model first, then backups, so a busy or retired model doesn't stop you.
    chosen = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
    models = [chosen] + [m for m in ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite") if m != chosen]
    parts = [{"text": b["text"]} if b["type"] == "text" else
             {"inline_data": {"mime_type": b["source"]["media_type"], "data": b["source"]["data"]}}
             for b in content_blocks] + [{"text": prompt}]
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "responseJsonSchema": schema.model_json_schema()}}
    for i, model in enumerate(models):
        try:
            r = http_json("POST", f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          {"x-goog-api-key": env("GEMINI_API_KEY")}, body, timeout=60, retries=1)
            break
        except SystemExit as e:
            busy = any(s in str(e.code) for s in (" 429 ", " 500 ", " 503 ", " 404 ", "Could not reach"))
            if not busy or i == len(models) - 1:
                raise
            print(f"Gemini model {model} unavailable, trying {models[i + 1]}", file=sys.stderr)
    try:
        raw = r["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        sys.exit("Gemini could not read a schedule from that input.")
    return _parse(raw, schema, "Gemini")


def ask_local(content_blocks: list[dict], prompt: str, schema: type[BaseModel]):
    """Any OpenAI-compatible local server, e.g. Ollama at http://localhost:11434."""
    base = env("LOCAL_URL").rstrip("/")
    model = os.environ.get("LOCAL_MODEL") or "qwen2.5vl:7b"
    content = []
    for b in content_blocks:
        if b["type"] == "text":
            content.append({"type": "text", "text": b["text"]})
        elif b["type"] == "image":
            s = b["source"]
            content.append({"type": "image_url", "image_url": {"url": f"data:{s['media_type']};base64,{s['data']}"}})
        else:
            sys.exit("The local reader can't read PDFs. Use a photo or screenshot instead.")
    js = schema.model_json_schema()
    content.append({"type": "text", "text": prompt + "\n\nReply with JSON only, matching this schema:\n" + json.dumps(js)})
    r = http_json("POST", f"{base}/v1/chat/completions", {},
                  {"model": model, "messages": [{"role": "user", "content": content}],
                   "response_format": {"type": "json_schema", "json_schema": {"name": "result", "schema": js}}},
                  timeout=1800)   # shortcut: CPU-only models can take many minutes; no progress shown
    try:
        raw = r["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        sys.exit("The local model returned nothing.")
    return _parse(raw, schema, "The local model")


def ask_claude(content_blocks: list[dict], prompt: str, schema: type[BaseModel]):
    """Send the schedule(s) plus instructions to Claude and get back validated JSON."""
    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.beta.messages.parse(
            model="claude-opus-5-5",
            max_tokens=16000,
            output_config={"effort": "medium"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": [*content_blocks, {"type": "text", "text": prompt}]}],
            output_format=schema,
        )
    except anthropic.AuthenticationError:
        sys.exit("Claude API key is missing or invalid (set ANTHROPIC_API_KEY).")
    except anthropic.RateLimitError:
        sys.exit("Claude API rate limit hit; try again in a minute.")
    except anthropic.APIConnectionError:
        sys.exit("Could not reach the Claude API; check your internet connection.")
    except anthropic.APIStatusError as e:
        sys.exit(f"Claude API error {e.status_code}: {e.message}")

    if response.stop_reason == "refusal" or response.parsed_output is None:
        sys.exit("Claude could not read a schedule from that input.")
    return response.parsed_output


def extract_shifts(content_blocks: list[dict]) -> list[Event]:
    result = ask_model(content_blocks, fill_prompt(EXTRACT_PROMPT), ShiftList)
    events = []
    for s in result.shifts:
        start = datetime.fromisoformat(s.start).replace(tzinfo=TZ)
        end = datetime.fromisoformat(s.end).replace(tzinfo=TZ)
        if end <= start:
            end += timedelta(days=1)
        events.append(Event(title=s.title, start=start, end=end, type="Work",
                            status="Confirmed", location=s.location, notes=s.notes))
    return events


# ------------------------------------------- family: photo of the fridge calendar

class FamilyItem(BaseModel):
    title: str          # e.g. "Mia: Soccer practice"
    date: str           # YYYY-MM-DD
    start_time: str     # HH:MM (24h), or "" if no time is written
    end_time: str       # HH:MM (24h), or ""
    people: str         # who it is for, comma-separated; "Everyone" for whole family
    location: str
    notes: str
    unsure: bool        # true if any part was hard to read


class FamilyList(BaseModel):
    items: list[FamilyItem]


FAMILY_PROMPT = """The image(s) above are photos of a family's hand-drawn or handwritten
calendar (on a fridge, whiteboard or paper). List every event written on it.
Today is {today} ({weekday}). Timezone is {tz}.
The person running this is called: {me}. Other names belong to family members.

Reading the calendar:
- Work out the month and year from the header; if none is written, use the
  month that best fits today and the weekday layout. Check day numbers against
  the weekday columns.
- Read handwriting carefully. Common shorthand: "appt" appointment, "drs"
  doctor's, "prac" practice, "bday" birthday, "pu" pick-up, "do" drop-off.
  Initials or colors may stand for people; use a legend if one is drawn.
- Arrows or lines across several days mean the event repeats on each of those
  days; list each day separately.
- Ignore doodles, stickers and crossed-out items.

Fields:
- title: "<Person>: <what>" for one person (e.g. "Mia: Dentist"), just "<what>"
  for the whole family (e.g. "Grandma's for dinner"). Expand shorthand.
- date: YYYY-MM-DD.
- start_time / end_time: 24h HH:MM. Use "" when no time is written (all-day).
  If only a start is written, leave end_time "".
- people: who the event is for, comma-separated, or "Everyone".
- notes: anything extra written, plus your best reading of anything unclear.
- unsure: true if you had to guess any word, date or time.
- Return an empty list if you cannot find a calendar in the image."""


def extract_family(content_blocks: list[dict], only_me: bool) -> list[Event]:
    result = ask_model(content_blocks, fill_prompt(FAMILY_PROMPT), FamilyList)
    names = {n.strip().lower() for n in os.environ.get("CALSYNC_MY_NAME", "").split(",") if n.strip()}
    events = []
    for it in result.items:
        who = {p.strip().lower() for p in it.people.split(",")}
        if only_me and not (who & names or "everyone" in who):
            continue
        day = date.fromisoformat(it.date)
        if it.start_time:
            start = datetime.combine(day, datetime.strptime(it.start_time, "%H:%M").time(), TZ)
            end = (datetime.combine(day, datetime.strptime(it.end_time, "%H:%M").time(), TZ)
                   if it.end_time else start + timedelta(hours=1))
            if end <= start:
                end += timedelta(days=1)
            all_day = False
        else:
            start = end = datetime.combine(day, datetime.min.time(), TZ)
            all_day = True
        notes = ("[check: handwriting was unclear] " if it.unsure else "") + it.notes
        events.append(Event(title=it.title, start=start, end=end, type="Family", status="Confirmed",
                            people=it.people, location=it.location, notes=notes.strip(), all_day=all_day))
    return events


# ------------------------------------------ describe: a sentence becomes events

class TextItem(BaseModel):
    title: str
    date: str           # YYYY-MM-DD
    start_time: str     # HH:MM (24h), or "" for an all-day event
    end_time: str       # HH:MM (24h), or ""
    type: str           # Work, Family or Hangout
    people: str
    location: str
    notes: str


class TextList(BaseModel):
    items: list[TextItem]


TEXT_PROMPT = """Turn the description above into calendar events.
Today is {today} ({weekday}). Timezone is {tz}. The person writing is called: {me}.

- Work out real dates from relative words ("tomorrow", "next Tuesday", "the 3rd").
  "Next <weekday>" means the first such weekday after today.
- A repeating event ("every Monday for 4 weeks") becomes one item per day, at most 60 items.
- date: YYYY-MM-DD. start_time / end_time: 24h HH:MM. If only a start time is given, leave
  end_time "" (it will default to 1 hour).
- A part of the day is a time, not all-day: morning 09:00-12:00, lunch 12:00-13:00,
  afternoon 13:00-17:00, evening 18:00-21:00, night 21:00-23:00. Put what you used in notes.
- Use "" for both times (all-day) only when the text gives no time of day at all, or says all day.
- type: "Work" for jobs, shifts and work meetings; "Hangout" only for the writer socialising
  with friends (meals out, parties, trips); otherwise "Family" (home, personal, appointments,
  children's activities and sports, errands).
- title: short and clear, e.g. "Dentist" or "Mia: Soccer practice". Do not put the time in the title.
- people: who it involves (comma-separated), else "". location: place if mentioned, else "".
- notes: anything else worth keeping, else "".
- Do not invent details. If there is no event in the text, return an empty list."""


def extract_text(text: str) -> list[Event]:
    text = text.strip()
    if not text:
        sys.exit("Type what is happening first, for example: Dentist next Tuesday at 3pm.")
    result = ask_model([{"type": "text", "text": "--- Description ---\n" + text[:4000]}],
                       fill_prompt(TEXT_PROMPT), TextList)
    events = []
    for it in result.items[:60]:
        try:
            day = date.fromisoformat(it.date)
            if it.start_time:
                start = datetime.combine(day, datetime.strptime(it.start_time, "%H:%M").time(), TZ)
                end = (datetime.combine(day, datetime.strptime(it.end_time, "%H:%M").time(), TZ)
                       if it.end_time else start + timedelta(hours=1))
                if end <= start:
                    end += timedelta(days=1)
                all_day = False
            else:
                start = end = datetime.combine(day, datetime.min.time(), TZ)
                all_day = True
        except ValueError:
            continue                               # the AI gave an unreadable date or time: skip that item
        kind = it.type if it.type in ("Work", "Family", "Hangout") else "Family"
        events.append(Event(title=it.title, start=start, end=end, type=kind, status="Confirmed",
                            people=it.people, location=it.location, notes=it.notes, all_day=all_day))
    return events


def image_block(path: Path) -> dict:
    """Load a photo, fix phone rotation and shrink it so it fits the API's size limits."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        Image = None
    if path.suffix.lower() in (".heic", ".heif"):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            sys.exit("iPhone .HEIC photo: run 'pip install pillow pillow-heif', or export it as JPEG.")
    if Image is None:
        mime = mimetypes.guess_type(path.name)[0]
        data = path.read_bytes()
    else:
        import io
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        img.thumbnail((2400, 2400))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
        mime, data = "image/jpeg", buf.getvalue()
    return {"type": "image", "source": {"type": "base64", "media_type": mime,
                                        "data": base64.standard_b64encode(data).decode()}}


def blocks_from_file(path: Path) -> list[dict]:
    mime = mimetypes.guess_type(path.name)[0] or "text/plain"
    if mime == "application/pdf":
        data = base64.standard_b64encode(path.read_bytes()).decode()
        return [{"type": "document", "source": {"type": "base64", "media_type": mime, "data": data}}]
    if mime in ("image/png", "image/jpeg", "image/gif", "image/webp") or path.suffix.lower() in (".heic", ".heif"):
        return [image_block(path)]
    return [{"type": "text", "text": path.read_text(errors="replace")}]


def blocks_from_email(days: int) -> list[dict]:
    """Search the inbox over IMAP for recent schedule emails."""
    host, user, password = env("IMAP_HOST"), env("IMAP_USER"), env("IMAP_PASSWORD")
    terms = [t.strip() for t in os.environ.get(
        "CALSYNC_EMAIL_TERMS", "schedule,shift,rota,roster").split(",") if t.strip()]
    since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")

    imap = imaplib.IMAP4_SSL(host)
    imap.login(user, password)
    imap.select("INBOX", readonly=True)
    ids: set[bytes] = set()
    sender = os.environ.get("CALSYNC_EMAIL_FROM")
    for term in terms:
        criteria = ["SINCE", since, "SUBJECT", f'"{term}"']
        if sender:
            criteria += ["FROM", f'"{sender}"']
        _, found = imap.search(None, *criteria)
        ids.update(found[0].split())

    blocks = []
    for msg_id in sorted(ids):
        _, parts = imap.fetch(msg_id, "(RFC822)")
        msg = email.message_from_bytes(parts[0][1])
        subject = str(make_header(decode_header(msg.get("Subject", ""))))
        print(f"  found email: {subject}")
        blocks.append({"type": "text", "text": f"--- Email: {subject} (sent {msg.get('Date')}) ---"})
        for part in msg.walk():
            ctype = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            if ctype == "text/plain":
                blocks.append({"type": "text", "text": payload.decode(errors="replace")})
            elif ctype == "text/html" and not any(p.get_content_type() == "text/plain" for p in msg.walk()):
                blocks.append({"type": "text", "text": payload.decode(errors="replace")})
            elif ctype == "application/pdf" or ctype.startswith("image/"):
                blocks.append({"type": "document" if ctype == "application/pdf" else "image",
                               "source": {"type": "base64", "media_type": ctype,
                                          "data": base64.standard_b64encode(payload).decode()}})
    imap.logout()
    return blocks


# --------------------------------------------------- hangouts: Immich photos

def immich_assets(since: date, until: date) -> list[dict]:
    base, key = env("IMMICH_URL").rstrip("/"), env("IMMICH_API_KEY")
    assets, page = [], 1
    while page:
        body = {"takenAfter": f"{since.isoformat()}T00:00:00Z",
                "takenBefore": f"{until.isoformat()}T23:59:59Z",
                "withExif": True, "withPeople": True, "size": 500, "page": page}
        result = http_json("POST", f"{base}/api/search/metadata", {"x-api-key": key}, body)
        assets += result["assets"]["items"]
        page = int(result["assets"]["nextPage"]) if result["assets"].get("nextPage") else None
    return assets


def cluster_hangouts(assets: list[dict], gap_hours: float, min_photos: int,
                     require_people: bool) -> list[Event]:
    me = {n.strip().lower() for n in os.environ.get("CALSYNC_MY_NAME", "").split(",") if n.strip()}

    def taken(a):
        ts = (a.get("exifInfo") or {}).get("dateTimeOriginal") or a["fileCreatedAt"]
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ)

    assets = sorted((a for a in assets if a.get("type") == "IMAGE" or a.get("type") == "VIDEO"), key=taken)
    groups, current = [], []
    for a in assets:
        if current and taken(a) - taken(current[-1]) > timedelta(hours=gap_hours):
            groups.append(current)
            current = []
        current.append(a)
    if current:
        groups.append(current)

    events = []
    for g in groups:
        if len(g) < min_photos:
            continue
        people = sorted({p["name"] for a in g for p in a.get("people") or []
                         if p.get("name") and p["name"].strip().lower() not in me})
        if require_people and not people:
            continue
        places = [", ".join(x for x in ((a.get("exifInfo") or {}).get("city"),
                                         (a.get("exifInfo") or {}).get("state")) if x) for a in g]
        places = [p for p in places if p]
        location = max(set(places), key=places.count) if places else ""
        start, end = taken(g[0]), taken(g[-1])
        end = max(end, start + timedelta(hours=1))
        if people:
            title = "Hangout with " + (", ".join(people[:3]) + (f" +{len(people) - 3}" if len(people) > 3 else ""))
        else:
            title = f"Photos in {location}" if location else "Photo outing"
        events.append(Event(title=title, start=start, end=end, type="Hangout", status="Done",
                            people=", ".join(people), location=location,
                            notes=f"{len(g)} photos in Immich", source_id=f"immich:{g[0]['id']}"))
    return events


# ---------------------------------------------------------------- Notion write

def notion_headers() -> dict:
    return {"Authorization": f"Bearer {env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION}


def existing_keys(db: str) -> set[str]:
    keys, cursor = set(), None
    while True:
        body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        result = http_json("POST", f"https://api.notion.com/v1/databases/{db}/query", notion_headers(), body)
        for page in result["results"]:
            texts = page["properties"].get("Source ID", {}).get("rich_text", [])
            if texts:
                keys.add("".join(t["plain_text"] for t in texts))
        if not result.get("has_more"):
            return keys
        cursor = result["next_cursor"]


def text(value: str) -> dict:
    return {"rich_text": [{"text": {"content": value[:2000]}}] if value else []}


def page_props(e: Event) -> dict:
    return {
        "Name": {"title": [{"text": {"content": e.title}}]},
        "Date": {"date": {"start": e.start.date().isoformat(), "end": None} if e.all_day
                 else {"start": e.start.isoformat(), "end": e.end.isoformat()}},
        "Type": {"select": {"name": e.type}},
        "Status": {"select": {"name": e.status}},
        "People": text(e.people),
        "Location": text(e.location),
        "Notes": text(e.notes),
    }


def create_page(db: str, e: Event) -> None:
    props = {**page_props(e), "Source ID": text(e.key())}
    http_json("POST", "https://api.notion.com/v1/pages", notion_headers(),
              {"parent": {"database_id": db}, "properties": props})


def _page_id(page_id: str) -> str:
    """A Notion page id is 32 hex digits (with or without dashes). Anything else is refused before it reaches a URL."""
    if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", str(page_id)):
        sys.exit("That isn't a valid Notion page id.")
    return page_id


def update_page(page_id: str, e: Event) -> None:
    """Edit an existing event. Source ID is left alone so duplicate protection still works."""
    _page_id(page_id)
    http_json("PATCH", f"https://api.notion.com/v1/pages/{page_id}", notion_headers(), {"properties": page_props(e)})


def archive_page(page_id: str) -> None:
    """Notion's delete: moves the page to Notion's trash, where it can be restored."""
    _page_id(page_id)
    http_json("PATCH", f"https://api.notion.com/v1/pages/{page_id}", notion_headers(), {"archived": True})


def list_pages(db: str, start: date, end: date) -> list[dict]:
    """Events in [start, end), oldest first, as plain dicts."""
    out, cursor = [], None
    flt = {"and": [{"property": "Date", "date": {"on_or_after": start.isoformat()}},
                   {"property": "Date", "date": {"before": end.isoformat()}}]}
    while True:
        body = {"page_size": 100, "filter": flt, "sorts": [{"property": "Date", "direction": "ascending"}],
                **({"start_cursor": cursor} if cursor else {})}
        r = http_json("POST", f"https://api.notion.com/v1/databases/{db}/query", notion_headers(), body)
        for pg in r["results"]:
            p = pg["properties"]
            plain = lambda k: "".join(t["plain_text"] for t in p.get(k, {}).get("rich_text", []))
            d = (p.get("Date", {}).get("date") or {})
            if not d.get("start"):
                continue
            out.append({"id": pg["id"], "source_id": plain("Source ID"),
                        "title": "".join(t["plain_text"] for t in p.get("Name", {}).get("title", [])) or "Untitled",
                        "type": (p.get("Type", {}).get("select") or {}).get("name", ""),
                        "status": (p.get("Status", {}).get("select") or {}).get("name", ""),
                        "people": plain("People"), "location": plain("Location"), "notes": plain("Notes"),
                        "start": d["start"], "end": d.get("end") or ""})
        if not r.get("has_more"):
            return out
        cursor = r["next_cursor"]


# ------------------------------------------- Google Calendar (read-only, via iCal link)

_ICS: dict[str, tuple[float, object]] = {}


def google_events(start: date, end: date, fresh: bool = False) -> list[dict]:
    """Events from Google's private iCal address(es), recurring ones expanded, as read-only cards.
    No sign-in needed: the secret address is the key. Cached for 5 minutes."""
    import icalendar
    import recurring_ical_events
    urls = [u for u in os.environ.get("GOOGLE_ICS_URLS", "").replace(",", " ").split() if u]
    for u in urls:
        if not u.lower().startswith("https://"):     # never fetch file:// or other local things from a typed address
            sys.exit("The calendar address must start with https://")
    out = []
    for n, url in enumerate(urls):
        hit = _ICS.get(url)
        if fresh or not hit or time.time() - hit[0] > 300:
            req = urllib.request.Request(url, headers={"User-Agent": "calsync"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    cal = icalendar.Calendar.from_ical(r.read())
            except urllib.error.HTTPError as e:
                sys.exit(f"Google said {e.code}. Copy the secret iCal address again; if you reset it in Google, the old one stops working.")
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                sys.exit(f"Couldn't read that calendar address: {getattr(e, 'reason', e)}")
            _ICS[url] = hit = (time.time(), cal)
        name = str(hit[1].get("X-WR-CALNAME", "Google"))
        for ev in recurring_ical_events.of(hit[1]).between(start, end):
            s = ev["DTSTART"].dt
            e = ev["DTEND"].dt if "DTEND" in ev else s
            allday = not isinstance(s, datetime)
            if not allday:
                s = (s.astimezone(TZ) if s.tzinfo else s.replace(tzinfo=TZ))
                e = (e.astimezone(TZ) if e.tzinfo else e.replace(tzinfo=TZ)) if isinstance(e, datetime) else s
            else:
                e = e - timedelta(days=1) if e > s else e      # Google's all-day end is the next day
            out.append({"id": f"g{n}:{ev.get('UID', '')}:{s}", "readonly": True, "source": name,
                        "title": str(ev.get("SUMMARY", "(no title)")), "type": "Google", "status": "", "people": "",
                        "location": str(ev.get("LOCATION", "")), "notes": "",
                        "all_day": allday, "date": s.date().isoformat() if not allday else s.isoformat(),
                        "start_time": "" if allday else s.strftime("%H:%M"),
                        "end_time": "" if allday else e.strftime("%H:%M"),
                        "end_date": e.date().isoformat() if not allday else e.isoformat()})
    return sorted(out, key=lambda c: (c["date"], c["start_time"]))


# ------------------------------------- Google Calendar with sign-in (read and edit)
# The user makes a Google "Desktop app" OAuth client once (client id + secret), then presses
# Connect in Settings. calsync keeps only a refresh token and asks Google for short access tokens.

GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar"
_GTOK = {"t": "", "exp": 0.0}


def google_oauth_ready() -> bool:
    return all(os.environ.get(k) for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"))


def google_token_request(fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        why = e.read().decode(errors="replace")[:300]
        sys.exit("Google refused the sign-in. Press Connect Google account in Settings again. "
                 "(If your Google Cloud app is still in Testing, sign-ins expire after 7 days; set it to In production.) " + why)
    except (urllib.error.URLError, TimeoutError) as e:
        sys.exit(f"Could not reach Google: {getattr(e, 'reason', e)}")


def google_access_token() -> str:
    if time.time() < _GTOK["exp"]:
        return _GTOK["t"]
    r = google_token_request({"client_id": env("GOOGLE_CLIENT_ID"), "client_secret": env("GOOGLE_CLIENT_SECRET"),
                              "refresh_token": env("GOOGLE_REFRESH_TOKEN"), "grant_type": "refresh_token"})
    _GTOK.update(t=r["access_token"], exp=time.time() + int(r.get("expires_in", 3600)) - 120)
    return _GTOK["t"]


def google_api(method: str, path: str, body: dict | None = None, params: dict | None = None):
    url = "https://www.googleapis.com/calendar/v3" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    return http_json(method, url, {"Authorization": "Bearer " + google_access_token()}, body)


def _gid(cal: str, ev: str) -> str:
    return f"gcal:{urllib.parse.quote(cal, safe='')}:{ev}"


def _split_gid(gid: str) -> tuple[str, str]:
    cal, ev = gid[5:].split(":", 1)       # the calendar part is url-quoted, so it never contains a colon
    if not re.fullmatch(r"[A-Za-z0-9_:.\-]+", ev):
        sys.exit("That isn't a valid Google event id.")
    return urllib.parse.unquote(cal), ev


def google_list_events(start: date, end: date) -> list[dict]:
    out = []
    cals = google_api("GET", "/users/me/calendarList", params={"minAccessRole": "reader"}).get("items", [])
    lo = datetime.combine(start, datetime.min.time(), TZ).isoformat()
    hi = datetime.combine(end, datetime.min.time(), TZ).isoformat()
    for cal in cals:
        if not cal.get("selected", False):
            continue
        items = google_api("GET", f"/calendars/{urllib.parse.quote(cal['id'], safe='')}/events",
                           params={"timeMin": lo, "timeMax": hi, "singleEvents": "true", "orderBy": "startTime",
                                   "maxResults": 250}).get("items", [])
        for ev in items:
            if ev.get("status") == "cancelled" or "start" not in ev:
                continue
            allday = "date" in ev["start"]
            if allday:
                s = date.fromisoformat(ev["start"]["date"])
                e = date.fromisoformat(ev["end"]["date"]) - timedelta(days=1)
                e = max(e, s)
            else:
                s = datetime.fromisoformat(ev["start"]["dateTime"]).astimezone(TZ)
                e = datetime.fromisoformat(ev["end"]["dateTime"]).astimezone(TZ)
            out.append({"id": _gid(cal["id"], ev["id"]), "source": cal.get("summaryOverride") or cal.get("summary", "Google"),
                        "readonly": cal.get("accessRole") not in ("owner", "writer"),
                        "title": ev.get("summary", "(no title)"), "type": "Google", "status": "", "people": "",
                        "location": ev.get("location", ""), "notes": (ev.get("description") or "").strip()[:500],
                        "all_day": allday, "date": s.isoformat() if allday else s.date().isoformat(),
                        "start_time": "" if allday else s.strftime("%H:%M"),
                        "end_time": "" if allday else e.strftime("%H:%M"),
                        "end_date": e.isoformat() if allday else e.date().isoformat()})
    return sorted(out, key=lambda c: (c["date"], c["start_time"]))


def google_save(card_id: str, e: Event) -> None:
    """Create (no id) or change (gcal: id) an event. Times are sent with the app's time zone."""
    if e.all_day:
        day = e.start.date()
        when = {"start": {"date": day.isoformat()}, "end": {"date": (day + timedelta(days=1)).isoformat()}}
    else:
        when = {"start": {"dateTime": e.start.isoformat(), "timeZone": TZ.key},
                "end": {"dateTime": e.end.isoformat(), "timeZone": TZ.key}}
    body = {"summary": e.title, "location": e.location, "description": e.notes, **when}
    if card_id.startswith("gcal:"):
        cal, ev = _split_gid(card_id)
        google_api("PATCH", f"/calendars/{urllib.parse.quote(cal, safe='')}/events/{ev}", body)
    else:
        google_api("POST", "/calendars/primary/events", body)


def google_delete(card_id: str) -> None:
    cal, ev = _split_gid(card_id)
    google_api("DELETE", f"/calendars/{urllib.parse.quote(cal, safe='')}/events/{ev}")


# ------------------------------------------------------- connection tests

NEEDS = {"Claude": ["ANTHROPIC_API_KEY"], "Gemini": ["GEMINI_API_KEY"], "Local": ["LOCAL_URL"], "Notion": ["NOTION_TOKEN", "NOTION_DATABASE_ID"],
         "Immich": ["IMMICH_URL", "IMMICH_API_KEY"], "Email": ["IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"],
         "Google Calendar": ["GOOGLE_ICS_URLS"],
         "Google Calendar API": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"]}
NOTION_PROPS = {"Name": "title", "Date": "date", "Type": "select", "Status": "select",
                "People": "rich_text", "Location": "rich_text", "Notes": "rich_text", "Source ID": "rich_text"}


def test_connection(name: str) -> str:
    """Try one service for real. Returns a short success note; raises or sys.exit()s with the problem."""
    missing = [k for k in NEEDS[name] if not os.environ.get(k)]
    if missing and name == "Google Calendar API":
        sys.exit("Paste the client ID and secret, press Connect Google account and sign in, then try again."
                 if "GOOGLE_REFRESH_TOKEN" in missing and len(missing) == 1 else
                 "Paste your OAuth client ID and secret first (see “How to get these”).")
    if missing:
        sys.exit("Fill in " + ", ".join(missing) + " first.")
    if name == "Claude":
        import anthropic
        try:
            anthropic.Anthropic().messages.create(model="claude-haiku-5-5", max_tokens=5,
                                                  messages=[{"role": "user", "content": "hi"}])
        except anthropic.AuthenticationError:
            sys.exit("Claude rejected that API key.")
        except anthropic.APIConnectionError:
            sys.exit("Could not reach the Claude API; check the internet connection.")
        except anthropic.APIStatusError as e:
            sys.exit(f"Claude API error {e.status_code}: {e.message}")
        return "Key works."
    if name == "Gemini":
        model = os.environ.get("GEMINI_MODEL") or "gemini-3.6-flash"
        http_json("GET", f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                  {"x-goog-api-key": os.environ["GEMINI_API_KEY"]})
        return f"Key works; model {model} found."
    if name == "Local":
        model = os.environ.get("LOCAL_MODEL") or "qwen2.5vl:7b"
        have = [m["name"] for m in http_json("GET", os.environ["LOCAL_URL"].rstrip("/") + "/api/tags", {}).get("models", [])]
        if not any(h == model or h.split(":")[0] == model for h in have):
            sys.exit(f"Connected, but {model} isn't downloaded. Run: ollama pull {model}")
        return f"Connected; {model} is ready."
    if name == "Notion":
        try:
            info = http_json("GET", f"https://api.notion.com/v1/databases/{os.environ['NOTION_DATABASE_ID']}", notion_headers())
        except SystemExit as e:
            if "object_not_found" in str(e.code):
                sys.exit("Notion can't find that database. Check that the link is the database's own (not the integration's), "
                         "and that the database has your integration under ⋯ > Connections.")
            raise
        bad = [f"{k} ({t})" for k, t in NOTION_PROPS.items() if info["properties"].get(k, {}).get("type") != t]
        if bad:
            sys.exit("Connected, but the database needs these columns: " + ", ".join(bad))
        return f"Connected to “{''.join(t['plain_text'] for t in info['title'])}”; all 8 columns present."
    if name == "Immich":
        base = os.environ["IMMICH_URL"].rstrip("/")
        return "Signed in as " + str(http_json("GET", f"{base}/api/users/me", {"x-api-key": os.environ["IMMICH_API_KEY"]}).get("email"))
    if name == "Google Calendar API":
        cals = google_api("GET", "/users/me/calendarList", params={"minAccessRole": "reader"}).get("items", [])
        return f"Signed in; {len(cals)} calendars found: " + ", ".join(c.get("summary", "?") for c in cals[:4])
    if name == "Google Calendar":
        found = google_events(date.today(), date.today() + timedelta(days=30), fresh=True)
        return f"Connected; {len(found)} events in the next 30 days."
    try:
        m = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], timeout=20)
        m.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
        m.logout()
    except (imaplib.IMAP4.error, OSError) as e:
        sys.exit(f"Email login failed: {e}")
    return "Login works."


# ---------------------------------------------------------------------- main

def show(events: list[Event]) -> None:
    for i, e in enumerate(events, 1):
        when = f"{e.start:%a %b %d}  all day           " if e.all_day else f"{e.start:%a %b %d  %I:%M %p} - {e.end:%I:%M %p}"
        extra = " | ".join(x for x in (e.people, e.location) if x)
        print(f"{i:>3}. {when}  {e.title}" + (f"  ({extra})" if extra else ""))
        if e.notes and e.type in ("Work", "Family"):
            print(f"       note: {e.notes}")
    ordered = sorted(((i, e) for i, e in enumerate(events, 1) if not e.all_day), key=lambda x: x[1].start)
    for (i, a), (j, b) in zip(ordered, ordered[1:]):
        if b.start < a.end:
            print(f"  ! #{i} and #{j} overlap ({a.title} / {b.title})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("shifts", help="add work shifts")
    src = s.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, nargs="+", help="one or more schedules as PDF, image or text")
    src.add_argument("--email", action="store_true", help="search your inbox")
    s.add_argument("--days", type=int, default=14, help="how far back to search email")
    f = sub.add_parser("family", help="read a photo of the family calendar")
    f.add_argument("--file", type=Path, nargs="+", required=True, help="photo(s) of the calendar")
    f.add_argument("--only-me", action="store_true", help="keep only your events and whole-family ones")
    h = sub.add_parser("hangouts", help="log past hangouts from Immich photos")
    h.add_argument("--since", type=date.fromisoformat, required=True)
    h.add_argument("--until", type=date.fromisoformat, default=date.today())
    h.add_argument("--gap-hours", type=float, default=2.0, help="new hangout after this long with no photos")
    h.add_argument("--min-photos", type=int, default=3)
    h.add_argument("--include-solo", action="store_true", help="also keep groups with no named people")
    for p in (s, f, h):
        p.add_argument("--yes", action="store_true", help="add without asking")
        p.add_argument("--dry-run", action="store_true", help="only show what would be added")
    args = ap.parse_args()

    if args.cmd == "shifts":
        if args.file:
            blocks = []
            for f in args.file:
                blocks += [{"type": "text", "text": f"--- Schedule file: {f.name} ---"}, *blocks_from_file(f)]
        else:
            blocks = blocks_from_email(args.days)
        if not blocks:
            sys.exit("No matching schedule emails found.")
        events = extract_shifts(blocks)
    elif args.cmd == "family":
        blocks = []
        for path in args.file:
            blocks += blocks_from_file(path)
        events = extract_family(blocks, args.only_me)
    else:
        events = cluster_hangouts(immich_assets(args.since, args.until),
                                  args.gap_hours, args.min_photos, not args.include_solo)

    if not args.dry_run:
        db = env("NOTION_DATABASE_ID")
        seen = existing_keys(db)
        skipped = [e for e in events if e.key() in seen]
        events = [e for e in events if e.key() not in seen]
        if skipped:
            print(f"Skipping {len(skipped)} already in Notion.")
    if not events:
        print("Nothing new to add.")
        return
    show(events)
    if args.dry_run:
        return
    if not args.yes:
        answer = input(f"\nAdd these {len(events)} to Notion? Enter 'y' for all, numbers like '1 3 4', or 'n': ").strip().lower()
        if answer in ("", "n", "no"):
            return
        if answer not in ("y", "yes"):
            keep = {int(x) for x in answer.replace(",", " ").split() if x.isdigit()}
            events = [e for i, e in enumerate(events, 1) if i in keep]
    for e in events:
        create_page(db, e)
        print(f"  added: {e.title} ({e.start:%b %d})")
    print("Done. They'll show up in Notion Calendar shortly.")


if __name__ == "__main__":
    main()
