import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jobcrawler.crawler import crawl, export_csv, load_cache, save_cache
from jobcrawler.selectors import parse_experience, select_jobs, tech_matches, validate_selector
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

    def test_validation(self):
        for invalid in [{"years": -1}, {"years": True}, {"years": float("nan")},
                        {"locations": "北京"}, {"keywords": [""]}, {"typo": 1},
                        {"keyword_mode": "xor"}, {"include_unknown_experience": "false"}, []]:
            with self.subTest(selector=invalid), self.assertRaises(ValueError):
                validate_selector(invalid)


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
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache = Path(self.directory.name) / "jobs.json"
        save_cache(crawl(max_pages=1, fetch=CrawlerTests().fetch), self.cache)
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


if __name__ == "__main__":
    unittest.main()
