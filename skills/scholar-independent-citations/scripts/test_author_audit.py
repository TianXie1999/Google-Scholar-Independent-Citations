"""Fictional fixtures only; no user archive, credentials or live requests."""
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_resolver import HTTPStore, classify_pair
import report


class FullAuditTests(unittest.TestCase):
    def fixture(self):
        item = {'result_id': 'CITING', 'title': 'Citing work', 'link': 'https://example.org/citing',
                'publication_info': {'summary': 'F Reader - Journal, 2000', 'authors': [{'name': 'F Reader'}]}}
        return {'author_id': 'OWNER', 'author': {'name': 'Fran Reader'}, 'snapshot_date': '2000-01-07',
                'papers': [{'paper': {'title': 'Source work', 'citation_id': 'OWNER:SOURCE', 'link': 'https://example.org/source', 'year': '2020'},
                            'detail': {'authors': 'Fran Reader', 'link': 'https://example.org/source'},
                            'displayed': 1, 'end_reason': 'last_page', 'citing': [item]}]}

    def metadata(self, url):
        title, name = ('Citing work', 'Feng Reader') if url.endswith('citing') else ('Source work', 'Fran Reader')
        return '<meta name="citation_title" content="' + title + '"><meta name="citation_author" content="' + name + '">'

    def test_checks_untruncated_previously_excluded_byline_and_preserves_output_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'profile_OWNER.json').write_text('{}')
            with patch('audit_resolver.metadata_get', side_effect=self.metadata) as get:
                result = report.generate(self.fixture(), root, root / 'output', full_author_audit=True)
            self.assertEqual(result['classification'], {'independent': 1, 'non_independent': 0, 'unresolved': 0})
            self.assertEqual(get.call_count, 2)  # Citing and source bylines.
            self.assertEqual(result['citation_edges'][0]['citing_authors'], 'Feng Reader')
            self.assertTrue(result['citation_edges'][0]['source_author_metadata_complete'])
            self.assertEqual(sorted(p.name for p in (root / 'output').iterdir()), ['independent_citations.csv', 'report.html'])
            with (root / 'output/independent_citations.csv').open(encoding='utf-8-sig') as handle:
                self.assertEqual(len(csv.DictReader(handle).fieldnames), 13)

    def test_short_full_names_are_distinct_and_initials_are_unresolved(self):
        self.assertEqual(classify_pair(['Ce Example'], [{'name': 'Caren Example'}], {})[0], 'independent')
        self.assertEqual(classify_pair(['Ce Example'], [{'name': 'C Example'}], {})[0], 'unresolved')

    def test_source_byline_can_establish_a_middle_initial(self):
        data = self.fixture()
        data['author'] = {'name': 'Unrelated Owner'}
        data['papers'][0]['detail']['authors'] = 'Jane Reader'
        data['papers'][0]['citing'][0]['publication_info'] = {'summary': 'J W Reader - Journal, 2000', 'authors': [{'name': 'J W Reader'}]}
        def get(url):
            title = 'Citing work' if url.endswith('citing') else 'Source work'
            return '<meta name="citation_title" content="' + title + '"><meta name="citation_author" content="Jane W. Reader">'
        with tempfile.TemporaryDirectory() as directory, patch('audit_resolver.metadata_get', side_effect=get):
            root = Path(directory); (root / 'profile_OWNER.json').write_text('{}')
            result = report.generate(data, root, root / 'output', full_author_audit=True)
        self.assertEqual(result['classification']['non_independent'], 1)
        self.assertEqual(result['citation_edges'][0]['source_authors'], 'Jane W. Reader')

    def test_offline_store_never_requests_live_metadata_and_checks_hashes(self):
        with tempfile.TemporaryDirectory() as directory, patch('audit_resolver.metadata_get', side_effect=AssertionError('Network forbidden')):
            root = Path(directory); seed = root / 'seed'; seed.mkdir()
            url = 'https://example.org/paper'; key = hashlib.sha256(url.encode()).hexdigest()
            (seed / (key + '.json')).write_text(json.dumps({'ok': True, 'body_sha256': hashlib.sha256(b'body').hexdigest()}))
            (seed / (key + '.body')).write_bytes(b'body')
            self.assertEqual(HTTPStore(root / 'output', seed, offline=True).get(url), 'body')
            (seed / (key + '.body')).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                HTTPStore(root / 'other', seed, offline=True).get(url)
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                HTTPStore(root / 'missing', offline=True).get('https://example.org/missing')

    def test_metadata_fetch_reuse_preserves_source_and_version_relationships(self):
        data = self.fixture(); item = data['papers'][0]['citing'][0]
        data['papers'][0]['citing'].append(dict(item, result_id='OTHER_VERSION'))
        data['papers'][0]['displayed'] = 2
        data['papers'].append(dict(data['papers'][0], paper=dict(data['papers'][0]['paper'], citation_id='OWNER:SECOND')))
        with tempfile.TemporaryDirectory() as directory, patch('audit_resolver.metadata_get', side_effect=self.metadata):
            root = Path(directory); (root / 'profile_OWNER.json').write_text('{}')
            result = report.generate(data, root, root / 'output', full_author_audit=True)
        self.assertEqual(result['retrieved'], 4)
        self.assertEqual(len({(r['source_citation_id'], r['scholar_result_id']) for r in result['citation_edges']}), 4)

    def test_explicit_retry_reconsiders_negative_http_cache_but_offline_stays_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); seed = root / 'seed'; seed.mkdir()
            url = 'https://example.org/paper'; key = hashlib.sha256(url.encode()).hexdigest()
            (seed / (key + '.json')).write_text(json.dumps({'ok': False, 'status': 403}))
            with patch('audit_resolver.metadata_get', return_value='metadata') as fetch:
                self.assertEqual(HTTPStore(root / 'fresh', seed, retry_unavailable=True).get(url), 'metadata')
                self.assertEqual(fetch.call_count, 1)
            with patch('audit_resolver.metadata_get', side_effect=AssertionError('Offline')):
                with self.assertRaisesRegex(RuntimeError, 'offline'):
                    HTTPStore(root / 'offline', seed, retry_unavailable=True, offline=True).get(url)

    def test_resource_note_and_wrong_ieee_body_cannot_override_original_record(self):
        from audit_resolver import Resolver
        original = {'title': 'Same title', 'link': 'https://openreview.net/forum?id=ORIGINAL',
                    'publication_info': {'summary': 'A Reader'}}
        candidate = {'title': 'Same title', 'source': 'https://openreview.net/forum?id=OTHER',
                     'openreview_note_id': 'OTHER', 'authors': [{'name': 'Alice Reader'}]}
        self.assertIn('OpenReview note', Resolver.validate(original, candidate))
        original.update(result_id='IEEE', link='https://ieeexplore.ieee.org/document/111')
        class WrongPage:
            def get(self, url):
                return 'xplGlobal.document.metadata=' + json.dumps({'articleId': '222', 'authors': [{'name': 'Alice Reader'}]}) + ';<meta name="citation_title" content="Same title"><meta name="citation_author" content="Alice Reader">'
            def json(self, url): raise RuntimeError('No other source')
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(Resolver(directory, WrongPage()).resolve(original)['complete'])

    def test_explicit_publisher_pii_selects_original_byline_over_same_title_preprint(self):
        from audit_resolver import Resolver
        item = {'result_id': 'C', 'title': 'Identical work title',
                'link': 'https://www.sciencedirect.com/science/article/pii/S1234567890',
                'publication_info': {'summary': 'A Reader…'}}
        candidates = [{'DOI': '10.1234/preprint', 'title': [item['title']],
                       'author': [{'given': 'Adam', 'family': 'Reader'}]},
                      {'DOI': '10.1234/journal', 'title': [item['title']],
                       'author': [{'given': 'Alice', 'family': 'Reader'}],
                       'link': [{'URL': 'https://api.elsevier.com/content/article/PII:S1234567890?httpAccept=text/xml'}]}]
        class Records:
            def get(self, url): raise RuntimeError('Landing page unavailable')
            def json(self, url): return {'message': {'items': candidates}}
        with tempfile.TemporaryDirectory() as directory:
            resolved = Resolver(directory, Records()).resolve(item)
            self.assertTrue(resolved['complete'])
            self.assertEqual(resolved['doi'], '10.1234/journal')
            self.assertEqual(resolved['record_match'], 'explicit_pii_record')

    def test_provider_record_cache_aliases_are_bounded_and_hash_checked(self):
        from audit_resolver import cached_record_identity
        for old, new in [('https://arxiv.org/abs/2501.12345?', 'https://arxiv.org/abs/2501.12345'),
                         ('https://api.crossref.org/works/10.1234%2FABC', 'https://api.crossref.org/works/10.1234%2Fabc')]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); seed = root / 'seed'; seed.mkdir()
                key = hashlib.sha256(old.encode()).hexdigest()
                (seed / (key + '.json')).write_text(json.dumps({'url': old, 'ok': True, 'body_sha256': hashlib.sha256(b'body').hexdigest()}))
                (seed / (key + '.body')).write_bytes(b'body')
                with patch('audit_resolver.metadata_get', side_effect=AssertionError('Offline')):
                    self.assertEqual(HTTPStore(root / 'output', seed, offline=True).get(new), 'body')
        self.assertNotEqual(cached_record_identity('https://api.crossref.org/works/10.1234/%C3%84'),
                            cached_record_identity('https://api.crossref.org/works/10.1234/%C3%A4'))
        self.assertIsNone(cached_record_identity('https://example.org/paper?'))
        self.assertIsNone(cached_record_identity('https://arxiv.org/abs/2501.12345?context=other'))

    def test_checked_source_identity_requires_matching_primary_name_id_and_hash(self):
        from author_audit import known_ids
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'identity.html'
            path.write_text('<h1>Alice Reader</h1><a href="https://scholar.google.com/citations?user=PERSON">Scholar</a>')
            identity = {'name': 'Alice Reader', 'author_id': 'PERSON', 'verified': True,
                        'evidence_body_path': str(path), 'evidence_body_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assertEqual(known_ids(self.fixture(), {}, [identity])['alicereader'], 'PERSON')
            path.write_text('Unrelated page')
            with self.assertRaisesRegex(ValueError, 'hash-bound'):
                known_ids(self.fixture(), {}, [identity])

    def test_optional_openalex_key_is_not_retained_or_forwarded_to_other_hosts(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'OPENALEX_API_KEY': 'TEST_PRIVATE_CREDENTIAL'}):
            root = Path(directory)
            with patch('audit_resolver.metadata_get', side_effect=lambda url: 'body TEST_PRIVATE_CREDENTIAL' if 'api.openalex.org' in url else 'other body') as get:
                store = HTTPStore(root)
                self.assertEqual(store.get('https://api.openalex.org/works?search=Paper'), 'body REDACTED')
                self.assertIn('api_key=TEST_PRIVATE_CREDENTIAL', get.call_args[0][0])
                store.get('https://example.org/paper')
                self.assertNotIn('api_key=', get.call_args[0][0])
            for path in root.iterdir():
                if path.is_file():
                    self.assertNotIn('TEST_PRIVATE_CREDENTIAL', path.read_text())


if __name__ == '__main__':
    unittest.main()
