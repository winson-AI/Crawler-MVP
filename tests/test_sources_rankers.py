import csv
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jobcrawler.crawler import cache_path, crawl, export_csv, load_cache, save_cache
from jobcrawler.models import build_job
from jobcrawler.rankers import rank_jobs
from jobcrawler.selectors import select_jobs
from jobcrawler.server import make_server
from jobcrawler.sources import SOURCES
from jobcrawler.sources.baidu import normalize_job
from jobcrawler.sources.base import JobPage


def job(id, date="", years=None, source="sample", **extra):
    return build_job(id=id, title="后端研发", company="示例公司", source=source,
                     url=f"https://example.com/jobs/{id}", locations=["北京市"],
                     requirements="使用Python研发", published_at=date,
                     experience={"min": years, "max": None, "evidence": "", "source": "structured"}, **extra)


class SampleSource:
    """Test adapter uses a different upstream schema and the shared output contract."""
    id, name, url = "sample", "示例招聘", "https://example.com/jobs"

    def fetch_page(self, page):
        return JobPage(page, 3, [job("old", "2025-01-01", 1), job("new", "2026-01-01", 5)]
                       if page == 1 else [job("unknown")])


class RankerTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [job("old", "2025-01-01", 1), job("unknown"), job("new", "2026-01-01", 5),
                     job("tie", "2026-01-01", 3), job("invalid", "not-a-date", 0)]

    def ids(self, ranker):
        return [j["id"] for j in rank_jobs(self.jobs, ranker)]

    def test_publication_ascending_descending_and_unknown_last(self):
        self.assertEqual(self.ids("published_asc"), ["old", "new", "tie", "unknown", "invalid"])
        self.assertEqual(self.ids("published_desc"), ["new", "tie", "old", "unknown", "invalid"])
        self.assertEqual([j["id"] for j in self.jobs], ["old", "unknown", "new", "tie", "invalid"])

    def test_experience_ascending_descending_and_unknown_last(self):
        self.assertEqual(self.ids("experience_asc"), ["invalid", "old", "tie", "new", "unknown"])
        self.assertEqual(self.ids("experience_desc"), ["new", "tie", "old", "invalid", "unknown"])
        self.jobs[3]["experience"]["min"] = 5
        self.jobs[3]["experience"]["max"] = 8
        self.assertEqual(self.ids("experience_desc")[:2], ["new", "tie"])

    def test_dates_compare_actual_timezone_and_never_use_updated_at(self):
        values = [job("later", "2026-01-01T00:30:00Z"), job("earlier", "2026-01-01T08:00:00+08:00"),
                  job("only-updated", updated_at="2027-01-01")]
        self.assertEqual([j["id"] for j in rank_jobs(values)], ["later", "earlier", "only-updated"])
        with self.assertRaises(ValueError):
            rank_jobs([], "invalid")

    def test_baidu_maps_publication_separately_from_update(self):
        normalized = normalize_job({"postId": "one", "name": "职位", "publishDate": "2025-01-01", "updateDate": "2026-01-01"})
        self.assertEqual(normalized["published_at"], "2025-01-01")
        self.assertEqual(normalized["updated_at"], "2026-01-01")


class SourceTests(unittest.TestCase):
    @patch("jobcrawler.crawler.time.sleep")
    def test_new_adapter_reuses_crawl_selector_ranker_export(self, sleep):
        with patch.dict(SOURCES, sample=SampleSource()):
            snapshot = crawl(source="sample")
        self.assertTrue(snapshot["complete"])
        self.assertEqual(snapshot["source_id"], "sample")
        self.assertEqual(snapshot["source_url"], SampleSource.url)
        selected = select_jobs(snapshot["jobs"], {"locations": ["北京"], "technologies": ["Python"], "keywords": ["研发"]})
        ranked = rank_jobs(selected, "experience_desc")
        rows = list(csv.DictReader(io.StringIO(export_csv(ranked).lstrip("\ufeff"))))
        self.assertEqual([r["title"] for r in rows], [j["title"] for j in ranked])
        self.assertEqual([j["id"] for j in ranked], ["new", "old", "unknown"])
        self.assertTrue(all(r["source"] == "sample" for r in rows))

    def test_separate_caches_and_legacy_publication_unknown(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(SOURCES, sample=SampleSource()):
            base = Path(directory) / "jobs.json"
            self.assertEqual(cache_path("baidu", base), base)
            self.assertNotEqual(cache_path("sample", base), base)
            legacy = {"jobs": [{"source": "baidu", "updated_at": "2026-01-01"}]}
            base.write_text(json.dumps(legacy))
            loaded = load_cache(base)
            self.assertEqual(loaded["jobs"][0]["published_at"], "")
            self.assertEqual(load_cache(cache_path("sample", base), "sample")["jobs"], [])
            with self.assertRaises(ValueError):
                load_cache(base, "sample")


class MultiSourceServerTests(unittest.TestCase):
    def setUp(self):
        self.registry = patch.dict(SOURCES, sample=SampleSource())
        self.registry.start()
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Path(self.directory.name) / "jobs.json"
        # Same upstream ID in different source caches must never collide.
        save_cache({"source_id": "baidu", "jobs": [job("old", source="baidu")]}, self.cache)
        self.original = self.cache.read_bytes()
        self.server = make_server(port=0, cache=self.cache)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()
        self.registry.stop()

    def get(self, path):
        with urlopen(self.url + path) as response:
            return json.load(response)

    def test_source_refresh_and_sorted_api_exports(self):
        self.assertEqual({s["id"] for s in self.get("/api/sources")["sources"]}, set(SOURCES))
        with patch("jobcrawler.crawler.time.sleep"):
            request = Request(self.url + "/api/crawl", data=b'{"source":"sample"}', headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
            deadline = time.monotonic() + 3
            while self.get("/api/status?source=sample")["running"]:
                if time.monotonic() > deadline:
                    self.fail("抓取未完成")
                threading.Event().wait(0.01)
        self.assertEqual(self.cache.read_bytes(), self.original)
        query = urlencode({"source": "sample", "ranker": "experience_desc", "selector": json.dumps({"technologies": ["Python"]})})
        result = self.get("/api/jobs?" + query)
        exported = self.get("/api/export?" + query)
        self.assertEqual([j["id"] for j in result["jobs"]], ["new", "old", "unknown"])
        self.assertEqual(result["jobs"], exported["jobs"])
        self.assertEqual(exported["ranker"], "experience_desc")
        self.assertEqual(exported["snapshot"]["source_id"], "sample")
        with urlopen(self.url + "/api/export?" + query + "&format=csv") as response:
            rows = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
        self.assertEqual([r["url"] for r in rows], [j["url"] for j in result["jobs"]])
        self.assertEqual(len(self.get("/api/jobs")["jobs"]), 1)

    def test_invalid_source_or_ranker_returns_400(self):
        for path in ["/api/jobs?source=missing", "/api/status?source=missing", "/api/jobs?ranker=missing", "/api/export?ranker=missing"]:
            with self.subTest(path=path), self.assertRaises(HTTPError) as result:
                self.get(path)
            self.assertEqual(result.exception.code, 400)
        with self.assertRaises(HTTPError) as result:
            urlopen(Request(self.url + "/api/crawl", data=b'{"source":"missing"}'))
        self.assertEqual(result.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
