import csv
import fcntl
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from jobcrawler.crawler import cache_path, save_cache
from jobcrawler.models import build_job
from jobcrawler.sync import sync_jobs


def snapshot(source, ids, **values):
    jobs = [build_job(id=i, title=f"Python工程师{i}", source=source, company=source,
                     url=f"https://example.com/{i}", locations=["北京市"],
                     requirements="3-5年Python开发经验", description="平台研发",
                     published_at=f"2026-10-{int(i):02d}") for i in ids]
    return {"source_id": source, "complete": True, "collected": len(jobs),
            "fetched_at": "2026-10-18T00:00:00+00:00", "jobs": jobs, **values}


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cache = root / "data/jobs.json"
        self.state = root / "data/sync-state.json"
        self.output = root / "output"
        fake_sources = patch("jobcrawler.incremental.get_source", side_effect=lambda s:
            SimpleNamespace(id=s, name=s, url="https://example.com", fetch_page=lambda page: None))
        fake_sources.start()
        self.addCleanup(fake_sources.stop)

    def run_sync(self, **kwargs):
        return sync_jobs(state_path=self.state, cache=self.cache, output=self.output, **kwargs)

    def test_baseline_repeat_and_reappearing_id(self):
        save_cache(snapshot("baidu", ["1"]), self.cache)
        with patch("jobcrawler.sync.crawl") as fetch:
            self.assertEqual(self.run_sync(sources=["baidu"], initialize=True)["seen"], {"baidu": 1})
            fetch.assert_not_called()
            fetch.return_value = snapshot("baidu", ["1", "2", "2"])
            first = self.run_sync(sources=["baidu"])
            self.assertEqual(first["new_count"], 1)
            path = first["sources"]["baidu"]["json"]
            self.assertEqual([j["id"] for j in json.loads(Path(path).read_text())["jobs"]], ["2"])
            self.assertEqual(self.run_sync(sources=["baidu"])["new_count"], 0)
            fetch.return_value = snapshot("baidu", ["2"])
            self.run_sync(sources=["baidu"])
            fetch.return_value = snapshot("baidu", ["1", "2"])
            fetch.return_value["jobs"][0]["title"] = "职位更名"
            self.assertEqual(self.run_sync(sources=["baidu"])["new_count"], 0)

    def test_identical_ids_in_different_sources_are_distinct(self):
        save_cache(snapshot("baidu", ["1"]), self.cache)
        with patch("jobcrawler.sync.crawl", side_effect=lambda **kw: snapshot(kw["source"], ["1"])):
            result = self.run_sync(sources=["baidu", "tencent"])
        self.assertEqual(result["sources"]["baidu"]["new_count"], 0)
        self.assertEqual(result["sources"]["tencent"]["new_count"], 1)

    def test_failed_or_partial_source_preserves_cache_and_continues(self):
        for failure in [RuntimeError("需要人工验证"), snapshot("baidu", ["2"], complete=False)]:
            with self.subTest(failure=failure):
                save_cache(snapshot("baidu", ["1"]), self.cache)
                before = self.cache.read_bytes()
                self.run_sync(sources=["baidu"], initialize=True)
                with patch("jobcrawler.sync.crawl", side_effect=[failure, snapshot("tencent", ["3"])]):
                    result = self.run_sync(sources=["baidu", "tencent"])
                self.assertEqual(result["failed_sources"], ["baidu"])
                self.assertEqual(result["sources"]["tencent"]["status"], "ok")
                self.assertEqual(self.cache.read_bytes(), before)
                self.assertEqual(json.loads(self.state.read_text())["sources"]["baidu"]["seen_ids"], ["1"])

    def test_selector_ranker_and_csv_reuse(self):
        data = snapshot("baidu", ["1", "3", "2"])
        data["jobs"][0]["locations"] = ["上海市"]
        with patch("jobcrawler.sync.crawl", return_value=data):
            result = self.run_sync(sources=["baidu"], selector={"locations": ["北京"], "technologies": ["Python"]})
        entry = result["sources"]["baidu"]
        self.assertEqual((entry["new_count"], entry["exported_count"]), (3, 2))
        self.assertEqual([j["id"] for j in json.loads(Path(entry["json"]).read_text())["jobs"]], ["3", "2"])
        rows = list(csv.DictReader(io.StringIO(Path(entry["csv"]).read_text(encoding="utf-8-sig"))))
        self.assertEqual([r["title"] for r in rows], ["Python工程师3", "Python工程师2"])
        self.assertEqual(json.loads(self.state.read_text())["sources"]["baidu"]["seen_ids"], ["1", "2", "3"])

    def test_export_failure_does_not_mark_seen_or_replace_cache(self):
        save_cache(snapshot("baidu", ["1"]), self.cache)
        before = self.cache.read_bytes()
        real_write = Path.write_text

        def fail_csv(path, *args, **kwargs):
            if path.suffix == ".csv":
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        with patch("jobcrawler.sync.crawl", return_value=snapshot("baidu", ["1", "2"])), \
                patch.object(Path, "write_text", fail_csv):
            self.assertEqual(self.run_sync(sources=["baidu"])["failed_sources"], ["baidu"])
        self.assertEqual(before, self.cache.read_bytes())
        self.assertEqual(json.loads(self.state.read_text())["sources"]["baidu"]["seen_ids"], ["1"])
        with patch("jobcrawler.sync.crawl", return_value=snapshot("baidu", ["1", "2"])):
            self.assertEqual(self.run_sync(sources=["baidu"])["new_count"], 1)

    def test_overlapping_sync_is_rejected(self):
        self.state.parent.mkdir(parents=True)
        with self.state.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "正在运行"):
                self.run_sync(sources=["baidu"], initialize=True)

    def test_new_source_uses_its_own_existing_baseline(self):
        self.run_sync(sources=["baidu"], initialize=True)
        save_cache(snapshot("tencent", ["1"]), cache_path("tencent", self.cache))
        with patch("jobcrawler.sync.crawl", return_value=snapshot("tencent", ["1", "2"])):
            result = self.run_sync(sources=["tencent"])
        self.assertEqual(result["seeded"], {"tencent": 1})
        self.assertEqual(result["new_count"], 1)
