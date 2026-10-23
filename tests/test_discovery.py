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

from jobcrawler.crawler import cache_path, save_cache, stamp_snapshot
from jobcrawler.discovery import enrich_job
from jobcrawler.incremental import content_hash
from jobcrawler.models import build_job
from jobcrawler.selectors import select_jobs
from jobcrawler.server import make_server


def job(source="baidu", ident="same", **changes):
    return {**build_job(id=ident, source=source, company=source, title="后端工程师",
        url="https://example.com/job/" + ident, locations=["北京"],
        requirements="3年以上经验，Python", description="平台研发", published_at="2026-10-20"), **changes}


def snapshot(source, jobs, **changes):
    return {"source_id": source, "source_name": source, "jobs": jobs, "collected": len(jobs),
            "complete": True, "fetched_at": "2026-10-20T00:00:00+00:00", **changes}


class DiscoveryLogicTests(unittest.TestCase):
    def test_uncertain_missing_fields_but_definite_mismatch_still_excluded(self):
        pending = job(details_pending=True, requirements="", description="", locations=[])
        selector = {"years": 3, "technologies": ["Rust"], "locations": ["上海"]}
        self.assertEqual(select_jobs([pending], selector), [])
        result = select_jobs([pending], selector, include_uncertain=True)
        self.assertEqual(result[0]["match_state"], "uncertain")
        self.assertEqual(len(result[0]["uncertainty_reasons"]), 3)
        self.assertEqual(select_jobs([{**pending, "locations": ["北京"]}], selector, include_uncertain=True), [])
        self.assertEqual(select_jobs([job()], {"technologies": ["Rust"]}, include_uncertain=True), [])

    def test_stale_details_cannot_assert_match_or_exclusion_but_title_can(self):
        pending = job(details_pending=True, requirements="Python，不适合销售")
        selected = select_jobs([pending], {"technologies": ["Python"], "exclude_keywords": ["销售"]}, include_uncertain=True)
        self.assertEqual(selected[0]["match_state"], "uncertain")
        self.assertEqual(selected[0]["matches"]["technologies"], [])
        self.assertEqual(select_jobs([{**pending, "title": "销售工程师"}], {"exclude_keywords": ["销售"]}, include_uncertain=True), [])
        self.assertEqual(select_jobs([{**pending, "title": "Python工程师"}], {"technologies": ["Python"]}, include_uncertain=True)[0]["match_state"], "matched")

    def test_legacy_freshness_is_not_fabricated_or_a_content_update(self):
        item = job()
        enriched = enrich_job(item, snapshot("baidu", [item]))
        self.assertIsNone(enriched["first_seen_at"])
        self.assertEqual(enriched["observation_basis"], "snapshot")
        self.assertIsNone(enriched["details_fetched_at"])
        self.assertEqual(content_hash(item), content_hash({**item, "first_seen_at": "2026-10-23", "last_seen_at": "2026-10-23"}))
        self.assertEqual(enrich_job(item, {})["freshness_status"], "unknown")
        self.assertEqual(enrich_job(item, {"fetched_at": "2099-01-01"})["freshness_status"], "unknown")

    def test_observations_survive_disappearance_and_do_not_retimestamp_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "observations.sqlite3"
            first = stamp_snapshot(snapshot("baidu", [job()]), {"jobs": []}, ledger)
            first_time = first["jobs"][0]["first_seen_at"]
            later = stamp_snapshot(snapshot("baidu", [job()], fetched_at="2026-10-23T01:00:00+00:00"), {"jobs": []}, ledger)
            self.assertEqual(later["jobs"][0]["first_seen_at"], first_time)
            self.assertNotEqual(later["jobs"][0]["last_seen_at"], first_time)
            legacy = stamp_snapshot(snapshot("baidu", [job(ident="old")]), {"jobs": []}, ledger, known_ids=["old"])
            self.assertIsNone(legacy["jobs"][0]["first_seen_at"])
            pending = stamp_snapshot(snapshot("baidu", [job(details_pending=True)]), first, ledger)
            self.assertEqual(pending["jobs"][0]["details_fetched_at"], first["jobs"][0]["details_fetched_at"])


class DiscoveryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "jobs.json"
        save_cache(snapshot("baidu", [job(), job(ident="other", published_at="2026-10-21")]), self.cache)
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

    def get(self, path, **query):
        with urlopen(self.url + path + "?" + urlencode(query)) as response:
            return json.load(response)

    def post(self, path, body, **headers):
        request = Request(self.url + path, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", **headers})
        with urlopen(request) as response:
            return json.load(response)

    def test_multi_source_pagination_and_full_export_use_identical_order(self):
        query = {"sources": "baidu,tencent", "include_uncertain": "1", "ranker": "published_desc"}
        full = self.get("/api/jobs", **query)
        first = self.get("/api/jobs", limit=1, **query)
        rest = self.get("/api/jobs", offset=1, limit=2, **query)
        self.assertEqual(full["total"], 3)
        self.assertEqual(full["jobs"], first["jobs"] + rest["jobs"])
        self.assertTrue(first["has_more"])
        self.assertFalse(rest["has_more"])
        exported = self.get("/api/export", format="json", limit=1, **query)
        self.assertEqual(exported["jobs"], full["jobs"])
        with urlopen(self.url + "/api/export?" + urlencode({**query, "format": "csv", "limit": 1})) as response:
            rows = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual([(row["source"], row["title"]) for row in rows], [(j["source"], j["title"]) for j in full["jobs"]])

    def test_intents_and_favorites_survive_restart_and_missing_listing(self):
        intent = {"name": "上海后端", "sources": ["baidu", "tencent"], "selector": {"locations": ["上海"]}, "ranker": "experience_asc"}
        a = self.post("/api/intents", intent)
        b = self.post("/api/intents", {**intent, "name": "北京后端"})
        self.post("/api/intents", {**a, "name": "上海研发"})
        for source in ("baidu", "tencent"):
            self.post("/api/favorites", {"source": source, "id": "same", "saved": True})
        self.stop(); self.start()
        self.assertEqual(len(self.get("/api/intents")["intents"]), 2)
        save_cache(snapshot("baidu", []), self.cache)
        favorites = self.get("/api/favorites")
        self.assertEqual(favorites["total"], 2)
        archived = next(j for j in favorites["jobs"] if j["source"] == "baidu")
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["last_seen_at"], "2026-10-20T00:00:00+00:00")
        self.post("/api/favorites", {"source": "baidu", "id": "same", "saved": False})
        self.assertEqual(self.get("/api/favorites")["total"], 1)
        self.post("/api/intents/delete", {"id": b["id"]})
        self.assertEqual(len(self.get("/api/intents")["intents"]), 1)

    def test_failed_source_does_not_hide_healthy_results_and_invalid_input_rejected(self):
        cache_path("tencent", self.cache).write_text("invalid JSON")
        result = self.get("/api/jobs", sources="baidu,tencent")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["source_errors"][0]["source"], "tencent")
        for query in ({"sources": "missing"}, {"limit": -1}, {"include_uncertain": "true"}):
            with self.assertRaises(HTTPError) as error:
                self.get("/api/jobs", **query)
            self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.post("/api/intents", {"name": "x", "sources": ["baidu"], "selector": {"years": -1}})
        self.assertEqual(error.exception.code, 400)
        with self.assertRaises(HTTPError) as error:
            self.post("/api/favorites", {"source": "baidu", "id": "same", "saved": True}, Origin="https://evil.example")
        self.assertEqual(error.exception.code, 403)

    def test_explicit_matched_only_does_not_call_unknown_experience_a_match(self):
        item = job(requirements="Python", experience={"min": None, "max": None, "evidence": "", "source": "unknown"})
        save_cache(snapshot("baidu", [item]), self.cache)
        selector = json.dumps({"years": 3, "include_unknown_experience": True})
        self.assertEqual(self.get("/api/jobs", selector=selector)["total"], 1)  # legacy API unchanged
        self.assertEqual(self.get("/api/jobs", selector=selector, include_uncertain=0)["total"], 0)
        result = self.get("/api/jobs", selector=selector, include_uncertain=1)
        self.assertEqual(result["uncertain_count"], 1)
