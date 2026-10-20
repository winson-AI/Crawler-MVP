import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from jobcrawler.crawler import cache_path, crawl, load_cache, save_cache
from jobcrawler.server import make_server
from jobcrawler.sources import SOURCES, get_source, load_custom_sources, register_custom_source
from jobcrawler.sources.custom import CustomSource, _public_socket, fetch_document, public_url, validate_config
from jobcrawler.sync import sync_jobs


def config(**overrides):
    return {"id": "custom_test", "name": "测试职位网站", "url": "https://careers.example.com/jobs",
            "parser": "json", "items": "jobs", **overrides}


def row(number="1", **overrides):
    return {"id": number, "title": "Python研发工程师", "url": "/job/" + number,
            "company": "测试公司", "locations": ["上海"], "description": "平台研发",
            "requirements": "3-5年Python经验", **overrides}


class CustomParserTests(unittest.TestCase):
    def test_json_mapping_and_shared_job_schema(self):
        adapter = CustomSource(config(items="data.posts", fields={"id": "code", "title": "name",
            "url": "link", "locations": "cities", "requirements": "requirements"}))
        page = adapter.parse_document(json.dumps({"data": {"posts": [{"code": "1", "name": "Python研发",
            "link": "/job/1", "cities": ["上海"], "requirements": "3-5年Python经验"}]}}))
        self.assertEqual(page.jobs[0]["url"], "https://careers.example.com/job/1")
        self.assertEqual(page.jobs[0]["source"], "custom_test")
        self.assertEqual(page.jobs[0]["experience"]["min"], 3)
        self.assertIn("Python", page.jobs[0]["technologies"])

    def test_json_ld_graph_normalizes_jobposting(self):
        document = '<script type="application/ld+json">' + json.dumps({"@graph": [
            {"@type": "Organization", "name": "non-job"},
            {"@type": ["Thing", "JobPosting"], "identifier": {"value": "schema-1"},
             "title": "Python工程师", "url": "/job/1", "description": "<p>研发平台</p>",
             "hiringOrganization": {"name": "公司"}, "jobLocation": {"address": {"addressLocality": "上海"}},
             "qualifications": "3年以上Python", "datePosted": "2026-10-20"}]}) + '</script>'
        adapter = CustomSource(config(parser="auto"))
        page = adapter.parse_document(document)
        self.assertEqual(adapter.parser_used, "schema")
        self.assertEqual(page.jobs[0]["id"], "schema-1")
        self.assertEqual(page.jobs[0]["locations"], ["上海"])
        self.assertEqual(page.jobs[0]["description"], "研发平台")

    def test_dom_selectors_relative_urls_and_missing_detail_markers(self):
        adapter = CustomSource(config(parser="dom", items=".card", fields={"title": ".title", "url": ".title",
            "id": {"selector": "", "attr": "data-id"}, "company": ".company", "locations": ".place"}))
        page = adapter.parse_document('<article class="card" data-id="one"><a class="title" href="/job/1">工程师</a>'
                                      '<b class="company">公司</b><i class="place">上海</i><i class="place">北京</i></article>')
        self.assertEqual(page.jobs[0]["id"], "one")
        self.assertEqual(page.jobs[0]["locations"], ["上海", "北京"])
        self.assertTrue(page.jobs[0]["details_pending"])

    def test_style_drift_and_login_document_fail_instead_of_empty_success(self):
        adapter = CustomSource(config(parser="dom", items=".card", fields={"title": ".title", "url": "a"}))
        for document in ('<h1>请登录或完成验证码</h1>', '<div class="new-card">job</div>',
                         '<div class="card"><a href="/job/1">样式改变</a></div>'):
            with self.subTest(document=document), self.assertRaises(ValueError):
                adapter.parse_document(document)
        with self.assertRaises(ValueError):
            CustomSource(config()).parse_document('{"error":"login required"}')

    def test_required_fields_allow_detecting_detail_layout_drift(self):
        adapter = CustomSource(config(required_fields=["description"]))
        with self.assertRaisesRegex(ValueError, "description"):
            adapter.parse_document(json.dumps({"jobs": [row(description="")]}))

    def test_identity_fallback_is_stable_and_bad_links_are_rejected(self):
        adapter = CustomSource(config())
        document = json.dumps({"jobs": [row(id="")]})
        first = adapter.parse_document(document).jobs[0]["id"]
        self.assertEqual(adapter.parse_document(document).jobs[0]["id"], first)
        for url in ("javascript:alert(1)", "file:///etc/passwd", "http://127.0.0.1/secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                adapter.parse_document(json.dumps({"jobs": [row(url=url)]}))

    def test_explicit_empty_array_and_empty_selector_are_allowed(self):
        self.assertFalse(CustomSource(config()).parse_document('{"jobs":[]}').has_more)
        adapter = CustomSource(config(parser="dom", items=".card", empty_selector=".empty"))
        self.assertEqual(adapter.parse_document('<p class="empty">没有职位</p>').jobs, [])

    def test_json_pagination_and_page_size_guard(self):
        adapter = CustomSource(config(pagination={"url": "https://careers.example.com/jobs?page={page}",
                                                   "total_path": "total", "page_size": 2}))
        first = adapter.parse_document(json.dumps({"jobs": [row("1"), row("2")], "total": 3}))
        last = adapter.parse_document(json.dumps({"jobs": [row("3")], "total": 3}), page=2)
        self.assertTrue(first.has_more)
        self.assertFalse(last.has_more)
        with self.assertRaisesRegex(ValueError, "页容量"):
            adapter.parse_document(json.dumps({"jobs": [row("1")], "total": 3}))

    def test_dom_pagination_uses_explicit_next_boundary(self):
        adapter = CustomSource(config(parser="dom", items=".card", fields={"title": "a", "url": "a"},
            pagination={"url": "https://careers.example.com/jobs?page={page}", "next_selector": "a.next"}))
        first = adapter.parse_document('<div class="card"><a href="/job/1">工程师</a></div><a class="next">下一页</a>')
        last = adapter.parse_document('<div class="card"><a href="/job/2">工程师2</a></div>'
                                     '<a class="next disabled">下一页</a>', page=2)
        self.assertTrue(first.has_more)
        self.assertEqual((last.total, last.has_more), (2, False))

    def test_document_mode_never_replays_saved_document_as_live_refresh(self):
        with self.assertRaisesRegex(ValueError, "旧文档"):
            CustomSource(config(mode="document")).fetch_page(1)

    def test_invalid_configuration_is_not_executable_or_a_path(self):
        for invalid in (config(id="../../secret"), config(id="baidu"), config(id=[]), config(script="evil()"),
                        config(fields={"title": {"script": "evil()"}}), config(required_fields=["unknown"]),
                        config(parser="dom", items="div[["), config(pagination={"url": "https://example.com"})):
            with self.subTest(config=invalid), self.assertRaises(ValueError):
                validate_config(invalid)

    def test_private_urls_and_credentials_are_blocked(self):
        for url in ("http://localhost/jobs", "http://127.0.0.1/jobs", "http://[::1]/jobs", "http://10.0.0.1/jobs",
                    "http://169.254.169.254/latest", "https://name:password@example.com/jobs", "file:///tmp/jobs"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)
        self.assertEqual(public_url("https://example.com/jobs?page=1&_security_check=secret&securityId=secret"),
                         "https://example.com/jobs?page=1")

    def test_dns_rebinding_and_mixed_private_answers_are_blocked_before_connect(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("jobcrawler.sources.custom.socket.getaddrinfo", return_value=entries), \
                patch("jobcrawler.sources.custom.socket.socket") as sock:
            with self.assertRaises(ValueError):
                _public_socket(("example.com", 443), 20)
            sock.assert_not_called()

    def test_public_socket_connects_to_resolved_numeric_address(self):
        entries = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch("jobcrawler.sources.custom.socket.getaddrinfo", return_value=entries), \
                patch("jobcrawler.sources.custom.socket.socket") as sock:
            self.assertIs(_public_socket(("example.com", 443), 20), sock.return_value)
            sock.return_value.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_redirect_to_private_network_is_not_followed(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 302
        connection.getresponse.return_value.getheader.return_value = "http://127.0.0.1/private"
        with patch("jobcrawler.sources.custom.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(ValueError):
                fetch_document("https://example.com/jobs")
        connection.close.assert_called_once()


class CustomMountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry_patch = patch.dict(SOURCES)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.config_path = self.root / "sources.json"
        self.cache = self.root / "data/jobs.json"
        self.server = make_server(port=0, cache=self.cache, custom_sources=self.config_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, route, body, **headers):
        request = Request(self.url + route, data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", **headers})
        with urlopen(request) as response:
            return response.status, json.load(response)

    def get(self, route):
        with urlopen(self.url + route) as response:
            return json.load(response)

    def test_preview_is_read_only_and_document_mount_populates_server(self):
        body = {"config": config(mode="document"), "document": json.dumps({"jobs": [row()]})}
        code, result = self.post("/api/sources/preview", body)
        self.assertEqual((code, result["collected"]), (200, 1))
        self.assertFalse(self.config_path.exists())
        self.assertNotIn("custom_test", SOURCES)
        code, result = self.post("/api/sources/custom", body)
        self.assertTrue(result["mounted"])
        api = self.get("/api/jobs?" + urlencode({"source": "custom_test", "selector": json.dumps({"locations": ["上海"]})}))
        self.assertEqual(api["total"], 1)
        snapshot = self.get("/api/status?source=custom_test")["snapshot"]
        self.assertEqual(snapshot["scope"], "document")
        self.assertEqual(snapshot["acquisition"], "provided_document")
        self.assertTrue(any(s["id"] == "custom_test" for s in self.get("/api/sources")["sources"]))
        with self.assertRaises(HTTPError) as error:
            self.post("/api/crawl", {"source": "custom_test"})
        self.assertEqual(error.exception.code, 400)

    def test_layout_drift_preserves_configuration_and_existing_jobs(self):
        body = {"config": config(mode="document"), "document": json.dumps({"jobs": [row()]})}
        self.post("/api/sources/custom", body)
        old_config = self.config_path.read_bytes()
        old_cache = cache_path("custom_test", self.cache).read_bytes()
        body["document"] = '{"error":"login"}'
        with self.assertRaises(HTTPError):
            self.post("/api/sources/custom", body)
        self.assertEqual(old_config, self.config_path.read_bytes())
        self.assertEqual(old_cache, cache_path("custom_test", self.cache).read_bytes())

    def test_http_mount_fetches_and_refreshes_real_configured_pages(self):
        document = json.dumps({"jobs": [row()]})
        with patch("jobcrawler.sources.custom.fetch_document", return_value=(document, config()["url"])) as fetch:
            code, result = self.post("/api/sources/custom", {"config": config()})
            self.assertEqual(code, 202)
            deadline = time.monotonic() + 3
            while self.get("/api/status?source=custom_test")["running"] and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            self.assertFalse(self.get("/api/status?source=custom_test")["running"])
            self.assertGreaterEqual(fetch.call_count, 2)
        self.assertEqual(self.get("/api/jobs?source=custom_test")["total"], 1)

    def test_offline_sample_cannot_be_mounted_as_http_validation(self):
        with self.assertRaises(HTTPError):
            self.post("/api/sources/custom", {"config": config(), "document": json.dumps({"jobs": [row()]})})
        self.assertFalse(self.config_path.exists())

    def test_configuration_reload_and_cli_sync_use_same_registry(self):
        register_custom_source(config(), self.config_path)
        del SOURCES["custom_test"]
        load_custom_sources(self.config_path)
        self.assertIsInstance(get_source("custom_test"), CustomSource)
        with patch("jobcrawler.sources.custom.fetch_document", return_value=(json.dumps({"jobs": [row()]}), config()["url"])):
            result = sync_jobs(sources=["custom_test"], cache=self.cache, state_path=self.root / "state.json",
                               output=self.root / "runs", delay=0.1)
        self.assertEqual(result["sources"]["custom_test"]["status"], "ok")
        self.assertEqual(result["sources"]["custom_test"]["collected"], 1)

    def test_missing_details_remain_pending_through_generic_sync(self):
        register_custom_source(config(), self.config_path)
        with patch("jobcrawler.sources.custom.fetch_document", return_value=(json.dumps({"jobs": [row(description="", requirements="")]}), config()["url"])):
            result = sync_jobs(sources=["custom_test"], cache=self.cache, state_path=self.root / "state.json", output=self.root / "runs")
        self.assertEqual(result["sources"]["custom_test"]["pending_detail_count"], 1)
        self.assertFalse(result["sources"]["custom_test"]["details_complete"])

    def test_cross_origin_write_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.post("/api/sources/custom", {"config": config()}, Origin="https://evil.example")
        self.assertEqual(error.exception.code, 403)

    def test_host_rebinding_cannot_manage_sources(self):
        with self.assertRaises(HTTPError) as error:
            self.post("/api/sources/custom", {"config": config()}, Host="evil.example")
        self.assertEqual(error.exception.code, 403)

    def test_updated_configuration_marks_old_snapshot_as_needing_refresh(self):
        self.post("/api/sources/custom", {"config": config(mode="document"), "document": json.dumps({"jobs": [row()]})})
        register_custom_source(config(mode="document", required_fields=["title", "url", "description"]), self.config_path)
        self.assertTrue(self.get("/api/status?source=custom_test")["configuration_changed"])

    def test_url_only_mount_adapts_document_and_remount_reuses_learned_config(self):
        body = {"url": "https://example.com/jobs", "document": json.dumps({"data": {"positions": [
            {"positionName": "Python工程师", "detailUrl": "/position/1", "cityName": "上海"}]}})}
        code, result = self.post("/api/sources/custom", body)
        self.assertEqual(code, 200)
        self.assertEqual(result["adaptation"]["status"], "adapted")
        identity = result["config"]["id"]
        self.assertEqual(result["config"]["items"], "data.positions")
        self.assertEqual(self.get("/api/jobs?source=" + identity)["total"], 1)
        self.assertEqual(self.get("/api/status?source=" + identity)["snapshot"]["adaptation"]["status"], "adapted")
        _, matched = self.post("/api/templates/match", {"url": body["url"]})
        self.assertEqual(matched["template"]["kind"], "learned")
        self.assertEqual(matched["config"]["id"], identity)
        self.assertEqual(len(self.get("/api/templates")["learned"]), 1)
        _, second = self.post("/api/sources/custom", body)
        self.assertIsNone(second["adaptation"])

    def test_url_template_preview_does_not_mount_and_uses_document_provenance(self):
        body = {"url": "https://jobs.ashbyhq.com/company", "document": json.dumps({"jobs": [
            {"title": "Engineer", "jobUrl": "https://jobs.ashbyhq.com/company/1"}]})}
        _, result = self.post("/api/sources/preview", body)
        self.assertEqual(result["template_id"], "ashby")
        self.assertEqual(result["acquisition"], "provided_document")
        self.assertEqual(result["scope"], "document")
        self.assertFalse(self.config_path.exists())

    def test_refresh_persists_successful_adaptation_but_not_failed_later_page(self):
        register_custom_source(config(), self.config_path)
        document = json.dumps({"changed": {"jobs": [row()]}})
        with patch("jobcrawler.sources.custom.fetch_document", return_value=(document, config()["url"])):
            self.post("/api/crawl", {"source": "custom_test"})
            deadline = time.monotonic() + 3
            while self.get("/api/status?source=custom_test")["running"] and time.monotonic() < deadline:
                threading.Event().wait(0.01)
        self.assertEqual(json.loads(self.config_path.read_text())["sources"][0]["items"], "changed.jobs")
        self.assertFalse(self.get("/api/status?source=custom_test")["configuration_changed"])
        saved = config(pagination={"url": "https://careers.example.com/jobs?page={page}", "page_size": 1, "total_path": "total"})
        register_custom_source(saved, self.config_path)
        old_config = self.config_path.read_bytes()
        old_jobs = cache_path("custom_test", self.cache).read_bytes()
        with patch("jobcrawler.sources.custom.fetch_document", side_effect=[
                (json.dumps({"new": {"jobs": [row()]}, "total": 2}), config()["url"]),
                ('{"error":"authentication required"}', config()["url"]) ]):
            self.post("/api/crawl", {"source": "custom_test"})
            deadline = time.monotonic() + 4
            while self.get("/api/status?source=custom_test")["running"] and time.monotonic() < deadline:
                threading.Event().wait(0.02)
        self.assertEqual(self.config_path.read_bytes(), old_config)
        self.assertEqual(cache_path("custom_test", self.cache).read_bytes(), old_jobs)
        self.assertEqual(get_source("custom_test").config["items"], "jobs")
