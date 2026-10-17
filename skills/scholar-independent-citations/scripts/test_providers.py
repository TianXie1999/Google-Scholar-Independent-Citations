"""Fictional fixtures only; no user archive, credentials or live requests."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import enrich_authors as metadata


class ProviderTests(unittest.TestCase):
    def item(self, url):
        return {'title': 'Original title', 'link': url, 'publication_info': {
            'authors': [{'name': 'A Example'}], 'summary': 'A Example…'}}

    def test_arxiv_pdf_is_normalized_before_skipping_pdf_bodies(self):
        page = '<meta name="citation_title" content="Original title"><meta name="citation_author" content="Alice Example">'
        item = self.item('https://arxiv.org/pdf/1234.56789v2.pdf')
        item['resources'] = [{'link': item['link'], 'file_format': 'PDF'}]
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', return_value=page) as get:
            ev = metadata.enrich(item, Path(folder))
        get.assert_called_once_with('https://arxiv.org/abs/1234.56789v2')
        self.assertTrue(ev['complete'])
        self.assertIsNone(metadata.arxiv_url('https://not-arxiv.org/pdf/1234.56789'))

    def test_doi_is_retained_when_pdf_resource_is_skipped(self):
        item = self.item('https://publisher.example/work')
        item['resources'] = [{'link': 'https://link.springer.com/content/pdf/10.1007/example.pdf', 'file_format': 'PDF'}]
        calls = []
        def get(url):
            calls.append(url)
            if 'api.crossref.org' in url:
                return json.dumps({'message': {'title': ['Original title'], 'DOI': '10.1007/example',
                                              'author': [{'given': 'Alice', 'family': 'Example'}]}})
            return '<html></html>'
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=get):
            ev = metadata.enrich(item, Path(folder))
        self.assertTrue(ev['complete'])
        self.assertIn('https://api.crossref.org/works/10.1007%2Fexample', calls)
        self.assertFalse(any('/content/pdf/' in url for url in calls))

    def test_doi_parsing_keeps_real_suffixes_and_handles_known_mit_path(self):
        self.assertEqual(metadata.doi_from_url('https://doi.org/10.1234/real.pdf'), '10.1234/real.pdf')
        self.assertEqual(metadata.doi_from_url('https://direct.mit.edu/tacl/article-pdf/doi/10.1162/tacl_a_00001/123456/file.pdf'), '10.1162/tacl_a_00001')

    def test_ieee_json_uses_requested_record_even_with_a_title_variant(self):
        url = 'https://ieeexplore.ieee.org/abstract/document/12345/'
        work = {'articleId': '12345', 'title': 'An updated title', 'doi': '10.1109/example',
                'authors': [{'name': 'Alice Example'}, {'name': 'Bob Reader'}]}
        body = '<script>xplGlobal.document.metadata = ' + json.dumps(work) + '; unsafeJavaScript();</script>'
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', return_value=body) as get:
            ev = metadata.enrich(self.item(url), Path(folder))
        get.assert_called_once_with(url)
        self.assertEqual(ev['method'], 'ieee_record_verified')
        self.assertEqual(ev['authors'], ['Alice Example', 'Bob Reader'])

    def test_ieee_wrong_id_cannot_be_rescued_by_matching_meta_tags(self):
        url = 'https://ieeexplore.ieee.org/document/12345/'
        body = 'xplGlobal.document.metadata = ' + json.dumps({'articleId': '99999', 'authors': [{'name': 'Alice Example'}]})
        body += '<meta name="citation_title" content="Original title"><meta name="citation_author" content="Alice Example">'
        def get(route):
            return json.dumps({'message': {'items': []}}) if 'api.crossref.org' in route else body
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=get):
            ev = metadata.enrich(self.item(url), Path(folder))
        self.assertFalse(ev['complete'])
        with self.assertRaises(ValueError):
            metadata.ieee_metadata(url, 'xplGlobal.document.metadata = not valid JSON;')

    def test_explicit_ieee_record_identity_survives_cache_reuse(self):
        url = 'https://ieeexplore.ieee.org/document/12345/'
        body = 'xplGlobal.document.metadata = ' + json.dumps({'articleId': '12345', 'title': 'Updated title',
                                                            'authors': [{'name': 'Alice Example'}]})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(metadata, 'get', return_value=body):
                self.assertTrue(metadata.enrich(self.item(url), root)['complete'])
            with patch.object(metadata, 'get', side_effect=AssertionError('Known record should use its cache')):
                self.assertTrue(metadata.enrich(self.item(url), root)['complete'])


if __name__ == '__main__':
    unittest.main()
