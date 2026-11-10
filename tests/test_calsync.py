"""Tests that run without any account, key or network. Run:  python -m unittest discover -s tests -v

They cover the logic most likely to hurt: dates and times, settings validation, secrets handling,
Google calendar parsing, the describe step, and the updater's safety checks. They do NOT cover
real Notion / Google / Gemini / Immich calls: those need your accounts (see README, "Not tested").
"""
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("CALSYNC_TIMEZONE", "America/New_York")

import calsync as cs  # noqa: E402
import app  # noqa: E402

TZ = ZoneInfo("America/New_York")


class Cards(unittest.TestCase):
    def test_timed_card_roundtrip(self):
        c = {"title": "Dentist", "date": "2026-11-17", "start_time": "15:00", "end_time": "16:00", "type": "Family"}
        e = app.from_card(c)
        self.assertFalse(e.all_day)
        self.assertEqual((e.start.hour, e.end.hour), (15, 16))
        self.assertEqual(app.to_card(e, set())["start_time"], "15:00")

    def test_all_day_when_no_start_time(self):
        e = app.from_card({"title": "Birthday", "date": "2026-11-20", "start_time": "", "end_time": "", "type": "Family"})
        self.assertTrue(e.all_day)

    def test_end_before_start_means_next_day(self):
        e = app.from_card({"title": "Night shift", "date": "2026-11-02", "start_time": "22:00", "end_time": "06:00", "type": "Work"})
        self.assertEqual(e.end.date(), date(2026, 11, 3))

    def test_missing_end_defaults_to_one_hour(self):
        e = app.from_card({"title": "x", "date": "2026-11-02", "start_time": "09:00", "type": "Family"})
        self.assertEqual((e.end - e.start).seconds, 3600)


class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = app.CONFIG
        app.CONFIG = Path(self.tmp.name) / "config" / "calsync.env"
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        app.CONFIG = self.old
        self.tmp.cleanup()

    def save(self, **values):
        return app.api_settings_save({"values": values})

    def test_blank_secret_keeps_saved_one(self):
        self.save(GEMINI_API_KEY="abc123")
        self.save(GEMINI_API_KEY="", GEMINI_MODEL="m")
        self.assertIn("GEMINI_API_KEY=abc123", app.CONFIG.read_text())

    def test_unknown_keys_are_ignored(self):
        self.save(NOT_A_SETTING="x", GEMINI_MODEL="m")
        self.assertNotIn("NOT_A_SETTING", app.CONFIG.read_text())

    def test_line_breaks_rejected(self):
        with self.assertRaises(SystemExit):
            self.save(GEMINI_MODEL="a\nEVIL=1")

    def test_notion_link_is_trimmed_to_id(self):
        self.save(NOTION_DATABASE_ID="https://www.notion.so/Work-DB-0123456789abcdef0123456789abcdef?v=1")
        self.assertIn("NOTION_DATABASE_ID=0123456789abcdef0123456789abcdef", app.CONFIG.read_text())

    def test_google_client_id_link_is_trimmed_and_bad_values_rejected(self):
        self.save(GOOGLE_CLIENT_ID="https://123-abc_9.apps.googleusercontent.com/")
        self.assertIn("GOOGLE_CLIENT_ID=123-abc_9.apps.googleusercontent.com", app.CONFIG.read_text())
        with self.assertRaises(SystemExit):
            self.save(GOOGLE_CLIENT_ID="not an id")
        with self.assertRaises(SystemExit):
            self.save(GOOGLE_CLIENT_SECRET="https://example.com")

    def test_bad_time_zone_rejected(self):
        with self.assertRaises(SystemExit):
            self.save(CALSYNC_TIMEZONE="Mars/Base")

    def test_secrets_never_sent_back_to_the_page(self):
        self.save(GEMINI_API_KEY="supersecretvalue1234")
        body = json.dumps(app.api_settings({}))
        self.assertNotIn("supersecretvalue1234", body)


class GoogleIcs(unittest.TestCase):
    ICS = (b"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nX-WR-CALNAME:Personal\n"
           b"BEGIN:VEVENT\nUID:a\nDTSTART;TZID=America/New_York:20261102T180000\nDTEND;TZID=America/New_York:20261102T190000\n"
           b"RRULE:FREQ=WEEKLY;BYDAY=MO\nSUMMARY:Gym\nEND:VEVENT\n"
           b"BEGIN:VEVENT\nUID:b\nDTSTART;VALUE=DATE:20261120\nDTEND;VALUE=DATE:20261121\nSUMMARY:Birthday\nEND:VEVENT\n"
           b"END:VCALENDAR\n")

    def fetch(self, url="https://example.com/x.ics"):
        cs._ICS.clear()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = self.ICS
        with mock.patch.dict(os.environ, {"GOOGLE_ICS_URLS": url}), mock.patch("urllib.request.urlopen", return_value=resp):
            return cs.google_events(date(2026, 11, 1), date(2026, 12, 1), fresh=True)

    def test_recurring_and_all_day(self):
        ev = self.fetch()
        gym = [e for e in ev if e["title"] == "Gym"]
        self.assertEqual([e["date"] for e in gym], ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"])
        self.assertEqual(gym[0]["start_time"], "18:00")
        b = [e for e in ev if e["title"] == "Birthday"][0]
        self.assertTrue(b["all_day"])
        self.assertEqual(b["date"], "2026-11-20")
        self.assertTrue(all(e["readonly"] for e in ev))

    def test_only_https_addresses(self):
        for bad in ("file:///C:/secret.ics", "http://example.com/x.ics", "ftp://x/y"):
            with self.assertRaises(SystemExit):
                self.fetch(bad)

    def test_google_ids_roundtrip(self):
        gid = cs._gid("me@gmail.com", "abc123")
        self.assertEqual(cs._split_gid(gid), ("me@gmail.com", "abc123"))
        gid = cs._gid("group.calendar.google.com#holiday@x", "ev:1")
        self.assertEqual(cs._split_gid(gid), ("group.calendar.google.com#holiday@x", "ev:1"))


class UrlSafety(unittest.TestCase):
    def test_notion_page_ids_are_validated(self):
        cs._page_id("01234567-89ab-cdef-0123-456789abcdef")
        for bad in ("../users", "abc", "0123456789abcdef0123456789abcdef/../x"):
            with self.assertRaises(SystemExit):
                cs._page_id(bad)

    def test_google_event_ids_are_validated(self):
        with self.assertRaises(SystemExit):
            cs._split_gid("gcal:me%40x.com:../../other")


class Describe(unittest.TestCase):
    def run_with(self, items):
        with mock.patch.object(cs, "ask_model", return_value=cs.TextList(items=items)):
            return cs.extract_text("anything")

    def item(self, **kw):
        base = dict(title="T", date="2026-11-13", start_time="18:00", end_time="21:00", type="Hangout", people="", location="", notes="")
        return cs.TextItem(**{**base, **kw})

    def test_times_and_types(self):
        e = self.run_with([self.item()])[0]
        self.assertEqual((e.start.hour, e.end.hour, e.type, e.all_day), (18, 21, "Hangout", False))

    def test_unknown_type_becomes_family(self):
        self.assertEqual(self.run_with([self.item(type="Spaceship")])[0].type, "Family")

    def test_unreadable_date_is_skipped_not_fatal(self):
        evs = self.run_with([self.item(date="next friday"), self.item()])
        self.assertEqual(len(evs), 1)

    def test_empty_text_asks_for_input(self):
        with self.assertRaises(SystemExit):
            cs.extract_text("   ")


class Updater(unittest.TestCase):
    """Runs the real updater code against a throwaway local 'GitHub'."""

    @classmethod
    def setUpClass(cls):
        cls.zip = io.BytesIO()
        with zipfile.ZipFile(cls.zip, "w") as z:
            z.writestr("StoutCrow-calsync-abc/app/new_file.py", "print('new')\n")
            z.writestr("StoutCrow-calsync-abc/launcher.py", "# new launcher\n")
            z.writestr("StoutCrow-calsync-abc/config/calsync.env", "EVIL=1\n")      # must never be copied
            z.writestr("StoutCrow-calsync-abc/lib/evil.py", "x=1\n")                # must never be copied
        outer = cls

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("/releases/latest"):
                    body = json.dumps({"tag_name": "v9.9.9", "body": "notes", "html_url": "http://x/r",
                                       "zipball_url": f"http://127.0.0.1:{outer.port}/repos/StoutCrow/calsync/zipball/v9.9.9",
                                       "assets": []}).encode()
                elif "zipball" in self.path:
                    body = outer.zip.getvalue()
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        os.environ["CALSYNC_UPDATE_API"] = f"http://127.0.0.1:{cls.port}"
        os.environ["CALSYNC_REPO"] = "StoutCrow/calsync"
        import importlib
        import update
        cls.update = importlib.reload(update)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        os.environ.pop("CALSYNC_UPDATE_API", None)

    def test_version_parsing(self):
        p = self.update.parse
        self.assertTrue(p("v1.10.0") > p("1.9.9"))
        self.assertTrue(p("2.0") > p("1.99.99"))
        self.assertFalse(p("1.0.0") > p("v1.0.0"))

    def test_check_sees_newer_release(self):
        c = self.update.check(fresh=True)
        self.assertTrue(c["newer"])
        self.assertEqual(c["latest"], "9.9.9")
        self.assertTrue(c["can_install"])

    def test_source_install_replaces_program_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config" / "calsync.env").write_text("KEEP=me\n")
            (root / "lib").mkdir()
            with mock.patch.dict(os.environ, {"CALSYNC_HOME": tmp}):
                r = self.update._install_source(self.update.latest(fresh=True))
            self.assertTrue(r["ok"])
            self.assertTrue((root / "app" / "new_file.py").exists())
            self.assertEqual((root / "launcher.py").read_text(), "# new launcher\n")
            self.assertEqual((root / "config" / "calsync.env").read_text(), "KEEP=me\n")   # settings untouched
            self.assertFalse((root / "lib" / "evil.py").exists())

    def test_checksum_lookup(self):
        sums = "abc123  calsync.exe\nffff  other.zip\n"
        self.assertEqual(self.update._wanted_sha(sums, "calsync.exe"), "abc123")
        with self.assertRaises(RuntimeError):
            self.update._wanted_sha(sums, "missing.exe")

    def test_only_this_projects_downloads_are_trusted(self):
        u = self.update
        self.assertFalse(u._trusted("https://evil.example/calsync.exe"))
        self.assertFalse(u._trusted("https://github.com/someone-else/calsync/releases/download/v1/calsync.exe"))


class Routes(unittest.TestCase):
    def test_every_route_needs_the_token_handler(self):
        """POST routes are only reachable through Handler.do_POST, which checks the token."""
        src = (ROOT / "app" / "app.py").read_text(encoding="utf-8")
        self.assertIn("compare_digest", src)
        self.assertIn('route = ROUTES.get(self.path)', src)

    def test_no_python_text_is_garbled(self):
        for f in list((ROOT / "app").glob("*.py")) + [ROOT / "launcher.py", ROOT / "app" / "web" / "index.html"]:
            txt = f.read_text(encoding="utf-8")
            self.assertNotIn("\u00e2\u20ac", txt, f"garbled text in {f.name}")


if __name__ == "__main__":
    unittest.main()
