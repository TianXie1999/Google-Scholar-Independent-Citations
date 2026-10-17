"""Fictional fixtures only; no user archive, credentials or live requests."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import enrich_authors as metadata
import report


class EnrichmentTests(unittest.TestCase):
    def item(self, names=None):
        names = names or [{'name': 'A Example'}]
        return {'title': 'Citing work', 'result_id': 'CITING', 'link': 'https://publisher.example/work',
                'publication_info': {'authors': names,
                                     'summary': ', '.join(a['name'] for a in names) + '… - Journal, 2000'}}

    def generate(self, item, supplement, offline=False, cached=False, reviewed=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'profile_SOURCE.json').write_text('{}')
            if cached:
                digest = hashlib.sha256((metadata.norm(item['title']) + '|' + item['link']).encode()).hexdigest()[:20]
                (root / 'metadata').mkdir()
                (root / 'metadata' / (digest + '.json')).write_text(json.dumps(supplement))
            if reviewed:
                (root / 'reviewed_authors.json').write_text(json.dumps({item['title']: supplement}))
            data = {'author_id': 'SOURCE', 'author': {'name': 'Alex Fixture'}, 'snapshot_date': '2000-01-02',
                    'papers': [{'paper': {'title': 'Source work', 'link': 'https://scholar.google.com/citations?user=SOURCE'},
                                'detail': {'authors': 'Alex Fixture'}, 'displayed': 1,
                                'end_reason': 'last_page', 'citing': [item]}]}
            with patch.object(report, 'enrich', return_value=supplement), patch.object(metadata, 'get', side_effect=AssertionError('Unexpected HTTP')):
                return report.generate(data, root, root / 'output', offline)

    def test_different_visible_id_survives_enrichment(self):
        item = self.item([{'name': 'A Fixture', 'author_id': 'OTHER_PERSON'}])
        supplement = {'authors': ['Alex Fixture'], 'complete': True, 'source': item['link']}
        result = self.generate(item, supplement)
        self.assertEqual(result['classification']['independent'], 1)

    def test_equal_id_survives_full_name_expansion(self):
        authors, error = metadata.align_authors([{'name': 'A Fixture', 'author_id': 'SOURCE'}], ['Alex Fixture'])
        self.assertFalse(error)
        self.assertEqual(report.classify(['Alex Fixture'], authors, {report.name_key('Alex Fixture'): 'SOURCE'}, False)[0], 'non_independent')

    def test_ambiguous_or_conflicting_ids_are_not_assigned(self):
        authors, error = metadata.align_authors([{'name': 'A Fixture', 'author_id': 'OTHER'}], ['Alex Fixture', 'Adam Fixture'])
        self.assertFalse(authors)
        self.assertIn('uniquely', error)
        authors, error = metadata.align_authors([{'name': 'A Fixture', 'author_id': 'ONE'}, {'name': 'Alex Fixture', 'author_id': 'TWO'}], ['Alex Fixture'])
        self.assertFalse(authors)
        self.assertIn('Conflicting', error)

    def test_surname_first_field_matches_without_changing_initial_policy(self):
        self.assertTrue(report.name_matches('Alice Example', 'Example, Alice'))
        self.assertTrue(report.name_matches('Alice Example', 'A Example'))
        self.assertEqual(metadata.canonical_name('John Example, Jr.'), 'John Example, Jr.')
        self.assertEqual(metadata.canonical_name('例甲'), '例甲')
        self.assertFalse(report.name_matches('例甲', '例乙'))

    def test_author_coverage_uses_distinct_slots(self):
        item = self.item([{'name': 'A Fixture'}, {'name': 'A Fixture'}])
        self.assertTrue(metadata.validate_authors(item, ['Alex Fixture']))
        self.assertFalse(metadata.validate_authors(item, ['Alex Fixture', 'Adam Fixture']))

    def test_reject_missing_visible_author_and_anonymous_placeholders(self):
        item = self.item()
        self.assertIn('visible author', metadata.validate_authors(item, ['Unrelated Person']))
        for name in ['Anonymous', 'Anonymous Authors', 'Unknown', 'Alice Example et al.', 'Alice Example…']:
            self.assertTrue(metadata.validate_authors(item, [name]))
        self.assertFalse(metadata.validate_authors(item, ['Alice Example']))

    def test_rejects_different_spelled_out_middle_names_for_alignment(self):
        self.assertFalse(metadata.possible_same('John Adam Example', 'John Alan Example'))
        self.assertTrue(metadata.possible_same('John Adam Example', 'J A Example'))

    def test_summary_authors_are_not_lost_when_structured_list_is_shorter(self):
        item = self.item()
        item['publication_info']['summary'] = 'A Example, B Reader… - Journal'
        self.assertTrue(metadata.validate_authors(item, ['Alice Example']))

    def test_packed_initials_align_to_multiple_given_names_without_changing_classifier(self):
        item = self.item([{'name': 'ABC Reader', 'author_id': 'VISIBLE_PERSON'}])
        names = ['Alice Beth Carol Reader']
        self.assertFalse(metadata.validate_authors(item, names))
        authors, error = metadata.align_authors(metadata.visible_authors(item), names)
        self.assertFalse(error)
        self.assertEqual(authors[0]['author_id'], 'VISIBLE_PERSON')
        self.assertFalse(metadata.possible_same('ABC Reader', 'Alice Beth David Reader'))
        self.assertFalse(metadata.possible_same('Abc Reader', 'Alice Beth Carol Reader'))
        self.assertFalse(report.name_matches('ABC Reader', 'Alice Beth Carol Reader'))
        self.assertTrue(metadata.possible_same('VL Reader', 'Ve-Len Reader'))
        self.assertTrue(metadata.possible_same('KH Reader', 'Khem-Halen Reader'))
        self.assertFalse(metadata.possible_same('Ve Reader', 'Vi Reader'))

    def test_invalid_automatic_supplement_stays_unresolved_online_and_offline(self):
        item = self.item()
        bad = {'authors': ['Unrelated Person'], 'complete': True, 'source': item['link']}
        self.assertEqual(self.generate(item, bad)['classification']['unresolved'], 1)
        self.assertEqual(self.generate(item, bad, offline=True, cached=True)['classification']['unresolved'], 1)

    def test_documented_manual_review_can_correct_a_bad_visible_name(self):
        item = self.item([{'name': 'Misparsed Affiliation'}])
        checked = {'authors': ['Alice Example'], 'source': 'https://publisher.example/checked'}
        result = self.generate(item, checked, offline=True, reviewed=True)
        self.assertEqual(result['classification']['independent'], 1)

    def crossref(self, candidates):
        def get(url):
            if 'api.crossref.org' in url:
                return json.dumps({'message': {'items': candidates}})
            return '<html></html>'
        return get

    def work(self, doi, authors, title='Citing work'):
        return {'title': [title], 'DOI': doi,
                'author': [{'given': name.split()[0], 'family': name.split()[-1]} for name in authors]}

    def test_crossref_skips_contradictory_first_result(self):
        candidates = [self.work('10.1234/wrong', ['Unrelated Person']), self.work('10.1234/right', ['Alice Example'])]
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=self.crossref(candidates)):
            ev = metadata.enrich(self.item(), Path(folder))
        self.assertEqual(ev['doi'], '10.1234/right')

    def test_conflicting_compatible_crossref_records_remain_unresolved(self):
        candidates = [self.work('10.1234/one', ['Alice Example']), self.work('10.1234/two', ['Anna Example'])]
        with tempfile.TemporaryDirectory() as folder, patch.object(metadata, 'get', side_effect=self.crossref(candidates)):
            ev = metadata.enrich(self.item(), Path(folder))
        self.assertFalse(ev['complete'])
        self.assertIn('conflicting', ' '.join(ev['errors']))

    def test_valid_legacy_cache_is_reused_and_bad_cache_is_revalidated(self):
        item = self.item()
        digest = hashlib.sha256((metadata.norm(item['title']) + '|' + item['link']).encode()).hexdigest()[:20]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'metadata').mkdir()
            path = root / 'metadata' / (digest + '.json')
            path.write_text(json.dumps({'authors': ['Alice Example'], 'complete': True, 'source': item['link']}))
            with patch.object(metadata, 'get', side_effect=AssertionError('Valid cache must not fetch')):
                self.assertTrue(metadata.enrich(item, root)['complete'])
            path.write_text(json.dumps({'authors': ['Unrelated Person'], 'complete': True, 'source': item['link']}))
            page = '<meta name="citation_title" content="Citing work"><meta name="citation_author" content="Alice Example">'
            with patch.object(metadata, 'get', return_value=page):
                self.assertEqual(metadata.enrich(item, root)['authors'], ['Alice Example'])

    def test_toward_towards_title_tolerance_is_preserved(self):
        self.assertTrue(metadata.title_match('Example Metadata Recovery: Towards Reliable Author Lists with Structured Records',
                                            'Example Metadata Recovery: Toward Reliable Author Lists with Structured Records'))


if __name__ == '__main__':
    unittest.main()
