import csv
import importlib
import io
import json
import unittest
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobcrawler.crawler import crawl, export_csv
from jobcrawler.rankers import rank_jobs
from jobcrawler.selectors import select_jobs
from jobcrawler.sources import SOURCES
from jobcrawler.sources.alibaba import AlibabaSource
from jobcrawler.sources.base import JobPage
from jobcrawler.sources.browser import BrowserSource
from jobcrawler.sources.bytedance import BytedanceSource
from jobcrawler.sources.common import iso_date, plain_text
from jobcrawler.sources.jd import JdSource
from jobcrawler.sources.meituan import MeituanSource
from jobcrawler.sources.pinduoduo import PinduoduoSource, normalize_job as normalize_pdd
from jobcrawler.sources.tencent import TencentSource
from scripts.verify_sources import crawl_with_checkpoints, verify_pipeline


def fixture(source):
    return json.loads((Path(__file__).parent / "fixtures" / f"{source}.json").read_text())


class OfficialSourceTests(unittest.TestCase):
    def test_captured_official_rows_reuse_selector_ranker_csv(self):
        for source, years in [("alibaba", 5), ("tencent", 5), ("meituan", 3), ("bytedance", 5), ("jd", 5)]:
            with self.subTest(source=source):
                normalize = importlib.import_module("jobcrawler.sources." + source).normalize_job
                job = normalize(fixture(source))
                self.assertEqual(job["source"], source)
                self.assertEqual(job["experience"]["min"], years)
                self.assertTrue(job["requirements"])
                self.assertTrue(job["description"])
                selected = select_jobs([job], {"locations": job["locations"][:1], "years": years,
                                               "keywords": [job["title"]]})
                self.assertEqual(len(selected), 1)
                self.assertEqual(select_jobs([job], {"years": years - 1}), [])
                rows = list(csv.DictReader(io.StringIO(export_csv(rank_jobs(selected)).lstrip("\ufeff"))))
                self.assertEqual(rows[0]["source"], source)
                self.assertEqual(rows[0]["requirements"], job["requirements"])

    def test_timestamps_not_updates_and_structured_experience_has_priority(self):
        from jobcrawler.sources.alibaba import normalize_job
        raw = fixture("alibaba")
        raw["requirement"] = "3年以上Python经验"
        job = normalize_job(raw)
        self.assertEqual(job["experience"]["min"], 5)
        self.assertIn("Python", job["technologies"])
        for source in ("tencent", "meituan"):
            job = importlib.import_module("jobcrawler.sources." + source).normalize_job(fixture(source))
            self.assertEqual(job["published_at"], "")
            self.assertTrue(job["updated_at"])
        self.assertEqual(iso_date("2026年10月10日"), "2026-10-10")
        self.assertEqual(iso_date(1791561600000), "2026-10-09T16:00:00+00:00")
        self.assertEqual(iso_date("unknown"), "")

    def test_pdd_contract_mapping_is_not_a_live_fixture(self):
        job = normalize_pdd({"code": "test-only", "name": "Java研发", "workLocation": "上海/广州",
                             "serveRequirement": "<p>3–5年开发经验，掌握Java</p>",
                             "jobDuty": "<p>研发</p>", "updateTime": "2026-10-10", "recruitType": "研发"})
        self.assertEqual(job["locations"], ["上海", "广州"])
        self.assertEqual(job["experience"]["max"], 5)
        self.assertEqual(job["published_at"], "")
        self.assertEqual(job["technologies"], ["Java"])
        self.assertEqual(plain_text("C++ template<T>"), "C++ template<T>")

    def test_pdd_captured_real_detail_supports_stack_and_unknown_year_filters(self):
        job = normalize_pdd(fixture("pinduoduo"))
        self.assertEqual(job["id"], "T021220")
        self.assertEqual(job["locations"], ["上海"])
        self.assertEqual(job["experience"]["source"], "unknown")
        selector = {"locations": ["上海"], "years": 3, "include_unknown_experience": True,
                    "technologies": ["PyTorch", "TensorFlow"], "keywords": ["算法", "研发"]}
        self.assertEqual(len(select_jobs([job], selector)), 1)
        self.assertEqual(select_jobs([job], {**selector, "include_unknown_experience": False}), [])
        self.assertNotIn("<p>", job["requirements"])

    @patch("jobcrawler.sources.alibaba.HttpClient")
    def test_alibaba_bootstrap_csrf_and_reported_page(self, factory):
        client = factory.return_value
        client.request.side_effect = ['__token__: "public-test-token", offCampus: "group_official_site"',
            {"success": True, "content": {"datas": [fixture("alibaba")], "totalCount": 30, "currentPage": 2}}]
        page = AlibabaSource().fetch_page(2)
        self.assertEqual((page.page, page.total), (2, 30))
        url, body = client.request.call_args.args
        self.assertIn("_csrf=public-test-token", url)
        self.assertEqual(body["pageIndex"], 2)
        self.assertEqual(body["channel"], "group_official_site")

    @patch("jobcrawler.sources.tencent.time.sleep")
    @patch("jobcrawler.sources.tencent.HttpClient")
    def test_tencent_requires_details_and_id_consistency(self, factory, sleep):
        client = factory.return_value
        raw = fixture("tencent")
        listing = {"Code": 200, "Data": {"Count": 15, "Posts": [{k: raw[k] for k in
            ("PostId", "RecruitPostName", "LocationName", "LastUpdateTime") if k in raw}]}}
        client.request.side_effect = [listing, {"Code": 200, "Data": raw}]
        page = TencentSource().fetch_page(2)
        self.assertEqual(page.jobs[0]["requirements"], raw["Requirement"])
        self.assertIn("pageIndex=2", client.request.call_args_list[0].args[0])
        self.assertIn("attrId=1", client.request.call_args_list[0].args[0])
        client.request.side_effect = [listing, {"Code": 200, "Data": {**raw, "PostId": "different"}}]
        with self.assertRaisesRegex(RuntimeError, "ID"):
            TencentSource().fetch_page(1)

    @patch("jobcrawler.sources.meituan.HttpClient")
    def test_meituan_social_filter_and_page_validation(self, factory):
        raw = fixture("meituan")
        factory.return_value.request.return_value = {"status": 1, "data": {
            "list": [raw], "page": {"pageNo": 2, "totalCount": 30}}}
        page = MeituanSource().fetch_page(2)
        self.assertEqual(page.page, 2)
        self.assertEqual(factory.return_value.request.call_args.args[1]["jobType"], [{"code": "3", "subCode": []}])
        raw["jobType"] = "1"
        with self.assertRaisesRegex(RuntimeError, "非社招"):
            MeituanSource().fetch_page(2)

    @patch("jobcrawler.sources.meituan.HttpClient")
    def test_meituan_reports_last_page_even_when_rows_contain_duplicate_ids(self, factory):
        raw = fixture("meituan")
        factory.return_value.request.return_value = {"status": 1, "data": {
            "list": [raw, raw], "page": {"pageNo": 3, "totalCount": 25}}}
        page = MeituanSource().fetch_page(3)
        self.assertFalse(page.has_more)
        self.assertEqual(page.jobs[0]["id"], page.jobs[1]["id"])

    @patch("jobcrawler.sources.jd.HttpClient")
    def test_jd_count_and_pagination_form(self, factory):
        factory.return_value.request.side_effect = [242, [fixture("jd")]]
        page = JdSource().fetch_page(2)
        self.assertEqual(page.total, 242)
        args = factory.return_value.request.call_args
        self.assertEqual(args.args[1]["pageIndex"], 2)
        self.assertTrue(args.kwargs["form"])
        self.assertTrue(page.has_more)

    def test_jd_distinct_publications_can_share_a_requirement(self):
        from jobcrawler.sources.jd import normalize_job
        raw = fixture("jd")
        other = {**raw, "id": raw["id"] + 1}
        self.assertNotEqual(normalize_job(raw)["id"], normalize_job(other)["id"])

    def test_bytedance_page_offset_and_recruit_type(self):
        source = BytedanceSource()
        source._context = object()
        raw = fixture("bytedance")
        with patch.object(source, "load_response", return_value={"code": 0, "data": {"job_post_list": [raw], "count": 25}}) as fetch:
            page = source.fetch_page(2)
            self.assertEqual(page.total, 25)
            self.assertIn("current=2", fetch.call_args.args[0])
            predicate = fetch.call_args.kwargs["request_matches"]
            self.assertTrue(predicate(MagicMock(post_data_json={"offset": 100})))
            self.assertFalse(predicate(MagicMock(post_data_json={"offset": 0})))
            raw["recruit_type"]["parent"]["id"] = "2"
            with self.assertRaisesRegex(RuntimeError, "非社招"):
                source.fetch_page(2)
        source._context = None

    def test_browser_error_does_not_become_zero_jobs(self):
        response = MagicMock(status=400)
        response.json.return_value = {"success": False, "errorCode": 400023, "errorMsg": "获取失败", "result": None}
        source = SOURCES["pinduoduo"]
        with self.assertRaisesRegex(RuntimeError, "400023.*获取失败"):
            source.response_payload(response)

    def test_pdd_pagination_contract_uses_normal_next_button_and_details(self):
        source = PinduoduoSource()
        source._context = MagicMock()
        page = source._context.new_page.return_value
        response = MagicMock(status=200)
        rows = [{"code": f"test-{i}", "name": "Java工程师"} for i in range(1, 52)]
        response.json.side_effect = [{"success": True, "result": {"list": rows[:10], "total": 51}},
                                     {"success": True, "result": {"list": rows[:50], "total": 51}},
                                     {"success": True, "result": {"list": rows[50:], "total": 51}}]
        page.expect_response.return_value.__enter__.return_value.value = response
        details = [{"success": True, "result": {"code": code, "name": "Java工程师", "workLocation": "上海",
                    "serveRequirement": "3年以上开发经验，熟悉Java", "jobDuty": "后端研发"}}
                   for code in (row["code"] for row in rows)]
        with patch.object(source, "fetch_detail", side_effect=details), patch("jobcrawler.sources.pinduoduo.time.sleep"):
            first, second = source.fetch_page(1), source.fetch_page(2)
        self.assertEqual([first.jobs[0]["id"], second.jobs[0]["id"]], ["test-1", "test-51"])
        self.assertEqual(len(first.jobs), 50)
        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)
        self.assertEqual(second.jobs[0]["experience"]["min"], 3)
        self.assertEqual(source.listing_setup_requests, 1)
        self.assertEqual([call.args[0] for call in page.get_by_text.call_args_list], ["10 条/页", "50 条/页"])
        page.goto.assert_called_once()
        page.get_by_title.assert_called_once_with("下一页", exact=True)
        page.get_by_title.return_value.click.assert_called_once()
        source._context = None

    def test_pdd_rejects_page_size_selection_that_did_not_take_effect(self):
        source = PinduoduoSource()
        source._context = MagicMock()
        payload = {"success": True, "result": {"list": [{"code": "one", "name": "工程师"}], "total": 100}}
        with patch.object(source, "_load_operation", return_value=payload), patch("jobcrawler.sources.pinduoduo.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "分页大小"):
                source.fetch_list_page(1)

    def test_pdd_refresh_reuses_live_tab_and_requests_fresh_first_page(self):
        source = PinduoduoSource()
        source._context = MagicMock()
        source._listing_page = page = MagicMock()
        page.is_closed.return_value = False
        source._current_page = 16
        source.listing_setup_requests = 1
        payload = {"success": True, "result": {"list": [
            {"code": str(i), "name": "工程师"} for i in range(50)], "total": 784}}
        def load(page, endpoint, operation, action):
            action()
            return payload
        with patch.object(source, "_load_operation", side_effect=load) as request:
            result = source.fetch_list_page(1)
        self.assertEqual(len(result.jobs), 50)
        self.assertTrue(source.listing_page_reused)
        self.assertEqual(source.listing_setup_requests, 1)
        self.assertEqual(request.call_args.args[2], {"page": 1})
        page.get_by_role.assert_called_once_with("button", name="搜 索", exact=True)
        page.get_by_role.return_value.click.assert_called_once()
        page.goto.assert_not_called()
        page.close.assert_not_called()
        page.get_by_text.assert_not_called()
        source._context.new_page.assert_not_called()

    def test_pdd_nested_sessions_borrow_browser_until_owner_exits(self):
        source = PinduoduoSource()
        calls = []
        @contextmanager
        def owner(adapter):
            calls.append("open")
            adapter._context = object()
            try:
                yield
            finally:
                calls.append("close")
                adapter._context = None
        with patch.object(BrowserSource, "session", owner):
            with source.session():
                context = source._context
                with source.session():
                    self.assertIs(source._context, context)
                self.assertEqual(calls, ["open"])
            self.assertEqual(calls, ["open", "close"])

    def test_browser_session_closed_after_failed_crawl(self):
        events = []

        class TestBrowser(BrowserSource):
            id, name, url = "testbrowser", "test", "https://example.com"

            @contextmanager
            def session(self):
                events.append("open")
                try:
                    yield
                finally:
                    events.append("close")

            def fetch_page(self, page):
                raise RuntimeError("获取失败")

        with patch.dict(SOURCES, testbrowser=TestBrowser()):
            with self.assertRaisesRegex(RuntimeError, "获取失败"):
                crawl(source="testbrowser")
        self.assertEqual(events, ["open", "close"])

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "需要可选 Playwright 依赖")
    def test_pdd_waits_for_final_success_instead_of_consuming_verification_placeholder(self):
        source = PinduoduoSource()
        first = MagicMock(status=200)
        first.json.return_value = {"success": False, "errorCode": 54001, "result": {"name": None}}
        final = MagicMock(status=200)
        final.json.return_value = {"success": True, "result": {"code": "test-only", "name": "工程师"}}
        page = MagicMock()
        page.expect_response.return_value.__enter__.return_value.value = final
        self.assertEqual(source.read_response(page, first, "/api/recruit/position/detail")["result"]["name"], "工程师")
        page.close.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "需要可选 Playwright 依赖")
    def test_pdd_verification_timeout_is_not_an_empty_success(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        first = MagicMock(status=200)
        first.json.return_value = {"success": False, "errorCode": 54001, "result": {"name": None}}
        page = MagicMock()
        page.expect_response.side_effect = BrowserTimeout("verification not finished")
        with self.assertRaisesRegex(RuntimeError, "验证响应超时"):
            PinduoduoSource().read_response(page, first, "/api/recruit/position/detail")

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "需要可选 Playwright 依赖")
    def test_pdd_captures_sdk_success_before_navigation_returns(self):
        source = PinduoduoSource()
        source._context = MagicMock()
        page = source._context.new_page.return_value
        first = MagicMock(status=200, url="https://careers.pddglobalhr.com/api/recruit/position/detail")
        first.request.post_data_json = {"code": "one"}
        first.json.return_value = {"success": False, "errorCode": 54001}
        final = MagicMock(status=200, url=first.url)
        final.request.post_data_json = {"code": "one"}
        final.json.return_value = {"success": True, "result": {"code": "one", "name": "真实响应"}}
        unrelated = MagicMock(status=200, url=first.url)
        unrelated.request.post_data_json = {"code": "another-job"}
        listeners = []
        page.on.side_effect = lambda event, callback: listeners.append(callback)

        def navigate(*args, **kwargs):
            for response in (first, unrelated, final):
                listeners[0](response)

        page.goto.side_effect = navigate
        result = source.fetch_detail("one")
        self.assertEqual(result, final.json.return_value)
        unrelated.json.assert_not_called()
        page.expect_response.assert_called_once()
        page.remove_listener.assert_called_once_with("response", listeners[0])

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "需要可选 Playwright 依赖")
    def test_pdd_buffered_verification_failure_is_reported_immediately(self):
        source = PinduoduoSource()
        page = MagicMock()
        first = MagicMock(status=200)
        first.json.return_value = {"success": False, "errorCode": 54001}
        rejected = MagicMock(status=400)
        rejected.json.return_value = {"errorCode": 400023, "errorMsg": "获取失败"}
        with self.assertRaisesRegex(RuntimeError, "400023"):
            source.read_response(page, first, "/detail", pending_responses=[rejected])
        page.expect_response.assert_not_called()

    def test_pdd_details_reuse_the_tab_that_holds_verification_state(self):
        source = PinduoduoSource()
        source._context = MagicMock()
        source._detail_page = None
        page = source._context.new_page.return_value
        with patch.object(source, "read_response", return_value={"success": True}):
            source.fetch_detail("one")
            source.fetch_detail("two")
        source._context.new_page.assert_called_once()
        self.assertEqual(page.goto.call_count, 2)
        page.close.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "需要可选 Playwright 依赖")
    def test_pdd_transient_verification_does_not_request_human_input(self):
        source = PinduoduoSource()
        source.verification_callback = MagicMock()
        first = MagicMock(status=200)
        first.json.return_value = {"success": False, "errorCode": 54001}
        final = MagicMock(status=200)
        final.json.return_value = {"success": True, "result": {"code": "one"}}
        page = MagicMock()
        page.get_by_placeholder.return_value.is_visible.return_value = True
        page.expect_response.return_value.__enter__.return_value.value = final
        with patch("jobcrawler.sources.pinduoduo.time.monotonic", return_value=0):
            self.assertTrue(source.read_response(page, first, "/detail")["success"])
        source.verification_callback.assert_not_called()
        page.bring_to_front.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("playwright"), "需要可选 Playwright 依赖")
    def test_pdd_short_timeout_still_records_visible_human_challenge(self):
        source = PinduoduoSource()
        source.verification_timeout = 2
        source.verification_callback = MagicMock(return_value="verification.json")
        first = MagicMock(status=200)
        first.json.return_value = {"success": False, "errorCode": 54001}
        page = MagicMock()
        page.get_by_placeholder.return_value.is_visible.return_value = True
        with patch("jobcrawler.sources.pinduoduo.time.monotonic", side_effect=[0, 2, 2]):
            with self.assertRaisesRegex(RuntimeError, "验证响应超时"):
                source.read_response(page, first, "/detail")
        self.assertEqual(source.verification_callback.call_args.args[-1], "needs_human")
        page.get_by_placeholder.return_value.fill.assert_not_called()

    def test_http_checkpoint_resumes_after_failed_page(self):
        raw = fixture("alibaba")
        normalize = importlib.import_module("jobcrawler.sources.alibaba").normalize_job
        calls = []

        class Pages:
            id, name, url = "alibaba", "test", "https://example.com"
            fail = True

            def fetch_page(self, page):
                calls.append(page)
                if page == 2 and self.fail:
                    raise RuntimeError("temporary upstream failure")
                return JobPage(page, 2, [normalize({**raw, "id": page})], has_more=page == 1)

        adapter = Pages()
        with tempfile.TemporaryDirectory() as directory, patch.dict(SOURCES, alibaba=adapter), patch("jobcrawler.crawler.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                crawl_with_checkpoints("alibaba", 0, Path(directory))
            adapter.fail = False
            result = crawl_with_checkpoints("alibaba", 0, Path(directory))
        self.assertTrue(result["complete"])
        self.assertEqual(calls, [1, 2, 2])

    def test_full_pipeline_reports_missing_upstream_fields_and_safe_csv(self):
        normalize = importlib.import_module("jobcrawler.sources.alibaba").normalize_job
        first = normalize(fixture("alibaba"))
        second = {**first, "id": "another", "title": "=literal", "requirements": "", "description": ""}
        snapshot = {"source_id": "alibaba", "jobs": [first, second], "complete": True}
        with tempfile.TemporaryDirectory() as directory:
            checks = verify_pipeline(snapshot, Path(directory))
            self.assertEqual(checks["missing_requirements"], 1)
            self.assertEqual(checks["all_exported"], 2)
            self.assertIn("'=literal", (Path(directory) / "alibaba-all.csv").read_text())

    def test_business_jobs_without_known_technical_terms_are_valid_samples(self):
        normalize = importlib.import_module("jobcrawler.sources.alibaba").normalize_job
        raw = {**fixture("alibaba"), "name": "税务经理", "requirement": "5年以上税务经验", "description": "税务管理"}
        job = normalize(raw)
        with tempfile.TemporaryDirectory() as directory:
            checks = verify_pipeline({"source_id": "alibaba", "jobs": [job]}, Path(directory))
        self.assertFalse(checks["technology_sample_available"])
        self.assertEqual(checks["matched"], 1)


if __name__ == "__main__":
    unittest.main()
