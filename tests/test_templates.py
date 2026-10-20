import json
import unittest
from pathlib import Path

from jobcrawler.sources.custom import CustomSource, validate_config
from jobcrawler.sources.templates import resolve_template, template_catalog


def source(**kwargs):
    return {"id": "custom_auto", "name": "自动适配测试", "url": "https://example.com/jobs", **kwargs}


class TemplateTests(unittest.TestCase):
    def test_catalog_is_distinct_and_every_reference_is_public(self):
        catalog = template_catalog()
        self.assertEqual(len(catalog["templates"]), 24)
        self.assertEqual(len({t["id"] for t in catalog["templates"]}), 24)
        for template in catalog["templates"]:
            validate_config(source(url=template["reference"]))

    def test_provider_board_binding_and_normalization(self):
        examples = [
            ("https://job-boards.greenhouse.io/company", "greenhouse", "https://boards-api.greenhouse.io/v1/boards/company/jobs?content=true",
             {"jobs": [{"id": 123, "title": "Python Engineer", "absolute_url": "/jobs/123", "content": "3年Python",
                        "location": {"name": "上海"}}]}),
            ("https://jobs.eu.lever.co/company?location=Paris", "lever", "https://api.eu.lever.co/v0/postings/company?location=Paris&mode=json",
             [{"id": "123", "text": "Python Engineer", "hostedUrl": "https://jobs.eu.lever.co/company/123",
               "descriptionPlain": "3年Python", "categories": {"allLocations": ["Paris"]}}]),
            ("https://jobs.ashbyhq.com/company", "ashby", "https://api.ashbyhq.com/posting-api/job-board/company",
             {"jobs": [{"title": "Python Engineer", "jobUrl": "https://jobs.ashbyhq.com/company/123",
                        "descriptionPlain": "3年Python", "isListed": True}]}),
        ]
        for url, provider, endpoint, document in examples:
            with self.subTest(provider=provider):
                result = resolve_template(url)
                self.assertEqual(result["config"]["fetch_url"], endpoint)
                adapter = CustomSource(result["config"])
                page = adapter.parse_document(json.dumps(document))
                self.assertEqual(len(page.jobs), 1)
                self.assertIn("Python", page.jobs[0]["technologies"])
                self.assertEqual(adapter.snapshot_metadata["template_id"], provider)

    def test_exact_domain_boundary_no_substring_spoof(self):
        for url in ("https://zhipin.com.evil.com/jobs", "https://evilzhipin.com/jobs",
                    "https://jobs.lever.co.evil.com/company"):
            self.assertEqual(resolve_template(url)["template"]["id"], "auto")
        self.assertEqual(resolve_template("https://www.zhipin.com/web/geek/jobs")["template"]["id"], "boss")

    def test_deep_links_and_unsupported_filters_never_expand_to_whole_board(self):
        for url in ("https://jobs.lever.co/company/job-id", "https://jobs.lever.co/company?skip=20",
                    "https://jobs.ashbyhq.com/company?department=eng", "https://job-boards.greenhouse.io/company/jobs/123"):
            with self.subTest(url=url):
                self.assertNotIn("fetch_url", resolve_template(url)["config"])
                self.assertEqual(resolve_template(url)["config"]["url"], url)

    def test_known_builtin_is_only_a_recommendation_not_a_silent_scope_change(self):
        result = resolve_template("https://careers.tencent.com/jobdesc.html?postId=123")
        self.assertEqual(result["template"]["source"], "tencent")
        self.assertNotIn("fetch_url", result["config"])
        self.assertTrue(result["warnings"])

    def test_learned_rule_reuse_does_not_reuse_old_query_pagination_or_other_tenant(self):
        saved = source(parser="json", items="jobs")
        result = resolve_template("https://example.com/jobs?city=Shanghai", [saved])
        self.assertEqual(result["template"]["kind"], "learned")
        self.assertTrue(result["config"]["url"].endswith("?city=Shanghai"))
        self.assertNotEqual(result["config"]["id"], saved["id"])
        self.assertEqual(resolve_template("https://example.com/other/jobs", [saved])["template"]["id"], "auto")
        bound = resolve_template("https://jobs.lever.co/company")["config"]
        self.assertNotEqual(resolve_template("https://jobs.lever.co/other", [bound])["template"]["kind"], "learned")

    def test_unknown_site_has_stable_identity_and_challenge_marker_is_not_saved(self):
        a = resolve_template("https://example.com/jobs?_security_check=secret")["config"]
        self.assertEqual(a["id"], resolve_template("https://example.com/jobs")["config"]["id"])
        self.assertNotIn("secret", json.dumps(a))

    def test_recorded_live_examples_replay_the_same_template_contract(self):
        path = Path(__file__).resolve().parents[1] / "jobcrawler/sources/template-examples.json"
        for sample in json.loads(path.read_text())["examples"]:
            adapter = CustomSource(resolve_template(sample["url"])["config"])
            jobs = adapter.parse_document(json.dumps(sample["document"])).jobs
            self.assertEqual(len(jobs), 1)
            self.assertEqual(adapter.config["template_id"], sample["template_id"])
            self.assertTrue(jobs[0]["details_pending"])


class AutoParserTests(unittest.TestCase):
    def test_nested_json_aliases_are_learned_and_reusable(self):
        document = json.dumps({"data": {"positions": [{"positionId": 1, "positionName": "Python工程师",
            "detailUrl": "/position/1", "company": {"name": "公司"}, "cityName": "上海", "jobRequirements": "3年以上Python经验"}]}})
        adapter = CustomSource(source())
        job = adapter.parse_document(document).jobs[0]
        self.assertEqual(job["company"], "公司")
        self.assertEqual(job["locations"], ["上海"])
        self.assertEqual(job["experience"]["min"], 3)
        self.assertEqual(adapter.config["items"], "data.positions")
        self.assertEqual(adapter.adaptation["status"], "adapted")
        second = CustomSource(adapter.config)
        self.assertEqual(second.parse_document(document).jobs[0]["id"], "1")
        self.assertIsNone(second.adaptation)

    def test_broken_template_falls_back_to_schema_without_changing_required_fields(self):
        adapter = CustomSource(source(parser="dom", items=".old", fields={"title": ".old-title", "url": "a"}))
        doc = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "工程师", "url": "/job/1"}) + '</script>'
        self.assertEqual(adapter.parse_document(doc).jobs[0]["title"], "工程师")
        self.assertEqual(adapter.config["parser"], "schema")
        self.assertNotIn("items", adapter.config)
        strict = CustomSource(source(parser="dom", items=".old", required_fields=["description"]))
        with self.assertRaises(ValueError):
            strict.parse_document(doc)

    def test_semantic_dom_layout_is_inferred(self):
        doc = '<article class="job-card"><h3>Python研发</h3><a href="/jobs/1">查看职位</a>' \
              '<b class="company">公司</b><span class="location">上海</span><p class="requirements">3年Python</p></article>'
        adapter = CustomSource(source(parser="dom", items=".obsolete", fields={"title": ".gone", "url": ".gone"}))
        self.assertEqual(adapter.parse_document(doc).jobs[0]["locations"], ["上海"])
        self.assertEqual(adapter.config["items"], ".job-card")

    def test_embedded_json_is_parsed_not_executed_and_survives_script_index_change(self):
        payload = json.dumps({"props": {"jobs": [{"title": "工程师", "url": "/job/1"}]}})
        doc = '<script>alert("never execute")</script><script type="application/json">' + payload + '</script>'
        adapter = CustomSource(source())
        self.assertEqual(len(adapter.parse_document(doc).jobs), 1)
        self.assertEqual(adapter.config["json_script"], 1)
        moved = '<script type="application/json">' + payload + '</script>'
        self.assertEqual(len(adapter.parse_document(moved).jobs), 1)
        self.assertEqual(adapter.config["json_script"], 0)

    def test_authentication_errors_empty_unknown_and_non_job_docs_do_not_adapt(self):
        for doc in ('{"success":false,"jobs":[{"title":"fake","url":"/job/1"}]}',
                    '{"error":"login required"}', '<h1>验证码</h1>', '{"data":[]}',
                    '{"articles":[{"title":"News","url":"/news/1"}]}'):
            with self.subTest(doc=doc), self.assertRaises(ValueError):
                CustomSource(source()).parse_document(doc)

    def test_ambiguous_job_arrays_are_rejected(self):
        doc = json.dumps({"jobs": [{"jobTitle": "A", "jobUrl": "/job/a"}],
                          "positions": [{"title": "B", "url": "/job/b"}]})
        with self.assertRaisesRegex(ValueError, "多个"):
            CustomSource(source()).parse_document(doc)

    def test_failed_adaptation_leaves_original_rule_intact(self):
        adapter = CustomSource(source(parser="dom", items=".old"))
        original, revision = dict(adapter.config), adapter.revision
        with self.assertRaises(ValueError):
            adapter.parse_document('<div class="job-card"><h3>Missing URL</h3></div>')
        self.assertEqual(adapter.config, original)
        self.assertEqual(adapter.revision, revision)

    def test_no_rows_are_silently_dropped_and_required_fields_not_relaxed(self):
        document = json.dumps({"data": {"jobs": [{"title": "Valid", "url": "/job/1"}, {"title": "No URL"}]}})
        with self.assertRaises(ValueError):
            CustomSource(source()).parse_document(document)

    def test_adaptation_cannot_remove_pagination_boundary(self):
        adapter = CustomSource(source(parser="json", items="obsolete", pagination={
            "url": "https://example.com/jobs?page={page}", "total_path": "total", "page_size": 20}))
        with self.assertRaisesRegex(ValueError, "自动适配"):
            adapter.parse_document('{"jobs":[{"title":"Engineer","url":"/job/1"}],"total":21}')

    def test_ashby_unlisted_postings_are_not_published(self):
        config = resolve_template("https://jobs.ashbyhq.com/company")["config"]
        document = {"jobs": [{"title": "Listed", "jobUrl": "/job/1", "isListed": True},
                             {"title": "Unlisted", "jobUrl": "/job/2", "isListed": False}]}
        self.assertEqual(len(CustomSource(config).parse_document(json.dumps(document)).jobs), 1)

    def test_multilocations_and_lever_description_sections_are_preserved(self):
        adapter = CustomSource(resolve_template("https://jobs.lever.co/company")["config"])
        document = [{"id": "1", "text": "Engineer", "hostedUrl": "/job/1", "categories": {
            "location": "上海", "allLocations": ["上海", "北京"]}, "lists": [
                {"text": "Responsibilities", "content": "<p>Python开发</p>"},
                {"text": "Qualifications", "content": "<p>3年以上工作经验</p>"}]}]
        job = adapter.parse_document(json.dumps(document)).jobs[0]
        self.assertEqual(job["locations"], ["上海", "北京"])
        self.assertIn("Python开发", job["description"])
        self.assertIn("3年以上", job["description"])
        self.assertFalse(job["details_pending"])

    def test_embedded_authentication_error_is_not_adapted(self):
        document = '<script type="application/json">{"success":false,"jobs":[{"jobTitle":"Fake","jobUrl":"/job/1"}]}</script>'
        with self.assertRaises(ValueError):
            CustomSource(source()).parse_document(document)

    def test_moved_array_retains_working_ats_multi_path_rules(self):
        config = resolve_template('https://jobs.lever.co/company')['config']
        config['items'] = 'obsolete.jobs'
        adapter = CustomSource(config)
        payload = [{'text': 'Engineer', 'hostedUrl': 'https://jobs.lever.co/company/1',
                    'categories': {'location': '上海', 'allLocations': ['上海', '北京']},
                    'lists': [{'text': 'Qualifications', 'content': '<p>Python experience</p>'}]}]
        job = adapter.parse_document(json.dumps(payload)).jobs[0]
        self.assertEqual(adapter.config['items'], '')
        self.assertEqual(adapter.config['fields'], config['fields'])
        self.assertEqual(job['locations'], ['上海', '北京'])
        self.assertIn('Python experience', job['description'])

    def test_text_and_url_menu_is_not_inferred_as_jobs(self):
        with self.assertRaises(ValueError):
            CustomSource(source()).parse_document('[{"text":"Home","url":"/home"}]')

    def test_auto_adaptation_can_be_disabled_and_new_config_is_validated(self):
        with self.assertRaises(ValueError):
            CustomSource(source(auto_adapt=False)).parse_document('{"jobs":[{"jobTitle":"Engineer","jobUrl":"/job/1"}]}')
        for bad in ({"auto_adapt": "yes"}, {"fetch_url": "http://127.0.0.1/"}, {"json_script": True}, {"template_id": []}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_config(source(**bad))
