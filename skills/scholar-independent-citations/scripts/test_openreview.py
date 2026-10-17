"""Fictional fixtures only; no user archive, credentials or live requests."""
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import enrich_authors as metadata


class OpenReviewTests(unittest.TestCase):
    def item(self, title='Original title'):
        url = 'https://openreview.net/pdf?id=ExactNote'
        return {'title': title, 'link': url, 'resources': [{'link': url, 'file_format': 'PDF'}],
                'publication_info': {'authors': [{'name': 'A Example', 'author_id': 'SCHOLAR_PERSON'}], 'summary': 'A Example…'}}

    def note(self, identifier='ExactNote', wrapped=True, authors=None):
        content = {'title': 'Updated submission title', 'authors': authors or ['Alice Example', 'Bob Reader'],
                   'authorids': ['~Alice_Example1', '~Bob_Reader1']}
        if wrapped:
            content = {key: {'value': value} for key, value in content.items()}
        return {'notes': [{'id': identifier, 'content': content}]}

    def test_search_recovers_exact_note_after_blocked_direct_lookup(self):
        called = []
        def get(url):
            called.append(url)
            if '/notes/search?' in url:
                return json.dumps(self.note())
            raise urllib.error.HTTPError(url, 403, 'challenge', {}, None)
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=get):
            ev = metadata.enrich(self.item(), Path(folder))
        self.assertTrue(ev['complete'])
        self.assertEqual(ev['openreview_note_id'], 'ExactNote')
        self.assertEqual(ev['openreview_author_ids'][0], '~Alice_Example1')
        self.assertNotIn('author_id', ev)
        self.assertEqual(len(called), 3)
        self.assertEqual(ev['authors'], ['Alice Example', 'Bob Reader'])

    def test_same_title_different_or_case_changed_note_is_never_substituted(self):
        for identifier in ['DifferentNote', 'exactnote']:
            payload = self.note(identifier)
            for entry in payload['notes']:
                entry['content']['title']['value'] = 'Original title'
            with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', return_value=json.dumps(payload)):
                ev = metadata.enrich(self.item(), Path(folder))
            self.assertFalse(ev['complete'])

    def test_legacy_content_and_shorter_query_are_supported(self):
        title = 'A long title ending Distinctive Query Suffix'
        def get(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            if url.startswith('https://api.openreview.net/notes/search?') and query.get('term') == ['Distinctive Query Suffix'] and query.get('limit') == ['100']:
                return json.dumps(self.note(wrapped=False))
            return json.dumps({'notes': []})
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=get):
            ev = metadata.enrich(self.item(title), Path(folder))
        self.assertTrue(ev['complete'])
        self.assertIn('Distinctive+Query+Suffix', ev['retrieval_url'])
        self.assertEqual(len(metadata.openreview_routes('ExactNote', title)), 10)

    def test_anonymous_note_is_not_complete_and_other_resource_id_is_ignored(self):
        item = self.item()
        item['resources'] = []  # A PDF main link need not be repeated as a resource.
        item['resources'].append({'link': 'https://openreview.net/forum?id=DifferentNote'})
        called = []
        def get(url):
            called.append(url)
            if url == 'https://openreview.net/forum?id=DifferentNote':
                return '<meta name="citation_title" content="Original title"><meta name="citation_author" content="Alice Example">'
            return json.dumps(self.note(authors=['Anonymous']))
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=get):
            ev = metadata.enrich(item, Path(folder))
        self.assertFalse(ev['complete'])
        self.assertNotIn(item['link'], called)
        self.assertFalse(any('id=DifferentNote' in url and '/notes' in url for url in called))

    def test_wrong_explicit_note_in_cached_supplement_is_rejected(self):
        candidate = {'authors': ['Alice Example'], 'matched_title': 'Original title',
                     'openreview_note_id': 'DifferentNote', 'source': 'https://openreview.net/forum?id=DifferentNote'}
        self.assertIn('different OpenReview note', metadata.validate_supplement(self.item(), candidate))

    def test_exact_original_note_allows_title_change_and_cache_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(metadata, 'get', return_value=json.dumps(self.note(wrapped=False))):
                self.assertTrue(metadata.enrich(self.item(), root)['complete'])
            with patch.object(metadata, 'get', side_effect=AssertionError('Exact note cache should be reused')):
                self.assertTrue(metadata.enrich(self.item(), root)['complete'])


if __name__ == '__main__':
    unittest.main()
