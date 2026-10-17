"""Fictional fixtures only; no user archive, credentials or live requests."""
import io
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import enrich_authors as metadata
import report


class Response:
    def __init__(self, body='<html>ok</html>'):
        self.body = body.encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, limit):
        return self.body[:limit]


class RecoveryTests(unittest.TestCase):
    def item(self, link='https://publisher.example/work'):
        return {'title': 'Citing work', 'link': link, 'publication_info': {
            'authors': [{'name': 'A Example'}], 'summary': 'A Example… - Journal, 2000'}}

    def test_transient_failure_recovers_with_bounded_retry(self):
        error = urllib.error.HTTPError('https://publisher.example', 429, 'limited', {'Retry-After': '1'}, None)
        with patch.object(metadata.urllib.request, 'urlopen', side_effect=[error, Response()]) as opened, patch.object(metadata.time, 'sleep') as sleep:
            self.assertIn('ok', metadata.get('https://publisher.example'))
        self.assertEqual(opened.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_timeout_attempts_are_bounded(self):
        with patch.object(metadata.urllib.request, 'urlopen', side_effect=TimeoutError) as opened, patch.object(metadata.time, 'sleep'):
            with self.assertRaises(TimeoutError):
                metadata.get('https://publisher.example')
        self.assertEqual(opened.call_count, 3)

    def test_permanent_failure_and_long_retry_after_are_not_retried(self):
        for status, headers in [(403, {}), (404, {}), (429, {'Retry-After': '60'})]:
            error = urllib.error.HTTPError('https://publisher.example', status, 'failed', headers, None)
            with patch.object(metadata.urllib.request, 'urlopen', side_effect=error) as opened, patch.object(metadata.time, 'sleep') as sleep:
                with self.assertRaises(urllib.error.HTTPError):
                    metadata.get('https://publisher.example')
            self.assertEqual(opened.call_count, 1)
            sleep.assert_not_called()

    def test_certificate_failure_and_browser_challenge_are_not_retried(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError('certificate'))
        with patch.object(metadata.urllib.request, 'urlopen', side_effect=error) as opened:
            with self.assertRaises(urllib.error.URLError):
                metadata.get('https://publisher.example')
        self.assertEqual(opened.call_count, 1)
        with patch.object(metadata.urllib.request, 'urlopen', return_value=Response('<title>Verifying your browser</title>')) as opened:
            with self.assertRaises(metadata.MetadataUnavailable):
                metadata.get('https://publisher.example')
        self.assertEqual(opened.call_count, 1)

    def test_only_explicit_retry_reconsiders_an_incomplete_cache(self):
        page = '<meta name="citation_title" content="Citing work"><meta name="citation_author" content="Alice Example">'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(metadata, 'get', side_effect=TimeoutError):
                self.assertFalse(metadata.enrich(self.item(), root)['complete'])
            with patch.object(metadata, 'get', return_value=page) as get:
                self.assertFalse(metadata.enrich(self.item(), root)['complete'])
                get.assert_not_called()
                self.assertTrue(metadata.enrich(self.item(), root, retry_unresolved=True)['complete'])
                get.assert_called_once()
            cache = next((root / 'metadata').iterdir())
            before = cache.read_bytes()
            with patch.object(metadata, 'get', side_effect=AssertionError('Successful cache must be reused')):
                self.assertTrue(metadata.enrich(self.item(), root, retry_unresolved=True)['complete'])
            self.assertEqual(cache.read_bytes(), before)

    def test_failure_diagnostics_retain_status_without_credentials(self):
        url = 'https://user:password@publisher.example/work?api_key=private&token=private2'
        error = urllib.error.HTTPError(url, 403, 'blocked', {}, None)
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=error):
            ev = metadata.enrich(self.item(url), Path(folder))
            stored = next((Path(folder) / 'metadata').iterdir()).read_text()
        self.assertTrue(any(a.get('status') == 403 for a in ev['attempts']))
        for secret in ['password', 'private', 'user:']:
            self.assertNotIn(secret, stored)
        self.assertIn('looked_up_at_utc', ev)

    def test_metadata_only_cli_uses_snapshot_without_collection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            item = self.item()
            item['result_id'] = 'CITING'
            data = {'author_id': 'SOURCE', 'author': {'name': 'Owner Person'}, 'snapshot_date': '2000-01-02',
                    'papers': [{'paper': {'title': 'Source', 'link': 'https://example.org/source'},
                                'detail': {'authors': 'Owner Person'}, 'displayed': 1, 'end_reason': 'last_page', 'citing': [item]}]}
            snapshot = root / 'collected.json'
            snapshot.write_text(json.dumps(data))
            (root / 'profile_SOURCE.json').write_text('{}')
            argv = ['report.py', '--snapshot', str(snapshot), '--output-dir', str(root / 'output'), '--retry-unresolved']
            supplement = {'complete': True, 'authors': ['Alice Example'], 'source': item['link']}
            with patch.object(sys, 'argv', argv), patch.object(report, 'collect', side_effect=AssertionError('No collection')), patch.object(report, 'enrich', return_value=supplement) as get, patch('sys.stdout', new_callable=io.StringIO):
                report.main()
            get.assert_called_once_with(item, root, retry_unresolved=True)
            self.assertEqual(snapshot.read_text(), json.dumps(data))

    def test_offline_retry_is_explicitly_rejected_without_network(self):
        with patch.object(sys, 'argv', ['report.py', '--output-dir', 'unused', '--offline', '--retry-unresolved']), patch('sys.stderr', new_callable=io.StringIO), patch.object(metadata, 'get', side_effect=AssertionError('No HTTP')):
            with self.assertRaises(SystemExit):
                report.main()


if __name__ == '__main__':
    unittest.main()
