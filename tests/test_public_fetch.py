import ipaddress
import json
import socket
import ssl
import unittest
from unittest.mock import MagicMock, patch

from jobcrawler.sources.custom import CustomSource, _doh_addresses, _public_socket, fetch_document


def entry(ip):
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))


def response(payload, status=200):
    value = MagicMock()
    value.status = status
    value.read.return_value = json.dumps(payload).encode()
    value.headers.get_content_charset.return_value = None
    value.getheader.return_value = "Tue, 20 Oct 2026 04:00:00 GMT"
    return value


class PublicFetchTests(unittest.TestCase):
    def test_fake_ip_fallback_pins_validated_numeric_public_address(self):
        audit = {}
        with patch('jobcrawler.sources.custom.socket.getaddrinfo', return_value=[entry('198.18.1.1'), entry('198.19.1.2')]) as dns, \
             patch('jobcrawler.sources.custom._doh_addresses', return_value=[ipaddress.ip_address('8.8.8.8')]) as doh, \
             patch('jobcrawler.sources.custom.socket.socket') as sock:
            _public_socket(('example.com', 443), 20, audit=audit)
            dns.assert_called_once_with('example.com', 443, type=socket.SOCK_STREAM)
            doh.assert_called_once_with('example.com', 20)
            sock.return_value.connect.assert_called_once_with(('8.8.8.8', 443))
        self.assertEqual(audit['dns_mode'], 'cloudflare_doh')
        self.assertEqual(audit['connected_ip'], '8.8.8.8')

    def test_mixed_fake_private_and_public_answers_do_not_trigger_fallback(self):
        for addresses in (['198.18.1.1', '8.8.8.8'], ['198.18.1.1', '127.0.0.1'], ['10.0.0.1'], ['169.254.169.254']):
            with self.subTest(addresses=addresses), \
                 patch('jobcrawler.sources.custom.socket.getaddrinfo', return_value=[entry(ip) for ip in addresses]), \
                 patch('jobcrawler.sources.custom._doh_addresses') as doh, \
                 patch('jobcrawler.sources.custom.socket.socket') as sock:
                with self.assertRaises(ValueError):
                    _public_socket(('example.com', 443), 20)
                doh.assert_not_called()
                sock.assert_not_called()

    def test_public_system_dns_does_not_send_query_to_external_resolver(self):
        audit = {}
        with patch('jobcrawler.sources.custom.socket.getaddrinfo', return_value=[entry('8.8.8.8')]), \
             patch('jobcrawler.sources.custom._doh_addresses') as doh, \
             patch('jobcrawler.sources.custom.socket.socket'):
            _public_socket(('example.com', 443), 20, audit=audit)
        doh.assert_not_called()
        self.assertEqual(audit['dns_mode'], 'system')

    def test_doh_uses_fixed_https_ip_and_normal_tls_without_proxy_or_redirect(self):
        reply = response({'Status': 0, 'Answer': [
            {'type': 5, 'data': 'cdn.example.com'}, {'type': 1, 'data': '8.8.8.8'}, {'type': 1, 'data': '8.8.8.8'}]})
        with patch('jobcrawler.sources.custom.http.client.HTTPSConnection') as cls:
            cls.return_value.getresponse.return_value = reply
            self.assertEqual(_doh_addresses('example.com', 20), [ipaddress.ip_address('8.8.8.8')])
            cls.assert_called_once_with('1.1.1.1', timeout=10)
            cls.return_value.request.assert_called_once_with('GET', '/dns-query?name=example.com&type=A', headers={'Accept': 'application/dns-json'})
            cls.return_value.close.assert_called_once()

    def test_doh_refuses_non_public_malformed_and_error_responses(self):
        bad = [({'Status': 3}, 200), ({'Status': False}, 200), ([], 200),
               ({'Status': 0, 'Answer': []}, 200), ({'Status': 0, 'Answer': 'bad'}, 200),
               ({'Status': 0, 'Answer': [None]}, 200), ({'Status': 0, 'Answer': [{'type': 1, 'data': 'invalid'}]}, 200),
               ({'Status': 0, 'Answer': [{'type': 1, 'data': '127.0.0.1'}]}, 200),
               ({'Status': 0, 'Answer': [{'type': 28, 'data': '::1'}]}, 200),
               ({'Status': 0, 'Answer': [{'type': 1, 'data': '198.18.1.1'}]}, 200),
               ({'Status': 0, 'Answer': [{'type': 1, 'data': '8.8.8.8'}, {'type': 1, 'data': '10.0.0.1'}]}, 200),
               ({'Status': 0}, 302), ({'Status': 0}, 429)]
        for payload, status in bad:
            with self.subTest(payload=payload, status=status), patch('jobcrawler.sources.custom.http.client.HTTPSConnection') as cls:
                cls.return_value.getresponse.return_value = response(payload, status)
                with self.assertRaises(ValueError):
                    _doh_addresses('example.com', 20)
                cls.return_value.request.assert_called_once()
                cls.return_value.close.assert_called_once()

    def test_doh_bounded_response_and_certificate_failure_fail_closed(self):
        with patch('jobcrawler.sources.custom.http.client.HTTPSConnection') as cls:
            reply = response({})
            reply.read.return_value = b'x' * 65537
            cls.return_value.getresponse.return_value = reply
            with self.assertRaises(ValueError):
                _doh_addresses('example.com', 20)
            reply.read.assert_called_once_with(65537)
        with patch('jobcrawler.sources.custom.http.client.HTTPSConnection') as cls:
            cls.return_value.request.side_effect = ssl.SSLCertVerificationError('certificate invalid')
            with self.assertRaises(ssl.SSLCertVerificationError):
                _doh_addresses('example.com', 20)
            cls.return_value.close.assert_called_once()

    def test_fetch_records_fresh_response_hash_and_does_not_send_conditional_cache_headers(self):
        audit = []
        with patch('jobcrawler.sources.custom.http.client.HTTPSConnection') as cls:
            cls.return_value.getresponse.return_value = response({'jobs': []})
            document, url = fetch_document('https://example.com/jobs', audit=audit.append)
            headers = cls.return_value.request.call_args.kwargs['headers']
            self.assertEqual(headers['Cache-Control'], 'no-cache')
            self.assertNotIn('If-None-Match', headers)
            self.assertNotIn('If-Modified-Since', headers)
        self.assertEqual(json.loads(document), {'jobs': []})
        self.assertEqual(audit[0]['http_status'], 200)
        self.assertEqual(len(audit[0]['response_sha256']), 64)
        self.assertTrue(audit[0]['requested_at'])

    def test_304_is_not_treated_as_live_jobs_or_read_from_cache(self):
        with patch('jobcrawler.sources.custom.http.client.HTTPSConnection') as cls:
            cls.return_value.getresponse.return_value = response({}, 304)
            with self.assertRaisesRegex(ValueError, '304'):
                fetch_document('https://example.com/jobs')

    def test_refresh_network_audit_is_reset_each_round_not_reused(self):
        adapter = CustomSource({'id': 'custom_audit', 'name': 'Audit', 'url': 'https://example.com/jobs', 'parser': 'json', 'items': 'jobs'})
        def fetch(url, audit):
            audit({'url': url, 'http_status': 200})
            return '{"jobs":[{"title":"Engineer","url":"/job/1"}]}', url
        with patch('jobcrawler.sources.custom.fetch_document', side_effect=fetch) as request:
            adapter.fetch_page(1)
            adapter.fetch_page(1)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(len(adapter.network_requests), 1)
        self.assertEqual(adapter.snapshot_metadata['acquisition'], 'network')
