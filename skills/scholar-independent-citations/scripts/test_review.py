"""Fictional fixtures only; no user archive, credentials or live requests."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

from report import generate, match_evidence, name_key
from report import render


class ReviewTests(unittest.TestCase):
    def test_explicit_review_is_labeled_and_escaped(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.generate(root, [self.item('other', 'Bob Reader')])
            result['citation_edges'][0]['reviewed_identity_correction'] = {'reviewer': '<script>reviewer</script>', 'reviewed_at_utc': '2000-01-07'}
            result['citation_edges'][0]['source_author_evidence_url'] = 'https://example.org/source'
            render(result, root / 'review.html')
            page = (root / 'review.html').read_text()
            self.assertIn('Reviewed relationship decision:', page)
            self.assertIn('&lt;script&gt;reviewer&lt;/script&gt;', page)
            self.assertNotIn('<script>reviewer', page)
            self.assertIn('Cited author evidence', page)

    def test_full_audit_uses_its_policy_and_evidence_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.generate(root, [self.item('other', 'Bob Reader')])
            result.update(analysis_mode='full_author_audit', policy_description='Compatible initials remain unresolved.')
            result['citation_edges'][0]['author_evidence_level'] = 'matched_full_metadata'
            render(result, root / 'full.html')
            page = (root / 'full.html').read_text()
            self.assertIn('Compatible initials remain unresolved.', page)
            self.assertIn('1 independent candidates with full metadata; 0 with Scholar-only bylines', page)
            self.assertNotIn('Unresolved (author ellipsis)', page)
            self.assertNotIn('Only explicit author-list ellipses require supplementation', page)

    def generate(self, root, citing, sources=1):
        (root / 'profile_SOURCE.json').write_text('{}')
        papers = [{'paper': {'title': 'Same source title', 'citation_id': 'SOURCE:' + str(i),
                             'link': 'https://scholar.google.com/citations?citation_for_view=SOURCE:' + str(i),
                             'year': str(2020 + i)}, 'detail': {'authors': 'Alex Fixture'},
                   'displayed': len(citing), 'end_reason': 'last_page', 'citing': citing} for i in range(sources)]
        return generate({'author_id': 'SOURCE', 'author': {'name': 'Alex Fixture'},
                         'snapshot_date': '2000-01-02', 'papers': papers}, root, root / 'output', True)

    def item(self, rid, name, identifier=None, link='https://publisher.example/work'):
        author = {'name': name}
        if identifier:
            author['author_id'] = identifier
        return {'result_id': rid, 'title': 'Citing ' + rid, 'link': link,
                'publication_info': {'summary': name + ' - Journal, 2000', 'authors': [author]}}

    def test_warns_on_full_names_and_initials_without_changing_statuses(self):
        citing = [self.item('full', 'Alex Fixture'), self.item('initials', 'A Fixture'), self.item('id', 'Different Name', 'SOURCE')]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.generate(root, citing)
            page = (root / 'output/report.html').read_text()
            self.assertEqual(result['classification'], {'independent': 0, 'non_independent': 3, 'unresolved': 0})
            self.assertEqual([r['author_match_kind'] for r in result['citation_edges']], ['full_name', 'initials', 'scholar_id'])
            self.assertEqual(page.count('Potential false positive:'), 2)
            self.assertIn('Review 2 name-based matches', page)
            self.assertIn('<mark>Different Name</mark>', page)
            self.assertIn('Cited paper', page)
            self.assertIn('Citing paper', page)

    def test_match_description_skips_an_explicitly_different_id(self):
        citing = [{'name': 'Alex Fixture', 'author_id': 'OTHER'}, {'name': 'A Fixture'}]
        self.assertEqual(match_evidence('Alex Fixture', citing, {name_key('Alex Fixture'): 'SOURCE'},
                                        'Shared author name (Scholar naming convention)'), ('initials', 'A Fixture'))

    def test_versions_and_same_title_sources_remain_separate(self):
        citing = [self.item('version_one', 'Alex Fixture'), self.item('version_two', 'Alex Fixture')]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.generate(root, citing, sources=2)
            page = (root / 'output/report.html').read_text()
            self.assertEqual(result['retrieved'], 4)
            self.assertEqual(result['classification']['non_independent'], 4)
            self.assertEqual(page.count('<details data-status='), 4)
            self.assertIn('Same source title (2020)', page)
            self.assertIn('Same source title (2021)', page)
            self.assertEqual(len({(r['source_citation_id'], r['scholar_result_id']) for r in result['citation_edges']}), 4)

    def test_linkless_citation_is_labeled_and_offers_a_search(self):
        item = self.item('linkless', 'A Fixture', link='')
        item['type'] = 'Citation'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.generate(root, [item])
            page = (root / 'output/report.html').read_text()
            self.assertIn('linkless Scholar citation record', page)
            self.assertIn('Search this title on Scholar', page)
            self.assertNotIn('href=""', page)
            self.assertEqual(result['citation_edges'][0]['citing_url'], '')

    def test_escaped_text_and_unchanged_default_files_and_csv_schema(self):
        item = self.item('other', 'Bob Reader')
        item['title'] = '<script>unsafe()</script>'
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = self.generate(root, [item])
            page = (root / 'output/report.html').read_text()
            self.assertNotIn('<script>unsafe()', page)
            self.assertIn('&lt;script&gt;unsafe()', page)
            self.assertEqual(sorted(p.name for p in (root / 'output').iterdir()), ['independent_citations.csv', 'report.html'])
            with (root / 'output/independent_citations.csv').open(encoding='utf-8-sig') as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(len(reader.fieldnames), 13)
                self.assertNotIn('author_match_kind', reader.fieldnames)
                self.assertEqual(len(list(reader)), 1)
            self.assertEqual(json.loads((root / 'audit.json').read_text())['classification'], result['classification'])


if __name__ == '__main__':
    unittest.main()
