import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jobcrawler.crawler import save_cache, cache_path
from jobcrawler.dashboard import record_health, source_health, home_summary
from jobcrawler.inbox import refresh_intent, read_inbox, preview_intent
from jobcrawler.personal import PersonalStore
from jobcrawler.server import make_server
from test_discovery import job, snapshot


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "data/jobs.json"
        self.store = PersonalStore(self.cache.parent / "personal.sqlite3")
        save_cache(snapshot("baidu", [job(), job(ident="other", locations=["上海"])]), self.cache)

    def intent(self):
        intent = self.store.save_intent({"name": "北京", "sources": ["baidu"], "selector": {"locations": ["北京"]}})
        refresh_intent(self.store, intent, self.cache)
        return intent

    def test_preview_is_read_only_and_edit_preserves_reading_then_undo(self):
        intent = self.intent()
        self.store.mark_read(intent["id"], "baidu", "same", 1, False)
        self.store.application(job(), "applied", "真实用户备注")
        change = {**intent, "selector": {}}
        preview = preview_intent(self.store, change, self.cache)
        self.assertEqual((preview["matching"], preview["newly_matching"], preview["unread_preserved"]), (2, 1, 1))
        self.assertEqual(self.store.intent(intent["id"])["selector"]["locations"], ["北京"])
        changed = self.store.save_intent({**change, "expected_revision": preview["expected_revision"]})
        refresh_intent(self.store, changed, self.cache)
        all_jobs = {j["id"]: j for j in read_inbox(self.store, intent["id"], "all")["jobs"]}
        self.assertEqual(all_jobs["same"]["read_state"], "unread")
        self.assertEqual(all_jobs["other"]["read_state"], "read")
        self.assertEqual(all_jobs["other"]["change_kind"], "baseline")
        self.assertEqual(all_jobs["same"]["application_status"], "applied")
        reverted = self.store.undo_intent(intent["id"], changed["revision"])
        refresh_intent(self.store, reverted, self.cache)
        self.assertEqual(read_inbox(self.store, intent["id"], "all", current_only=True)["total"], 1)
        self.assertEqual(read_inbox(self.store, intent["id"])["total"], 1)
        self.assertNotIn("previous_settings", self.store.intent(intent["id"]))

    def test_restart_requires_explicit_confirmation_and_current_revision(self):
        intent = self.intent()
        self.store.mark_read(intent["id"], "baidu", "same", 1, False)
        for body in ({**intent, "edit_mode": "restart"},
                     {**intent, "edit_mode": "restart", "expected_revision": intent["revision"]},
                     {**intent, "expected_revision": intent["revision"] - 1}):
            with self.assertRaises(ValueError):
                self.store.save_intent(body)
        self.assertEqual(read_inbox(self.store, intent["id"])["total"], 1)
        reset = self.store.save_intent({**intent, "edit_mode": "restart", "expected_revision": intent["revision"], "restart_confirmed": True})
        refresh_intent(self.store, reset, self.cache)
        self.assertEqual(read_inbox(self.store, intent["id"])["total"], 0)
        with self.assertRaises(ValueError):
            self.store.undo_intent(intent["id"], reset["revision"])

    def test_failed_source_on_edit_preserves_history_and_baselines_after_recovery(self):
        intent = self.intent()
        self.store.mark_read(intent["id"], "baidu", "same", 1, False)
        self.cache.write_text("invalid")
        changed = self.store.save_intent({**intent, "selector": {}})
        self.assertEqual(len(refresh_intent(self.store, changed, self.cache)["source_errors"]), 1)
        self.assertEqual(read_inbox(self.store, intent["id"])["total"], 1)
        save_cache(snapshot("baidu", [job(), job(ident="other")]), self.cache)
        refresh_intent(self.store, self.store.intent(intent["id"]), self.cache)
        self.assertEqual(read_inbox(self.store, intent["id"])["total"], 1)
        self.assertEqual(self.store.intent(intent["id"])["baseline_sources"], [])

    def test_health_distinguishes_failure_zero_new_partial_and_stale_events(self):
        reports = self.root / "reports"
        report = {"config": {"cache": str(self.cache.resolve())}, "started_at": "2026-10-24T01:00:00+00:00",
                  "finished_at": "2026-10-24T02:00:00+00:00", "new_count": 0,
                  "sources": {"baidu": {"status": "error", "error": "Network failure\nprivate browser arguments"}}}
        save_cache(report, reports / "first/report.json")
        health = source_health(self.cache, reports, self.store)
        self.assertEqual(health["latest_run"]["outcome"], "no_fresh_data")
        self.assertIsNone(health["latest_run"]["new_count"])
        baidu = next(r for r in health["sources"] if r["source"] == "baidu")
        self.assertEqual(baidu["last_success_at"], "2026-10-20T00:00:00+00:00")
        self.assertIsNone(baidu["new_count"])
        self.assertNotIn("private", baidu["error"])
        self.assertIsNone(record_health(self.cache, "baidu", "ok", "2026-10-27T00:00:00+00:00", new_count=0))
        record_health(self.cache, "baidu", "error", "2026-10-20T00:00:00+00:00", "late old failure")
        baidu = next(r for r in source_health(self.cache, reports, self.store)["sources"] if r["source"] == "baidu")
        self.assertEqual((baidu["status"], baidu["new_count"]), ("ok", 0))
        report["sources"]["tencent"] = {"status": "ok", "new_count": 0}
        # An older batch finishing late must not replace a newer source attempt.
        report["finished_at"] = "2099-01-01T00:00:00+00:00"
        save_cache(report, reports / "first/report.json")
        health = source_health(self.cache, reports, self.store)
        self.assertEqual(health["latest_run"]["outcome"], "partial_success")
        self.assertEqual(next(r for r in health["sources"] if r["source"] == "baidu")["status"], "ok")

    def test_home_counts_are_scoped_and_do_not_consume_unread(self):
        intent = self.intent()
        self.store.mark_read(intent["id"], "baidu", "same", 1, False)
        self.store.application(job(), "planned", "待准备")
        home = home_summary(self.store, self.cache)
        self.assertEqual((home["intents"][0]["matching"], home["intents"][0]["unread"]), (1, 1))
        self.assertEqual(home["followup"], {"planned": 1, "applied": 0, "interview": 0})
        self.assertEqual(read_inbox(self.store, intent["id"])["total"], 1)


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "data/jobs.json"
        save_cache(snapshot("baidu", [job(), job(ident="other", locations=["上海"])]), self.cache)
        self.server = make_server(port=0, cache=self.cache)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, body=None, **query):
        req = Request(f"http://127.0.0.1:{self.server.server_port}" + path + ("?" + urlencode(query) if query else ""),
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Content-Type": "application/json"})
        with urlopen(req) as response:
            return json.load(response)

    def test_edit_preview_home_and_followup_export_use_existing_paths(self):
        intent = self.request("/api/intents", {"name": "北京", "sources": ["baidu"], "selector": {"locations": ["北京"]}})
        change = {**intent, "selector": {}}
        preview = self.request("/api/intents/preview", change)
        self.assertEqual(preview["matching"], 2)
        self.assertEqual(self.request("/api/home")["intents"][0]["matching"], 1)
        changed = self.request("/api/intents", {**change, "expected_revision": preview["expected_revision"]})
        self.assertEqual(self.request("/api/home")["intents"][0]["matching"], 2)
        self.request("/api/intents/undo", {"id": intent["id"], "expected_revision": changed["revision"]})
        self.assertEqual(self.request("/api/inbox", intent_id=intent["id"], current=1, state="all")["total"], 1)
        self.request("/api/applications", {"source": "baidu", "id": "same", "status": "applied"})
        self.request("/api/applications", {"source": "baidu", "id": "other", "status": "planned"})
        query = {"sources": "baidu", "applications": 1, "application_status": "planned"}
        self.assertEqual(self.request("/api/jobs", **query)["jobs"], self.request("/api/export", **query)["jobs"])
        self.assertEqual(self.request("/api/export", **query)["jobs"][0]["id"], "other")
        self.assertTrue(self.request("/api/health")["sources"])
        with self.assertRaises(HTTPError):
            self.request("/api/intents", {**change, "expected_revision": preview["expected_revision"]})
