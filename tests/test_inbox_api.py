import csv
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jobcrawler.crawler import cache_path, save_cache
from jobcrawler.server import make_server
from test_discovery import job, snapshot


class InboxApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "jobs.json"
        save_cache(snapshot("baidu", [job()]), self.cache)
        save_cache(snapshot("tencent", [job("tencent")]), cache_path("tencent", self.cache))
        self.start()

    def start(self):
        self.server = make_server(port=0, cache=self.cache)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def tearDown(self):
        self.stop()

    def request(self, path, query=None, body=None, raw=False, headers=None):
        request = Request(self.url + path + ("?" + urlencode(query) if query else ""),
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json", **(headers or {})})
        with urlopen(request) as response:
            data = response.read()
            return data if raw else json.loads(data)

    def intent(self):
        return self.request("/api/intents", body={"name": "后端", "sources": ["baidu", "tencent"],
                                                 "selector": {}, "ranker": "published_desc"})

    def test_version_ack_and_full_export_survive_restart(self):
        intent = self.intent()
        query = {"intent_id": intent["id"], "state": "unread"}
        self.assertEqual(self.request("/api/inbox", query)["total"], 0)
        save_cache(snapshot("baidu", [job(title="后端更新"), job(ident="new", published_at="2026-10-21")],
                            fetched_at="2026-10-22T00:00:00+00:00"), self.cache)
        result = self.request("/api/inbox", query)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["counts"], {"unread": 2, "read": 1, "updated": 1})
        # Merely reading the API or exporting never consumes unread versions.
        first = self.request("/api/inbox", {**query, "limit": 1})
        self.assertTrue(first["has_more"])
        exported = self.request("/api/inbox/export", {**query, "limit": 1})
        self.assertEqual(exported["jobs"], result["jobs"])
        data = self.request("/api/inbox/export", {**query, "format": "csv"}, raw=True)
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual([r["id"] for r in rows], [j["id"] for j in result["jobs"]])
        self.assertEqual(rows[0]["read_state"], "unread")
        self.request("/api/inbox/read", body={"intent_id": intent["id"], "source": "baidu", "id": "same", "version": 1})
        self.assertEqual(self.request("/api/inbox", query)["total"], 2)  # old page cannot ack v2
        self.request("/api/inbox/read", body={"intent_id": intent["id"], "source": "baidu", "id": "same", "version": 2})
        self.stop(); self.start()
        self.assertEqual(self.request("/api/inbox", query)["total"], 1)
        self.assertEqual(self.request("/api/inbox", {**query, "state": "updated"})["jobs"][0]["read_state"], "read")

    def test_application_identity_export_notes_and_missing_snapshot(self):
        self.request("/api/applications", body={"source": "baidu", "id": "same", "status": "applied", "notes": "=private note"})
        result = self.request("/api/jobs", {"sources": "baidu,tencent"})
        statuses = {j["source"]: j["application_status"] for j in result["jobs"]}
        self.assertEqual(statuses, {"baidu": "applied", "tencent": "none"})
        save_cache(snapshot("baidu", []), self.cache)
        self.stop(); self.start()
        tracked = self.request("/api/applications")
        self.assertEqual(tracked["total"], 1)
        self.assertTrue(tracked["jobs"][0]["archived"])
        self.assertFalse(tracked["jobs"][0]["favorite"])
        self.assertEqual(self.request("/api/export", {"sources": "all", "applications": 1})["jobs"], tracked["jobs"])
        data = self.request("/api/export", {"sources": "all", "applications": 1, "format": "csv"}, raw=True)
        row = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))[0]
        self.assertEqual(row["application_status"], "applied")
        self.assertEqual(row["application_notes"], "'=private note")
        self.request("/api/applications", body={"source": "baidu", "id": "same", "status": "interview", "notes": "约面试"})
        self.assertEqual(self.request("/api/applications")["jobs"][0]["application_status"], "interview")

    def test_local_notification_ack_is_separate_and_invalid_preferences_rejected(self):
        intent = self.intent()
        settings = {"enabled": True, "frequency": "hourly", "quiet_start": 0, "quiet_end": 0}
        self.request("/api/digest/settings", body={"intent_id": intent["id"], "settings": settings})
        save_cache(snapshot("baidu", [job(), job(ident="new")], fetched_at="2026-10-22T00:00:00+00:00"), self.cache)
        self.request("/api/inbox/refresh", body={"intent_id": intent["id"]})
        self.request("/api/inbox/refresh", body={"intent_id": intent["id"]})
        notifications = self.request("/api/notifications")["notifications"]
        self.assertEqual(len(notifications), 1)
        self.assertTrue(Path(notifications[0]["file"]).is_file())
        downloaded = self.request("/api/notifications/export", {"id": notifications[0]["id"]})
        self.assertEqual(downloaded["entries"], notifications[0]["entries"])
        self.request("/api/notifications/read", body={"id": notifications[0]["id"]})
        self.assertTrue(self.request("/api/notifications")["notifications"][0]["read"])
        self.assertEqual(self.request("/api/inbox", {"intent_id": intent["id"]})["total"], 1)
        for path, body in (("/api/digest/settings", {"intent_id": intent["id"], "settings": {"quiet_start": 24}}),
                           ("/api/inbox/read", {"intent_id": intent["id"], "source": "baidu", "id": "same", "version": True}),
                           ("/api/applications", {"source": "baidu", "id": "same", "status": "sent"})):
            with self.assertRaises(HTTPError) as error:
                self.request(path, body=body)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.request("/api/digest/settings", body={"intent_id": intent["id"], "settings": settings}, headers={"Origin": "https://evil.example"})
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
