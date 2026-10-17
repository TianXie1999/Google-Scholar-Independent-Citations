"""Load evidence inputs, replay author metadata, and apply explicit reviews.

Bundles contain evidence and reviewed decisions, never precomputed analysis
totals. Every citation relationship is classified again by the full auditor.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

from author_audit import analyze, native_report, run_audit, source_item, known_ids
from audit_resolver import norm, Resolver

PATH_FIELDS = {'path', 'evidence_pdf_path', 'evidence_text_path', 'evidence_body_path',
               'native_cache', 'reused_resolution_path'}
STATUSES = ('independent', 'non_independent', 'unresolved')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_record(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def verify_file(entry):
    path = Path(entry['path'])
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.get('sha256'):
        raise ValueError('Evidence file missing or hash mismatch: ' + str(path))
    return path


def transform_paths(value, transform, key=''):
    if isinstance(value, dict):
        return {k: transform_paths(v, transform, k) for k, v in value.items()}
    if isinstance(value, list):
        return [transform_paths(v, transform, 'path' if key == 'http_caches' else key) for v in value]
    if isinstance(value, str) and value and key in PATH_FIELDS:
        return transform(value)
    return value


def load_bundle(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text())
    if raw.get('schema_version') != 1:
        raise ValueError('Unsupported audit evidence bundle schema')
    bundle = transform_paths(raw, lambda value: str((path.parent / value).resolve()))
    for entry in bundle.get('files', []):
        verify_file(entry)
    for field in ('snapshot', 'profile'):
        if bundle.get(field):
            verify_file(bundle[field])
    bundle['_manifest'] = str(path)
    bundle['_manifest_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return bundle


def read_json(entry):
    return json.loads(verify_file(entry).read_text())


def apply_reviews(result, decisions):
    """Apply only explicit decisions bound to one source/result and its bylines."""
    rows = {(r['source_citation_id'], r['scholar_result_id']): r for r in result['citation_edges']}
    seen = set()
    for decision in decisions:
        key = decision['source_citation_id'], decision['scholar_result_id']
        if key in seen or key not in rows:
            raise ValueError('Duplicate or unknown reviewed relationship')
        seen.add(key)
        row = rows[key]
        if decision.get('expected_status') != row['status'] or decision.get('status') not in STATUSES:
            raise ValueError('Stale or invalid reviewed classification')
        for title in ('source_title', 'citing_title'):
            if norm(decision.get(title, '')) != norm(row[title]):
                raise ValueError('Reviewed relationship title differs')
        for field in ('source_authors', 'citing_authors'):
            expected = decision.get(field)
            if not isinstance(expected, list) or [norm(n) for n in expected] != [norm(n.strip()) for n in row[field].split(';')]:
                raise ValueError('Reviewed relationship byline differs')
        if decision.get('citing_url') != row['citing_url']:
            raise ValueError('Reviewed relationship URL differs')
        if not decision.get('reason') or not decision.get('reviewer') or not decision.get('evidence'):
            raise ValueError('Reviewed decision requires a reviewer, reason and retained evidence')
        try:
            reviewed_at = datetime.fromisoformat(decision['reviewed_at_utc'].replace('Z', '+00:00'))
            if reviewed_at.tzinfo is None:
                raise ValueError('Timezone required')
        except (KeyError, ValueError, TypeError):
            raise ValueError('Reviewed decision requires a dated review with timezone')
        for entry in decision['evidence']:
            verify_file(entry)
        if decision['status'] == 'independent' and (row['author_evidence_level'] != 'matched_full_metadata'
                                                    or not row['source_author_metadata_complete']):
            raise ValueError('An independent identity decision requires complete source and citing bylines')
        row['status'] = decision['status']
        row['overlapping_author'] = decision.get('overlapping_author', '')
        if row['status'] == 'non_independent' and not row['overlapping_author']:
            raise ValueError('A reviewed shared-author decision must identify its overlap')
        if row['status'] == 'non_independent' and norm(row['overlapping_author']) not in {norm(n) for n in decision['source_authors']}:
            raise ValueError('Reviewed overlapping author is absent from the source byline')
        row['classification_basis'] = decision['reason']
        row['reviewed_identity_correction'] = deepcopy(decision)
    counts = Counter(r['status'] for r in rows.values())
    result['classification'] = {key: counts[key] for key in STATUSES}
    for paper in result['papers']:
        local = Counter(row['status'] for row in rows.values() if row['source_citation_id'] == paper['source_citation_id'])
        paper.update({key: local[key] for key in STATUSES})
    evidence = Counter((row['status'], row['author_evidence_level']) for row in rows.values())
    result['metadata_evidence'] = [{'status': status, 'level': level, 'relationships': count}
                                 for (status, level), count in evidence.items()]
    result['reviewed_relationships'] = len(seen)
    result['classification_is_automated'] = not bool(seen)
    return result


def run_bundle(data, profile, cache, bundle, replay=False, offline=False, workers=4, retry_unresolved=False):
    inputs = bundle.get('inputs', {})
    if bundle.get('author_id') and bundle['author_id'] != data['author_id']:
        raise ValueError('Evidence bundle belongs to a different author profile')
    if replay:
        if not bundle.get('snapshot') or not bundle.get('profile') or not all((bundle.get('replay') or {}).get(k) for k in ('authors', 'sources')):
            raise ValueError('Replay bundle requires snapshot, profile, author and source evidence')
        if data != read_json(bundle['snapshot']) or profile != read_json(bundle['profile']):
            raise ValueError('Replay requires the exact bundled snapshot and profile')
        evidence = bundle.get('replay') or {}
        resolutions = transform_paths(read_json(evidence['authors']), lambda value: str((Path(evidence['authors']['path']).parent / value).resolve()))
        sources = transform_paths(read_json(evidence['sources']), lambda value: str((Path(evidence['sources']['path']).parent / value).resolve()))
        items = {item['result_id']: item for paper in data['papers'] for item in paper['citing']}
        if set(resolutions) != set(items) or set(sources) != {p['paper']['citation_id'] for p in data['papers']}:
            raise ValueError('Replay evidence does not cover exactly the snapshot records')
        ids = known_ids(data, profile, inputs.get('source_identities'))
        for key, item in items.items():
            candidate = resolutions[key]
            if candidate.get('result_id') != key:
                raise ValueError('Replay author evidence identifies a different result')
            if candidate.get('complete'):
                reason = Resolver.validate(item, dict(candidate))
                if reason:
                    raise ValueError('Replay author evidence rejected: ' + reason)
        for paper in data['papers']:
            candidate = sources[paper['paper']['citation_id']]
            if candidate.get('complete'):
                reason = Resolver.validate(source_item(paper, ids), dict(candidate))
                if reason:
                    raise ValueError('Replay source evidence rejected: ' + reason)
        result = analyze(data, profile, resolutions, sources, inputs.get('source_identities'))
        result['new_metadata_lookups'] = 0
        # Keep evidence records for exporting a new replay manifest.
        cache = Path(cache); (cache / 'authors').mkdir(parents=True, exist_ok=True)
        for key, record in resolutions.items():
            if not key or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in key):
                raise ValueError('Invalid Scholar result ID in replay')
            (cache / 'authors' / (key + '.json')).write_text(json.dumps(record, ensure_ascii=False, indent=2))
        (cache / 'source-authors.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2))
    else:
        result = run_audit(data, profile, cache, offline=offline, workers=workers,
                           native_cache=inputs.get('native_cache'), http_caches=inputs.get('http_caches'),
                           discoveries=inputs.get('discoveries'), supplements=inputs.get('supplements'),
                           identities=inputs.get('source_identities'), retry_unresolved=retry_unresolved)
    apply_reviews(result, bundle.get('review_decisions', []))
    result = native_report(result, data)
    result['evidence_bundle_sha256'] = bundle['_manifest_sha256']
    result['snapshot_content_sha256'] = digest(data)
    result['profile_content_sha256'] = digest(profile)
    result['evidence_mode'] = 'historical_replay' if replay else 'full_author_audit'
    return result


def export_bundle(path, data, profile, cache, bundle=None):
    """Freeze author evidence; reference existing retained source files by hash.

    No PDF is modified or copied. Keep the referenced evidence files available.
    """
    path = Path(path).resolve()
    if path.exists():
        raise ValueError('Choose a new evidence-bundle path; refusing to overwrite a manifest')
    folder = path.parent / (path.stem + '-files')
    folder.mkdir(parents=True, exist_ok=False)
    ids = {item['result_id'] for paper in data['papers'] for item in paper['citing']}
    authors = {key: json.loads((Path(cache) / 'authors' / (key + '.json')).read_text()) for key in sorted(ids)}
    sources = json.loads((Path(cache) / 'source-authors.json').read_text())
    records = {}
    for name, value in [('snapshot', data), ('profile', profile), ('authors', authors), ('sources', sources)]:
        # Metadata records can contain evidence paths, relative to this JSON file.
        value = transform_paths(value, lambda p: os.path.relpath(p, folder)) if name in ('authors', 'sources') else value
        target = folder / (name + '.json')
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        records[name] = file_record(target)
    result = deepcopy(bundle or {})
    result = {k: v for k, v in result.items() if not k.startswith('_')}
    result.update(schema_version=1, author_id=data['author_id'], snapshot=records['snapshot'], profile=records['profile'],
                  replay={'authors': records['authors'], 'sources': records['sources']})
    result.setdefault('inputs', {})
    http = Path(cache).resolve() / 'http'
    if http.is_dir():
        result['inputs']['http_caches'] = list(dict.fromkeys(result['inputs'].get('http_caches', []) + [str(http)]))
    result.setdefault('review_decisions', [])
    files = {entry['path']: entry for entry in result.get('files', [])}
    def register(value):
        candidate = Path(value)
        if candidate.is_file():
            entry = file_record(candidate)
            files[entry['path']] = entry
        return value
    for value in (result.get('inputs', {}), result.get('review_decisions', []), authors, sources):
        transform_paths(value, register)
    if http.is_dir():
        for candidate in http.iterdir():
            if candidate.suffix in ('.json', '.body'):
                register(str(candidate))
    files.update({entry['path']: entry for entry in records.values()})
    result['files'] = list(files.values())
    result = transform_paths(result, lambda p: os.path.relpath(p, path.parent))
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return path
