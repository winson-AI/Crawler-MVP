import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import urlencode

from jobcrawler.crawler import crawl, export_csv, load_cache, save_cache
from jobcrawler.incremental import content_hash
from jobcrawler.selectors import job_recruitment_type, parse_experience, select_jobs, tech_matches, validate_selector
from jobcrawler.sources.baidu import normalize_job
from jobcrawler.sources.base import JobPage
from jobcrawler.server import make_server


def raw(job_id="one", **values):
    return {"postId": job_id, "name": "后端研发工程师", "workPlace": "北京市,上海市",
            "serviceCondition": "3-5年工作经验，熟悉 Java、Python、Go、C++", "workContent": "大模型平台研发",
            **values}


class ExperienceTests(unittest.TestCase):
    def test_real_world_phrasings(self):
        cases = [
            ("3-5 年数据策略研发经验，千万 DAU 产品增长经验优先", 3, 5),
            ("大模型行业从业经验5年以上", 5, None),
            ("5 年以上云计算、AI 基础设施、HPC 或生物信息领域销售经验", 5, None),
            ("3‑5 年互联网社区 / 内容平台产品经理经验，有创作者生态经验优先", 3, 5),
            ("三至五年开发经验", 3, 5), ("十年以上工作经验", 10, None),
            ("2年以内工作经验", 0, 2), ("经验不限", 0, None),
            ("2027年毕业，实习6个月", None, None), ("5年以上经验优先", None, None),
            ("熟悉Python，具备开发经验", None, None),
        ]
        for text, low, high in cases:
            with self.subTest(text=text):
                result = parse_experience("", text)
                self.assertEqual((result["min"], result["max"]), (low, high))

    def test_structured_field_has_priority(self):
        result = parse_experience("1-3年", "5年以上开发经验")
        self.assertEqual((result["min"], result["max"], result["source"]), (1, 3, "workYears"))


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [normalize_job(raw()), normalize_job(raw("two", name="销售经理", workPlace="深圳市",
                    serviceCondition="5年以上销售经验", workContent="云计算销售")),
                    normalize_job(raw("three", serviceCondition="熟悉Python，具备开发经验"))]

    def test_combined_selector_and_evidence(self):
        result = select_jobs(self.jobs, {"locations": ["北京"], "years": 3,
            "technologies": ["python", "golang"], "keywords": ["大模型", "研发"]})
        self.assertEqual([j["id"] for j in result], ["one"])
        self.assertEqual(result[0]["matches"]["technologies"], ["python", "golang"])
        self.assertEqual(select_jobs(self.jobs, {"years": 6, "locations": ["北京"]}), [])

    def test_any_all_exclusions_and_unknown(self):
        self.assertEqual(len(select_jobs(self.jobs, {"years": 3, "include_unknown_experience": True})), 2)
        self.assertEqual(len(select_jobs(self.jobs, {"technologies": ["Python", "Rust"], "technology_mode": "any"})), 2)
        self.assertEqual(select_jobs(self.jobs, {"technologies": ["Python", "Rust"]}), [])
        self.assertEqual(len(select_jobs(self.jobs, {"keywords": ["销售", "缺失"], "keyword_mode": "any"})), 1)
        self.assertEqual(select_jobs(self.jobs, {"keywords": ["销售", "缺失"]}), [])
        self.assertEqual(len(select_jobs(self.jobs, {"exclude_keywords": ["销售"]})), 2)
        self.assertEqual(len(select_jobs(self.jobs, {})), 3)

    def test_tech_token_boundaries(self):
        for term, text, expected in [("Java", "JavaScript", False), ("Go", "Google", False),
            ("Go", "Golang", True), ("C++", "熟练C++开发", True), ("C#", "C#", True),
            ("Python", "熟悉PYTHON开发", True), ("nodejs", "Node.js", True)]:
            self.assertEqual(tech_matches(term, text), expected)

    def test_city_filter_matches_hierarchical_address_without_substring_false_positive(self):
        item = {**self.jobs[0], "locations": ["上海·浦东新区·张江"]}
        for city in ("上海", "上海市", "上海·浦东新区·张江"):
            self.assertEqual(len(select_jobs([item], {"locations": [city]})), 1)
        for city in ("海", "北京"):
            self.assertEqual(select_jobs([item], {"locations": [city]}), [])

    def test_validation(self):
        for invalid in [{"years": -1}, {"years": True}, {"years": float("nan")},
                        {"locations": "北京"}, {"keywords": [""]}, {"typo": 1},
                        {"keyword_mode": "xor"}, {"include_unknown_experience": "false"}, []]:
            with self.subTest(selector=invalid), self.assertRaises(ValueError):
                validate_selector(invalid)

    def test_recruitment_type_filter_and_combined_conditions(self):
        jobs = [{**self.jobs[0], "recruitment_type": "campus"},
                {**self.jobs[1], "recruitment_type": "social"},
                {**self.jobs[2], "source": "custom_unknown", "recruitment_type": "unknown"}]
        for kind, ids in (("all", ["one", "two", "three"]), ("campus", ["one"]),
                          ("social", ["two"]), ("unknown", ["three"])):
            self.assertEqual([j["id"] for j in select_jobs(jobs, {"recruitment_type": kind})], ids)
        self.assertEqual(len(select_jobs(jobs, {})), 3)
        self.assertEqual(len(select_jobs(jobs, {"recruitment_type": "campus", "locations": ["北京"],
            "years": 3, "technologies": ["Python"], "keywords": ["大模型"]})), 1)
        self.assertEqual(select_jobs(jobs, {"recruitment_type": "campus", "locations": ["深圳"]}), [])

    def test_recruitment_type_validation(self):
        for value in (None, "", "intern", "校招", [], True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_selector({"recruitment_type": value})
        self.assertEqual(validate_selector({})["recruitment_type"], "all")

    def test_recruitment_type_uses_explicit_evidence_not_experience(self):
        cases = [
            ({"recruitment_type": "校园招聘", "source": "baidu"}, "campus"),
            ({"recruitment_type": "social", "title": "校招专员"}, "social"),
            ({"title": "后端工程师（校招）"}, "campus"),
            ({"category": "社会招聘"}, "social"),
            ({"title": "校招/社招工程师", "source": "baidu"}, "unknown"),
            ({"source": "baidu"}, "social"),
            ({"url": "https://campus.lagou.com/"}, "campus"),
            ({"url": "https://campus.lagou.com.evil.com/"}, "unknown"),
            ({"title": "实习生", "requirements": "应届毕业生，校园项目经验", "experience": {"min": 0}}, "unknown"),
        ]
        for job, kind in cases:
            with self.subTest(job=job):
                self.assertEqual(job_recruitment_type(job), kind)

    def test_legacy_cache_inherits_campus_source_without_changing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            job = {k: v for k, v in self.jobs[0].items() if k != "recruitment_type"}
            job["source"] = "custom_old_campus"
            snapshot = {"source_id": job["source"], "source_url": "https://campus.51job.com/asiainfo/jobs.html", "jobs": [job]}
            save_cache(snapshot, path)
            before = path.read_bytes()
            adapter = SimpleNamespace(id=job["source"], name="校招", url=snapshot["source_url"])
            with patch("jobcrawler.crawler.get_source", return_value=adapter):
                loaded = load_cache(path, job["source"])
            self.assertEqual(loaded["jobs"][0]["recruitment_type"], "campus")
            self.assertEqual(path.read_bytes(), before)

    def test_adding_derived_type_does_not_report_all_legacy_jobs_as_updated(self):
        legacy = {k: v for k, v in self.jobs[0].items() if k != "recruitment_type"}
        self.assertEqual(content_hash(legacy), content_hash({**legacy, "recruitment_type": "social"}))
        self.assertNotEqual(content_hash(legacy), content_hash({**legacy, "recruitment_type": "campus"}))
        legacy["source"] = "custom_campus"
        url = "https://campus.lagou.com/"
        self.assertEqual(content_hash(legacy, url), content_hash({**legacy, "recruitment_type": "campus"}, url))


class CrawlerTests(unittest.TestCase):
    def fetch(self, page):
        return JobPage(page, 3, [normalize_job(row) for row in (
            [raw("one"), raw("two")] if page == 1 else [raw("two"), raw("three")])])

    @patch("jobcrawler.crawler.time.sleep")
    def test_pages_dedup_and_completeness(self, sleep):
        snapshot = crawl(fetch=self.fetch)
        self.assertEqual(snapshot["pages"], 2)
        self.assertTrue(snapshot["complete"])
        self.assertEqual([j["id"] for j in snapshot["jobs"]], ["one", "two", "three"])
        partial = crawl(max_pages=1, fetch=self.fetch)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["collected"], 2)

    @patch("jobcrawler.crawler.time.sleep")
    def test_stuck_and_empty_pages_fail(self, sleep):
        for rows in [[raw("one")], []]:
            with self.assertRaises(RuntimeError):
                crawl(fetch=lambda p: JobPage(p, 3, [normalize_job(row) for row in rows]))

    def test_atomic_cache_roundtrip_and_csv(self):
        snapshot = crawl(max_pages=1, fetch=self.fetch)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            save_cache(snapshot, path)
            self.assertEqual(load_cache(path), snapshot)
            self.assertFalse(path.with_suffix(".tmp").exists())
        snapshot["jobs"][0]["title"] = "=HYPERLINK(1)"
        csv = export_csv(snapshot["jobs"])
        self.assertTrue(csv.startswith("\ufeff"))
        self.assertIn("'=HYPERLINK(1)", csv)
        self.assertIn("https://talent.baidu.com/jobs/detail/SOCIAL/one", csv)

    @patch("jobcrawler.crawler.time.sleep")
    def test_explicit_last_page_with_upstream_duplicates_is_complete(self, sleep):
        pages = {1: JobPage(1, 4, [normalize_job(raw("one")), normalize_job(raw("one"))], has_more=True),
                 2: JobPage(2, 4, [normalize_job(raw("two")), normalize_job(raw("three"))], has_more=False)}
        result = crawl(fetch=pages.__getitem__)
        self.assertTrue(result["complete"])
        self.assertEqual((result["collected"], result["scanned"], result["duplicates"]), (3, 4, 1))

    @patch("jobcrawler.crawler.time.sleep")
    def test_explicit_more_pages_prevents_early_completion(self, sleep):
        pages = {1: JobPage(1, 1, [normalize_job(raw("one"))], has_more=True),
                 2: JobPage(2, 2, [normalize_job(raw("two"))], has_more=False)}
        self.assertEqual(crawl(fetch=pages.__getitem__)["collected"], 2)


class ServerTests(unittest.TestCase):
    def pdd_adapter(self, events):
        adapter = SimpleNamespace(id="pinduoduo", name="PDD", _context=None, _browser=None)
        @contextmanager
        def session():
            events.append(("open", threading.get_ident()))
            adapter._context = object()
            try:
                yield
            finally:
                adapter._context = None
                events.append(("close", threading.get_ident()))
        adapter.session = session
        return adapter

    def refresh_pdd(self):
        request = Request(self.url + "/api/crawl", data=b'{"source":"pinduoduo"}',
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            self.assertEqual(response.status, 202)
        deadline = time.monotonic() + 3
        while True:
            with urlopen(self.url + "/api/status") as response:
                status = json.load(response)
            if not status["running"]:
                return status
            if time.monotonic() >= deadline:
                self.fail("刷新任务未结束")
            threading.Event().wait(0.01)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Path(self.directory.name) / "jobs.json"
        save_cache(crawl(max_pages=1, fetch=CrawlerTests().fetch), self.cache)
        self.pdd_stop = threading.Event()
        with patch("jobcrawler.server.Event", return_value=self.pdd_stop):
            self.server = make_server(port=0, cache=self.cache)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def test_ui_api_export_and_invalid_selector(self):
        with urlopen(self.url) as response:
            self.assertIn("职位雷达", response.read().decode())
        with urlopen(self.url + "/api/jobs") as response:
            self.assertEqual(json.load(response)["total"], 2)
        with urlopen(self.url + "/api/export?format=csv") as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
        with self.assertRaises(HTTPError) as result:
            urlopen(self.url + "/api/jobs?selector=%7B%22years%22%3A-1%7D")
        self.assertEqual(result.exception.code, 400)

    def test_recruitment_selector_api_and_exports(self):
        snapshot = load_cache(self.cache)
        snapshot["jobs"][0]["recruitment_type"] = "campus"
        save_cache(snapshot, self.cache)
        for kind in ("campus", "social", "unknown"):
            params = urlencode({"selector": json.dumps({"recruitment_type": kind})})
            with urlopen(self.url + "/api/jobs?" + params) as response:
                result = json.load(response)
            self.assertEqual(result["total"], 0 if kind == "unknown" else 1)
            self.assertTrue(all(job["recruitment_type"] == kind for job in result["jobs"]))
            with urlopen(self.url + "/api/export?format=json&" + params) as response:
                exported = json.load(response)
            self.assertEqual(exported["jobs"], result["jobs"])
            self.assertEqual(exported["selector"]["recruitment_type"], kind)
            with urlopen(self.url + "/api/export?format=csv&" + params) as response:
                self.assertIn("recruitment_type", response.read().decode())
        with self.assertRaises(HTTPError) as result:
            urlopen(self.url + "/api/jobs?" + urlencode({"selector": '{"recruitment_type":"invalid"}'}))
        self.assertEqual(result.exception.code, 400)

    def test_refresh_failure_preserves_cache(self):
        before = self.cache.read_bytes()
        with patch("jobcrawler.server.crawl", side_effect=RuntimeError("upstream unavailable")):
            request = Request(self.url + "/api/crawl", data=b'{}', headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
            # Joining the worker gives deterministic evidence of the failure state.
            for worker in threading.enumerate():
                if worker is not self.thread and worker.name.endswith("(refresh)"):
                    worker.join(timeout=2)
        self.assertEqual(before, self.cache.read_bytes())
        with urlopen(self.url + "/api/status") as response:
            self.assertEqual(json.load(response)["error"], "upstream unavailable")

    def test_pdd_full_refresh_uses_list_first_sync_instead_of_full_detail_crawl(self):
        result = {"sources": {"pinduoduo": {"status": "ok"}}, "report_path": "test-report.json"}
        with patch("jobcrawler.server.get_source", return_value=self.pdd_adapter([])), \
                patch("jobcrawler.server.sync_jobs", return_value=result) as sync, patch("jobcrawler.server.crawl") as crawl:
            status = self.refresh_pdd()
        self.assertEqual(status["report_path"], "test-report.json")
        self.assertEqual(sync.call_args.kwargs["pdd_details"], "defer")
        self.assertEqual(sync.call_args.kwargs["sources"], ["pinduoduo"])
        self.assertEqual(sync.call_args.kwargs["delay"], 6)
        crawl.assert_not_called()

    def test_pdd_refreshes_share_owner_thread_and_close_session_at_shutdown(self):
        events, threads = [], []
        adapter = self.pdd_adapter(events)
        def sync(**kwargs):
            threads.append(threading.get_ident())
            return {"sources": {"pinduoduo": {"status": "ok"}}, "report_path": "test-report.json"}
        with patch("jobcrawler.server.get_source", return_value=adapter), patch("jobcrawler.server.sync_jobs", side_effect=sync), \
                patch("jobcrawler.server.monotonic", return_value=100), patch.object(self.pdd_stop, "wait", return_value=False) as wait:
            self.assertIsNone(self.refresh_pdd()["error"])
            self.assertIsNone(self.refresh_pdd()["error"])
            self.assertEqual(len(events), 1)
            self.assertEqual(threads, [events[0][1], events[0][1]])
            wait.assert_called_once_with(120)
            self.server.shutdown()
            self.server.server_close()
            self.assertEqual([event[0] for event in events], ["open", "close"])

    def test_pdd_does_not_add_cooldown_after_enough_idle_time(self):
        result = {"sources": {"pinduoduo": {"status": "ok"}}, "report_path": "test-report.json"}
        with patch("jobcrawler.server.get_source", return_value=self.pdd_adapter([])), \
                patch("jobcrawler.server.sync_jobs", return_value=result), \
                patch("jobcrawler.server.monotonic", side_effect=[100, 100, 221, 221]), \
                patch.object(self.pdd_stop, "wait", return_value=False) as wait:
            self.assertIsNone(self.refresh_pdd()["error"])
            self.assertIsNone(self.refresh_pdd()["error"])
            wait.assert_not_called()

    def test_pdd_failed_refresh_also_cools_down_without_reopening_session(self):
        events = []
        results = [{"sources": {"pinduoduo": {"status": "paused", "error": "manual verification"}},
                    "report_path": "paused-report.json"},
                   {"sources": {"pinduoduo": {"status": "ok"}}, "report_path": "test-report.json"}]
        with patch("jobcrawler.server.get_source", return_value=self.pdd_adapter(events)), \
                patch("jobcrawler.server.sync_jobs", side_effect=results), \
                patch("jobcrawler.server.monotonic", return_value=100), \
                patch.object(self.pdd_stop, "wait", return_value=False) as wait:
            self.assertEqual(self.refresh_pdd()["error"], "manual verification")
            self.assertIsNone(self.refresh_pdd()["error"])
            wait.assert_called_once_with(120)
            self.assertEqual([event[0] for event in events], ["open"])

    def test_pdd_shutdown_interrupts_cooldown_without_starting_another_refresh(self):
        events = []
        result = {"sources": {"pinduoduo": {"status": "ok"}}, "report_path": "test-report.json"}
        with patch("jobcrawler.server.get_source", return_value=self.pdd_adapter(events)), \
                patch("jobcrawler.server.sync_jobs", return_value=result) as sync:
            self.assertIsNone(self.refresh_pdd()["error"])
            request = Request(self.url + "/api/crawl", data=b'{"source":"pinduoduo"}',
                              headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                self.assertEqual(response.status, 202)
            deadline = time.monotonic() + 3
            while True:
                with urlopen(self.url + "/api/status") as response:
                    status = json.load(response)
                if (status.get("progress") or {}).get("phase") == "cooldown" or time.monotonic() >= deadline:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(status["progress"]["phase"], "cooldown")
            self.assertGreater(status["progress"]["seconds"], 110)
            self.server.shutdown()
            self.server.server_close()
            self.assertEqual(sync.call_count, 1)
            self.assertEqual([event[0] for event in events], ["open", "close"])


if __name__ == "__main__":
    unittest.main()
