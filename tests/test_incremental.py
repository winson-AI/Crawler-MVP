import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobcrawler.crawler import cache_path, load_cache, save_cache
from jobcrawler.models import build_job
from jobcrawler.sources.base import JobPage
from jobcrawler.sources.pinduoduo import PinduoduoSource
from jobcrawler.sync import sync_jobs
from jobcrawler.verification import VerificationRequired, record_verification, record_visual_review


def job(job_id, updated="2026-10-18", requirements="3-5年Python经验"):
    return build_job(id=job_id, title="Python工程师" + job_id, source="tencent", company="腾讯",
        url="https://example.com/" + job_id, locations=["北京"], updated_at=updated,
        requirements=requirements, description="平台研发")


class SplitSource:
    id, name, url = "tencent", "test", "https://example.com"
    verification_callback = None
    verification_timeout = 180

    def __init__(self, jobs):
        self.jobs = jobs
        self.list_calls, self.detail_calls = [], []
        self.fail_page = self.fail_detail = self.pause_detail = None

    def fetch_list_page(self, page):
        self.list_calls.append(page)
        if page == self.fail_page:
            raise RuntimeError("temporary list failure")
        rows = self.jobs[(page-1)*2:page*2]
        summaries = [{**j, "requirements": "", "description": ""} for j in rows]
        return JobPage(page, len(self.jobs), summaries, has_more=page*2 < len(self.jobs))

    def fetch_job_detail(self, summary):
        self.detail_calls.append(summary["id"])
        if summary["id"] == self.fail_detail:
            raise RuntimeError("temporary detail failure")
        if summary["id"] == self.pause_detail:
            evidence = self.verification_callback(MagicMock(), "/detail", {"code": summary["id"]}, "needs_human")
            raise VerificationRequired("human verification needed", evidence)
        return next(j for j in self.jobs if j["id"] == summary["id"])


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cache, self.state = self.root / "data/jobs.json", self.root / "data/state.json"
        self.adapter = SplitSource([job("1"), job("2"), job("3")])
        adapter_patch = patch("jobcrawler.incremental.get_source", side_effect=lambda s: self.adapter)
        adapter_patch.start()
        self.addCleanup(adapter_patch.stop)

    def seed(self, jobs):
        save_cache({"source_id": "tencent", "complete": True, "jobs": jobs}, cache_path("tencent", self.cache))

    def run_sync(self):
        return sync_jobs(sources=["tencent"], cache=self.cache, state_path=self.state,
                         output=self.root / "output", delay=0.1)

    def resume(self, report):
        return sync_jobs(resume=Path(report["report_path"]).parent)

    def test_list_first_fetches_only_new_or_changed_details_and_exports_both(self):
        self.seed([job("1"), job("2")])
        self.adapter.jobs[1] = job("2", "2026-10-19", "5年以上Python经验")
        result = self.run_sync()
        entry = result["sources"]["tencent"]
        self.assertEqual(self.adapter.list_calls, [1, 2])
        self.assertEqual(self.adapter.detail_calls, ["2", "3"])
        self.assertEqual((entry["new_count"], entry["updated_count"]), (1, 1))
        self.assertEqual(entry["acquisition"]["legacy_cache_hits"], 1)
        for field, expected in [("json", "3"), ("updated_json", "2")]:
            self.assertEqual([j["id"] for j in json.loads(Path(entry[field]).read_text())["jobs"]], [expected])
        repeated = self.run_sync()["sources"]["tencent"]
        self.assertEqual(self.adapter.detail_calls, ["2", "3"])
        self.assertEqual((repeated["new_count"], repeated["updated_count"]), (0, 0))
        self.assertEqual(repeated["acquisition"]["detail_cache_hits"], 3)
        self.assertEqual(repeated["acquisition"]["detail_requests"], 0)

    def test_list_failure_resumes_pages_before_any_detail_request(self):
        self.adapter.fail_page = 2
        first = self.run_sync()
        self.assertEqual(first["failed_sources"], ["tencent"])
        self.assertEqual(self.adapter.detail_calls, [])
        self.adapter.fail_page = None
        resumed = self.resume(first)
        self.assertEqual(resumed["new_count"], 3)
        self.assertEqual(self.adapter.list_calls, [1, 2, 2])
        self.assertEqual(resumed["sources"]["tencent"]["acquisition"]["listing_cache_pages"], 1)

    def test_stuck_page_is_quarantined_and_retried_instead_of_reused_forever(self):
        fetch = self.adapter.fetch_list_page

        def stuck(page):
            result = fetch(page)
            if page == 2:
                result.jobs = []
            return result

        with patch.object(self.adapter, "fetch_list_page", side_effect=stuck):
            first = self.run_sync()
        self.assertEqual(first["failed_sources"], ["tencent"])
        checkpoint = Path(first["sources"]["tencent"]["checkpoint"])
        self.assertEqual(len(list(checkpoint.glob("invalid-page-2-*.json"))), 1)
        self.assertFalse((checkpoint / "pages/0002.json").exists())
        resumed = self.resume(first)
        self.assertEqual(resumed["new_count"], 3)
        self.assertEqual(self.adapter.list_calls, [1, 2, 2])

    def test_detail_failure_resumes_without_listing_or_completed_detail_requests(self):
        self.adapter.fail_detail = "2"
        first = self.run_sync()
        self.assertEqual(first["failed_sources"], ["tencent"])
        self.assertEqual(json.loads(self.state.read_text())["sources"]["tencent"]["seen_ids"], [])
        self.assertFalse(cache_path("tencent", self.cache).exists())
        self.adapter.fail_detail = None
        resumed = self.resume(first)
        self.assertEqual(self.adapter.list_calls, [1, 2])
        self.assertEqual(self.adapter.detail_calls, ["1", "2", "2", "3"])
        self.assertEqual(resumed["new_count"], 3)
        self.assertEqual(resumed["sources"]["tencent"]["acquisition"]["detail_cache_hits"], 1)

    def test_old_partial_listing_is_refreshed_without_erasing_checkpoint_evidence(self):
        self.adapter.fail_page = 2
        first = self.run_sync()
        checkpoint = Path(first["sources"]["tencent"]["checkpoint"])
        old = time.time() - 1800
        os.utime(checkpoint / "pages/0001.json", (old, old))
        self.adapter.fail_page = None
        self.adapter.jobs[0] = job("4")
        result = self.resume(first)
        self.assertEqual(self.adapter.list_calls, [1, 2, 1, 2])
        self.assertEqual(result["sources"]["tencent"]["acquisition"]["listing_restarts"], 1)
        self.assertEqual(len(list(checkpoint.glob("expired-pages-*"))), 1)
        self.assertEqual({j["id"] for j in load_cache(cache_path("tencent", self.cache), "tencent")["jobs"]}, {"4", "2", "3"})

    def test_verification_pauses_preserves_history_and_resumes_completed_details(self):
        self.seed([job("1")])
        before = cache_path("tencent", self.cache).read_bytes()
        self.adapter.pause_detail = "3"
        first = self.run_sync()
        self.assertEqual(first["paused_sources"], ["tencent"])
        self.assertEqual(first["failed_sources"], [])
        self.assertEqual(before, cache_path("tencent", self.cache).read_bytes())
        self.assertEqual(json.loads(self.state.read_text())["sources"]["tencent"]["seen_ids"], ["1"])
        evidence = Path(first["sources"]["tencent"]["verification"])
        self.assertEqual(json.loads(evidence.read_text())["status"], "needs_human")
        self.adapter.pause_detail = None
        resumed = self.resume(first)
        self.assertEqual(resumed["new_count"], 2)
        self.assertEqual(self.adapter.detail_calls, ["2", "3", "3"])
        self.assertEqual(self.adapter.list_calls, [1, 2])

    def test_failed_history_commit_retains_original_update_baseline_for_resume(self):
        self.seed([job("1")])
        self.adapter.jobs[0] = job("1", "2026-10-19")
        real_save = save_cache

        def fail_history(value, path):
            if Path(path) == self.state and len(value["sources"]["tencent"]["seen_ids"]) > 1:
                raise OSError("history disk failure")
            return real_save(value, path)

        with patch("jobcrawler.sync.save_cache", side_effect=fail_history):
            first = self.run_sync()
        self.assertEqual(first["failed_sources"], ["tencent"])
        calls = list(self.adapter.detail_calls)
        resumed = self.resume(first)
        self.assertEqual(self.adapter.detail_calls, calls)
        self.assertEqual((resumed["new_count"], resumed["updated_count"]), (2, 1))

    def test_resume_completed_run_does_not_repeat_network_or_exports(self):
        result = self.run_sync()
        calls = (list(self.adapter.list_calls), list(self.adapter.detail_calls))
        path = Path(result["sources"]["tencent"]["json"])
        modified = path.stat().st_mtime_ns
        self.resume(result)
        self.assertEqual(calls, (self.adapter.list_calls, self.adapter.detail_calls))
        self.assertEqual(modified, path.stat().st_mtime_ns)

    def test_report_write_failure_after_history_commit_does_not_erase_new_exports_on_resume(self):
        real_save = save_cache

        def fail_final_report(value, path):
            if (Path(path).name == "report.json"
                    and value.get("sources", {}).get("tencent", {}).get("status") == "ok"):
                raise OSError("report disk failure after history commit")
            return real_save(value, path)

        with patch("jobcrawler.sync.save_cache", side_effect=fail_final_report):
            with self.assertRaisesRegex(OSError, "report disk failure"):
                self.run_sync()
        run = next((self.root / "output").iterdir())
        before = (run / "tencent-new.json").read_bytes()
        calls = list(self.adapter.detail_calls)
        result = sync_jobs(resume=run)
        self.assertEqual(result["new_count"], 3)
        self.assertEqual(json.loads((run / "tencent-new.json").read_text())["jobs"], json.loads(before)["jobs"])
        self.assertEqual(self.adapter.detail_calls, calls)

    def test_old_checkpoint_cannot_roll_back_a_newer_successful_run(self):
        self.adapter.fail_detail = "2"
        old = self.run_sync()
        self.adapter.fail_detail = None
        newer = self.run_sync()
        before = cache_path("tencent", self.cache).read_bytes()
        result = self.resume(old)
        self.assertIn("更新快照", result["sources"]["tencent"]["error"])
        self.assertEqual(before, cache_path("tencent", self.cache).read_bytes())

    def test_mismatched_detail_identity_is_not_cached_or_committed(self):
        with patch.object(self.adapter, "fetch_job_detail", return_value=job("wrong-id")):
            result = self.run_sync()
        self.assertEqual(result["failed_sources"], ["tencent"])
        self.assertFalse(cache_path("tencent", self.cache).exists())
        self.assertEqual(list((self.state.parent / "details").rglob("*.json")), [])

    def test_state_evidence_contains_only_public_operation_and_no_answer(self):
        page = MagicMock()
        page.url = "https://careers.pddglobalhr.com/jobs/detail?code=one&verifyAuthToken=do-not-persist"
        path = record_verification(page, self.root, "pinduoduo", "/detail",
            {"code": "one", "verifyAuthToken": "do-not-persist", "anti_content": "do-not-persist"}, "needs_human")
        record = json.loads(Path(path).read_text())
        self.assertEqual(record["operation"], {"code": "one"})
        self.assertNotIn("do-not-persist", Path(path).read_text())
        self.assertNotIn("answer", record)
        self.assertEqual(record["browser_agent"]["page_url"], "https://careers.pddglobalhr.com/jobs/detail?code=one")
        self.assertFalse(record["browser_agent"]["may_solve_or_fill_captcha"])
        page.screenshot.assert_called_once()

    def test_browser_verification_resumes_only_on_real_success_not_visual_decision(self):
        source = PinduoduoSource()
        first = MagicMock(status=200)
        first.request.post_data_json = {"code": "one"}
        first.json.return_value = {"errorCode": 54001, "success": False}
        final = MagicMock(status=200)
        final.json.return_value = {"success": True, "result": {"code": "one"}}
        page = MagicMock()
        page.get_by_placeholder.return_value.is_visible.return_value = True
        page.expect_response.return_value.__enter__.return_value.value = final
        observer = MagicMock(return_value="visual-evidence.json")
        source.verification_callback = observer
        result = source.read_response(page, first, "/detail")
        self.assertTrue(result["success"])
        self.assertEqual([c.args[-1] for c in observer.call_args_list], ["needs_human", "cleared"])
        page.bring_to_front.assert_called_once()
        page.get_by_placeholder.return_value.fill.assert_not_called()
        page.get_by_placeholder.return_value.click.assert_not_called()

    def test_visual_ready_does_not_clear_server_verification_or_accept_answer(self):
        image = self.root / "verification.png"
        image.write_bytes(b"test-image-only")
        evidence = self.root / "verification.json"
        save_cache({"source": "pinduoduo", "status": "needs_human", "screenshot": str(image)}, evidence)
        before = evidence.read_bytes()
        review = record_visual_review(evidence, "ready")
        self.assertFalse(review["allows_resume_without_server_success"])
        self.assertEqual(evidence.read_bytes(), before)
        self.assertNotIn("answer", review)
        with self.assertRaises(ValueError):
            record_visual_review(evidence, "captcha-answer")
