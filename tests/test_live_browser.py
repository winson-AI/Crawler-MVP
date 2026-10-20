import json
from concurrent.futures import Future
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobcrawler.sources.custom import CustomSource, validate_config
from jobcrawler.sources.live_browser import CONNECT_TIMEOUT_MS, READ_TIMEOUT_SECONDS, ChromeReader, chrome_endpoint, recruitment_url, response_text
from jobcrawler.sources.templates import resolve_template


class LiveBrowserTests(unittest.TestCase):
    def test_anonymous_cleanup_tolerates_an_already_disconnected_driver(self):
        reader = ChromeReader(anonymous=True)
        reader.playwright, reader.browser = MagicMock(), MagicMock()
        client, browser = reader.playwright, reader.browser
        browser.is_connected.side_effect = [True, False]
        browser.close.side_effect = RuntimeError('driver disconnected')
        reader.disconnect()
        client.stop.assert_called_once()
        self.assertIsNone(reader.browser)
        reader.executor.shutdown()

    def test_public_reader_never_connects_user_chrome_or_exports_credentials(self):
        reader = ChromeReader(anonymous=True)
        client = MagicMock()
        browser = client.chromium.launch.return_value
        browser.contexts = [MagicMock()]
        with patch('playwright.sync_api.sync_playwright') as start, \
                patch('jobcrawler.sources.live_browser.chrome_endpoint') as endpoint:
            start.return_value.start.return_value = client
            reader.connect()
        client.chromium.launch.assert_called_once_with(channel='chrome', headless=True)
        client.chromium.connect_over_cdp.assert_not_called()
        endpoint.assert_not_called()
        browser.new_context.assert_called_once_with(service_workers='block')
        reader.disconnect()
        browser.close.assert_called_once()
        reader.executor.shutdown()

    def test_public_sdk_jsonp_is_decoded_as_data_not_executed(self):
        raw = b'jQuery123_456({"jobs":[{"title":"\\u5de5\\u7a0b\\u5e08"}]});'
        self.assertEqual(json.loads(response_text(raw))['jobs'][0]['title'], '工程师')
        malicious = b'callback({"title":"one"});evil();'
        self.assertEqual(response_text(malicious), malicious.decode())

    def test_campus_templates_are_tenant_scoped_and_stable_across_tracking_ids(self):
        config = resolve_template('https://campus.lagou.com/')['config']
        self.assertEqual(config['mode'], 'http')
        self.assertEqual(config['template_id'], 'lagou_campus')
        ids = []
        for tracking in ('first', 'second'):
            adapter = CustomSource(config)
            doc = '<li class="job-item"><a class="job-link" href="https://www.lagou.com/wn/jobs/123456.html?source=campus_jobs&amp;show=' + tracking + '"><span class="job-name">工程师</span><p class="experience"><span>上海</span><span>本科</span></p></a><a class="company-link"><h4 class="title">公司</h4></a></li>'
            job = adapter.parse_document(doc).jobs[0]
            ids.append(job['id'])
            self.assertEqual(job['url'], 'https://www.lagou.com/wn/jobs/123456.html')
            self.assertEqual(job['company'], '公司')
            self.assertEqual(job['locations'], ['上海'])
        self.assertEqual(ids[0], ids[1])
        campus = resolve_template('https://campus.51job.com/asiainfo/jobs.html')['config']
        self.assertEqual(campus['mode'], 'public_browser')
        self.assertEqual(campus['company'], '亚信')
        other = resolve_template('https://campus.51job.com/another/jobs.html')['config']
        self.assertNotIn('items', other)
        self.assertEqual(other['mode'], 'http')

    def test_public_browser_metadata_and_restrictions(self):
        adapter = CustomSource(self.config(mode='public_browser'))
        evidence = {'_network_texts': ['工程师'], 'anonymous': True, 'session_reused': False}
        document = '{"jobs":[{"title":"工程师","url":"/job/1"}]}'
        with patch('jobcrawler.sources.live_browser.fetch_browser_document',
                   return_value=(document, adapter.url, evidence)) as fetch:
            self.assertEqual(len(adapter.fetch_page(1).jobs), 1)
            fetch.assert_called_once_with(adapter.url, anonymous=True)
        self.assertEqual(adapter.snapshot_metadata['acquisition'], 'public_browser_network')
        with self.assertRaises(ValueError):
            validate_config(self.config(mode='public_browser', url='https://example.com/'))

    def test_human_authorization_has_a_coherent_wait_budget(self):
        reader = ChromeReader()
        client = MagicMock()
        client.chromium.connect_over_cdp.return_value.contexts = [MagicMock()]
        with patch('playwright.sync_api.sync_playwright') as start, \
                patch('jobcrawler.sources.live_browser.chrome_endpoint', return_value='ws://127.0.0.1:9222/devtools/browser/test'):
            start.return_value.start.return_value = client
            reader.connect()
        self.assertEqual(client.chromium.connect_over_cdp.call_args.kwargs['timeout'], CONNECT_TIMEOUT_MS)
        self.assertGreater(CONNECT_TIMEOUT_MS, 60000)
        self.assertGreater(READ_TIMEOUT_SECONDS, CONNECT_TIMEOUT_MS / 1000 + 75)
        reader.disconnect()
        reader.executor.shutdown()

    def test_pending_read_does_not_queue_more_connection_prompts(self):
        reader = ChromeReader()
        reader.pending = Future()
        with patch.object(reader.executor, 'submit') as submit:
            with self.assertRaisesRegex(ValueError, '未重复发起授权'):
                reader.read('https://www.zhipin.com/')
            submit.assert_not_called()
        reader.executor.shutdown()

    def test_late_analytics_cannot_evict_fresh_document_evidence(self):
        reader = ChromeReader()
        context, page, cdp = MagicMock(), MagicMock(), MagicMock()
        reader.browser = MagicMock(contexts=[context])
        context.new_page.return_value = page
        context.new_cdp_session.return_value = cdp
        page.url = 'https://www.51job.com/'
        page.content.return_value = '<a href="/job_detail/1">工程师</a>'
        page.locator.return_value.inner_text.return_value = '工程师'
        page.locator.return_value.evaluate_all.return_value = True
        def navigate(*args, **kwargs):
            observed = cdp.on.call_args.args[1]
            observed({'type': 'Document', 'requestId': 'doc', 'response': {
                'url': page.url, 'status': 200}})
            for number in range(20):
                observed({'type': 'XHR', 'requestId': str(number), 'response': {
                    'url': page.url + 'analytics', 'status': 200}})
            observed({'type': 'Document', 'requestId': 'iframe', 'response': {
                'url': page.url + 'widget', 'status': 200}})
        page.goto.side_effect = navigate
        cdp.send.side_effect = lambda method, *args: {'body': '工程师'} if method == 'Network.getResponseBody' else {}
        with patch.object(reader, 'connect'):
            _, _, evidence = reader._read(page.url)
        self.assertEqual(evidence['_network_texts'], ['工程师'])
        self.assertEqual(evidence['responses'][0]['resource_type'], 'Document')
        self.assertEqual(evidence['responses'][0]['url'], page.url)
        self.assertEqual(len(evidence['responses']), 1)
        cdp.detach.assert_called_once()
        reader.browser.close.assert_not_called()
        reader.executor.shutdown()

    def config(self, **updates):
        return {'id': 'custom_live_test', 'name': '测试', 'url': 'https://www.zhipin.com/',
                'mode': 'browser', 'parser': 'json', 'items': 'jobs', **updates}

    def test_browser_opt_in_is_restricted_to_portals_and_current_page(self):
        self.assertEqual(validate_config(self.config())['mode'], 'browser')
        for url in ('https://zhipin.com.evil.com/', 'https://example.com/', 'http://127.0.0.1/'):
            with self.assertRaises(ValueError):
                recruitment_url(url)
        for updates in ({'fetch_url': 'https://www.zhipin.com/api'},
                        {'pagination': {'url': 'https://www.zhipin.com/?page={page}', 'page_size': 10}}):
            with self.assertRaises(ValueError):
                validate_config(self.config(**updates))

    def test_endpoint_only_uses_loopback_from_validated_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'port'
            for content in ('9222\n/devtools/browser/abc-123', 'http://evil\n/devtools/browser/abc',
                            '99999\n/devtools/browser/abc', '9222\n/other', '9222\n/devtools/browser/a?token=b'):
                path.write_text(content)
                if content.startswith('9222\n/devtools/browser/abc-123'):
                    self.assertEqual(chrome_endpoint(path), 'ws://127.0.0.1:9222/devtools/browser/abc-123')
                else:
                    with self.assertRaises(ValueError):
                        chrome_endpoint(path)

    def test_browser_jobs_must_correlate_with_new_response_and_raw_text_is_not_persisted(self):
        document = json.dumps({'jobs': [{'title': '工程师', 'url': '/job/1'}]})
        for text, success in [('本轮工程师', True), ('旧 DOM 未出现在网络', False)]:
            adapter = CustomSource(self.config())
            evidence = {'_network_texts': [text], 'requested_at': 'now'}
            with patch('jobcrawler.sources.live_browser.fetch_browser_document',
                       return_value=(document, adapter.url, evidence)):
                if success:
                    self.assertEqual(len(adapter.fetch_page(1).jobs), 1)
                    self.assertEqual(adapter.snapshot_metadata['acquisition'], 'browser_network')
                    self.assertEqual(evidence['jobs_correlated_with_network'], 1)
                else:
                    with self.assertRaisesRegex(ValueError, '新网络响应'):
                        adapter.fetch_page(1)
            self.assertNotIn('_network_texts', json.dumps(adapter.snapshot_metadata))

    def test_observed_browser_json_recovers_click_only_dom_without_fetching_an_api(self):
        payload = json.dumps({'result': {'jobList': [{'jobId': 1, 'jobName': 'Python工程师',
            'jobInfoUrlPc': 'https://jobs.51job.com/shanghai/1.html', 'companyName': '公司',
            'jobAreaString': '上海', 'workYearString': '3-5年'}]}}, ensure_ascii=False)
        adapter = CustomSource(self.config(url='https://we.51job.com/pc/search', parser='dom', items='.obsolete'))
        evidence = {'_network_texts': [payload], 'responses': [{'url': 'https://we.51job.com/observed-job-response'}]}
        with patch('jobcrawler.sources.live_browser.fetch_browser_document',
                   return_value=('<div>click-only cards</div>', adapter.url, evidence)):
            job = adapter.fetch_page(1).jobs[0]
        self.assertEqual(job['title'], 'Python工程师')
        self.assertEqual(job['company'], '公司')
        self.assertEqual(job['locations'], ['上海'])
        self.assertEqual(adapter.config['items'], 'result.jobList')
        self.assertEqual(evidence['parser_input'], 'observed_network_json')
        self.assertNotIn('_network_texts', evidence)
        self.assertIsNotNone(adapter.adaptation)

    def test_browser_response_fallback_keeps_strict_fields_and_rejects_ambiguity(self):
        for updates, texts in [({'required_fields': ['description']}, ['{"jobs":[{"title":"工程师","url":"/job/1"}]}']),
                               ({'auto_adapt': False}, ['{"jobs":[{"title":"工程师","url":"/job/1"}]}']),
                               ({}, ['{"jobs":[{"title":"工程师","url":"/job/1"}]}',
                                     '{"jobs":[{"title":"工程师","url":"/job/2"}]}'])]:
            adapter = CustomSource(self.config(parser='dom', items='.obsolete', **updates))
            evidence = {'_network_texts': texts}
            with patch('jobcrawler.sources.live_browser.fetch_browser_document',
                       return_value=('<div>no cards</div>', adapter.url, evidence)):
                with self.assertRaises(ValueError):
                    adapter.fetch_page(1)
            self.assertNotIn('_network_texts', evidence)

    def test_lagou_live_dom_mapping_skips_logo_links_and_does_not_invent_city(self):
        config = resolve_template('https://www.lagou.com/wn/')['config']
        self.assertEqual(config['mode'], 'http')
        config.update(items='.obsolete')
        adapter = CustomSource(config)
        doc = '<div class="positionItem__newhash"><a href="/wn/jobs/1.html" title="工程师">工程师 3-5年 / 本科</a><a href="/gongsi/1"><img></a><a href="/gongsi/1">公司</a></div>'
        job = adapter.parse_document(doc).jobs[0]
        self.assertEqual(job['company'], '公司')
        self.assertEqual(job['locations'], [])
        self.assertEqual(job['experience']['min'], 3)
        self.assertEqual(adapter.config['items'], '[class*="positionItem__"]')
        self.assertNotIn('items', resolve_template('https://www.lagou.com/wn/zhaopin/?city=Shanghai')['config'])

    def test_disconnect_preserves_user_browser_and_context(self):
        reader = ChromeReader()
        reader.playwright, reader.browser = MagicMock(), MagicMock()
        client, browser = reader.playwright, reader.browser
        reader.disconnect()
        client.stop.assert_called_once()
        browser.close.assert_not_called()
        reader.executor.shutdown()

    def test_public_home_mapping_recovers_broken_rules_without_inventing_location(self):
        url = 'https://www.zhaopin.com/'
        config = resolve_template(url)['config']
        config['items'] = '.obsolete'
        adapter = CustomSource(config)
        doc = '<div class="featured-jobs-card"><a class="featured-jobs-card__title" href="/jobdetail/1.htm">工程师</a><a class="featured-jobs-card__company">公司</a></div>'
        job = adapter.parse_document(doc).jobs[0]
        self.assertEqual(job['title'], '工程师')
        self.assertEqual(job['locations'], [])
        self.assertTrue(job['details_pending'])
        self.assertIsNotNone(adapter.adaptation)
        deep = resolve_template('https://www.zhaopin.com/jobs?city=Shanghai')['config']
        self.assertNotIn('items', deep)


if __name__ == '__main__':
    unittest.main()
