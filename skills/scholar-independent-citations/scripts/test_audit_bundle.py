"""Fictional fixtures only; no user archive, credentials or live requests."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import audit_bundle as bundles
import report


class BundleTests(unittest.TestCase):
    def fixture(self, root, review=True):
        item = {'result_id': 'CITING', 'title': 'Citing work', 'link': 'https://example.org/citing',
                'publication_info': {'summary': 'A Fixture - Journal, 2000', 'authors': [{'name': 'A Fixture'}]}}
        data = {'author_id': 'OWNER', 'author': {'name': 'Alex Fixture'}, 'snapshot_date': '2000-01-07',
                'papers': [{'paper': {'citation_id': 'OWNER:SOURCE', 'title': 'Source work', 'link': 'https://example.org/source'},
                            'detail': {'authors': 'Alex Fixture', 'link': 'https://example.org/source'},
                            'displayed': 1, 'end_reason': 'last_page', 'citing': [item]}]}
        profile = {'cited_by': {'table': [{'citations': {'all': 1}}]}}
        author = {'result_id': 'CITING', 'complete': True, 'title': item['title'], 'source': item['link'],
                  'provider': 'primary_html', 'authors': [{'name': 'Alex Fixture'}],
                  'status': 'independent'}  # Deliberately wrong precomputed field: must be ignored.
        source = dict(author, title='Source work', source='https://example.org/source')
        values = {'snapshot': data, 'profile': profile, 'authors': {'CITING': author}, 'sources': {'OWNER:SOURCE': source}}
        entries = {}
        for name, value in values.items():
            p = root / (name + '.json'); p.write_text(json.dumps(value)); entries[name] = bundles.file_record(p)
        evidence = root / 'review.txt'; evidence.write_text('An explicitly reviewed distinction between the two people.')
        decision = {'source_citation_id': 'OWNER:SOURCE', 'scholar_result_id': 'CITING', 'source_title': 'Source work',
                    'citing_title': 'Citing work', 'citing_url': item['link'], 'source_authors': ['Alex Fixture'],
                    'citing_authors': ['Alex Fixture'], 'expected_status': 'non_independent', 'status': 'independent',
                    'reviewer': 'Human reviewer', 'reason': 'Reviewed distinct people with the same full name.',
                    'reviewed_at_utc': '2000-01-07T00:00:00+00:00', 'evidence': [bundles.file_record(evidence)]}
        manifest = {'schema_version': 1, 'author_id': 'OWNER', 'snapshot': entries['snapshot'], 'profile': entries['profile'],
                    'replay': {'authors': entries['authors'], 'sources': entries['sources']},
                    'review_decisions': [decision] if review else [], 'inputs': {},
                    'files': [bundles.file_record(evidence)], 'classification': {'independent': 99999}}
        path = root / 'bundle.json'; path.write_text(json.dumps(manifest))
        return path, data, profile

    def run_bundle(self, root, path, data, profile, **kwargs):
        with patch('urllib.request.urlopen', side_effect=AssertionError('Replay must not access network')):
            return bundles.run_bundle(data, profile, root / 'cache', bundles.load_bundle(path), replay=True, **kwargs)

    def test_reclassifies_metadata_and_applies_only_explicit_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root, review=False)
            automatic = self.run_bundle(root, path, data, profile)
            self.assertEqual(automatic['classification']['non_independent'], 1)
            self.assertTrue(automatic['classification_is_automated'])
            manifest = json.loads(path.read_text()); manifest['review_decisions'] = [self.decision(root)]
            path.write_text(json.dumps(manifest))
            reviewed = self.run_bundle(root, path, data, profile)
            self.assertEqual(reviewed['classification']['independent'], 1)
            self.assertEqual(reviewed['reviewed_relationships'], 1)
            self.assertFalse(reviewed['classification_is_automated'])
            self.assertEqual(reviewed['new_metadata_lookups'], 0)

    def decision(self, root):
        # Build the same synthetic approved decision without changing evidence.
        return {'source_citation_id': 'OWNER:SOURCE', 'scholar_result_id': 'CITING', 'source_title': 'Source work',
                'citing_title': 'Citing work', 'citing_url': 'https://example.org/citing', 'source_authors': ['Alex Fixture'],
                'citing_authors': ['Alex Fixture'], 'expected_status': 'non_independent', 'status': 'independent',
                'reviewer': 'Human reviewer', 'reason': 'Reviewed distinct people with the same full name.',
                'reviewed_at_utc': '2000-01-07T00:00:00+00:00', 'evidence': [bundles.file_record(root / 'review.txt')]}

    def test_changed_source_file_is_rejected_before_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            (root / 'authors.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                self.run_bundle(root, path, data, profile)

    def test_missing_author_record_is_rejected_even_with_updated_file_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            (root / 'authors.json').write_text('{}')
            manifest = json.loads(path.read_text()); manifest['replay']['authors'] = bundles.file_record(root / 'authors.json')
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'cover exactly'):
                self.run_bundle(root, path, data, profile)

    def test_snapshot_and_profile_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            for changed_data, changed_profile in [(dict(data, snapshot_date='2000-01-08'), profile), (data, {})]:
                with self.assertRaisesRegex(ValueError, 'exact bundled'):
                    self.run_bundle(root, path, changed_data, changed_profile)

    def test_review_requires_matching_pair_bylines_status_and_evidence(self):
        for field, value in [('citing_authors', ['Other Person']), ('source_title', 'Other source'),
                             ('expected_status', 'independent'), ('evidence', []), ('reviewer', ''),
                             ('reviewed_at_utc', '2000-01-07'), ('scholar_result_id', 'OTHER')]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); path, data, profile = self.fixture(root)
                manifest = json.loads(path.read_text()); manifest['review_decisions'][0][field] = value
                path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError): self.run_bundle(root, path, data, profile)

    def test_duplicate_review_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            manifest = json.loads(path.read_text()); manifest['review_decisions'] *= 2; path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                self.run_bundle(root, path, data, profile)

    def test_manual_independence_cannot_fill_an_unknown_byline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            bundle = bundles.load_bundle(path)
            result = self.run_bundle(root, path, data, profile)
            row = result['citation_edges'][0]; row.update(status='non_independent', author_evidence_level='scholar_visible_only')
            with self.assertRaisesRegex(ValueError, 'complete source and citing'):
                bundles.apply_reviews(result, bundle['review_decisions'])

    def test_single_cli_command_needs_no_collector_or_extra_profile_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            argv = ['report.py', '--evidence-bundle', str(path), '--replay', '--output-dir', str(root / 'output')]
            with patch('sys.argv', argv), patch.object(report, 'collect', side_effect=AssertionError('No collection')), \
                 patch('urllib.request.urlopen', side_effect=AssertionError('No network')):
                with redirect_stdout(io.StringIO()): report.main()
            audit = json.loads((root / 'output/.scholar-cache/OWNER/audit.json').read_text())
            self.assertEqual(audit['classification']['independent'], 1)
            self.assertEqual({p.name for p in (root / 'output').iterdir() if not p.name.startswith('.')},
                             {'report.html', 'independent_citations.csv'})

    def test_export_and_replay_preserve_decisions_and_relative_evidence_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            result = self.run_bundle(root, path, data, profile)
            exported = bundles.export_bundle(root / 'exported.json', data, profile, root / 'cache', bundles.load_bundle(path))
            raw = json.loads(exported.read_text())
            self.assertFalse(Path(raw['snapshot']['path']).is_absolute())
            replayed = bundles.run_bundle(data, profile, root / 'again', bundles.load_bundle(exported), replay=True)
            self.assertEqual(replayed['classification'], result['classification'])
            with self.assertRaisesRegex(ValueError, 'overwrite'):
                bundles.export_bundle(exported, data, profile, root / 'cache')

    def test_fresh_audit_does_not_import_historical_author_records(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root, review=False)
            def get(url):
                title, name = ('Citing work', 'Adam Fixture') if url.endswith('citing') else ('Source work', 'Alex Fixture')
                return '<meta name="citation_title" content="' + title + '"><meta name="citation_author" content="' + name + '">'
            with patch('audit_resolver.metadata_get', side_effect=get):
                fresh = bundles.run_bundle(data, profile, root / 'fresh', bundles.load_bundle(path))
            self.assertEqual(fresh['classification']['independent'], 1)
            self.assertEqual(fresh['citation_edges'][0]['citing_authors'], 'Adam Fixture')

    def test_new_collection_uses_its_new_profile_instead_of_bundled_old_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, data, profile = self.fixture(root)
            current_profile = dict(profile, co_authors=[{'name': 'Other Reader', 'author_id': 'NEW'}])
            def collect(author_id, cache, budget):
                cache = Path(cache); cache.mkdir(parents=True)
                (cache / 'profile_OWNER.json').write_text(json.dumps(current_profile))
                return data
            def get(url):
                title = 'Citing work' if url.endswith('citing') else 'Source work'
                return '<meta name="citation_title" content="' + title + '"><meta name="citation_author" content="Alex Fixture">'
            argv = ['report.py', '--evidence-bundle', str(path), '--scholar-url', 'https://scholar.google.com/citations?user=OWNER',
                    '--output-dir', str(root / 'output')]
            with patch('sys.argv', argv), patch.object(report, 'collect', side_effect=collect), patch('audit_resolver.metadata_get', side_effect=get):
                with redirect_stdout(io.StringIO()): report.main()
            audit = json.loads((root / 'output/.scholar-cache/OWNER/audit.json').read_text())
            self.assertEqual(audit['profile_content_sha256'], bundles.digest(current_profile))


if __name__ == '__main__':
    unittest.main()
