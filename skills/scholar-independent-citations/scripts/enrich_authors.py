#!/usr/bin/env python3
"""Enrich Scholar results with full authors; export conservative classifications."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import socket
import ssl
import time
import unicodedata
import urllib.parse
import urllib.error
import urllib.request


def norm(s):
    value = unicodedata.normalize('NFKD', s).casefold()
    return ''.join(c for c in value if c.isalnum() and not unicodedata.combining(c))


def canonical_name(value):
    """Normalize one structured author field, never a whole author list."""
    value = ' '.join(html.unescape(value).split()).strip()
    if value.count(',') == 1:
        family, given = (part.strip() for part in value.split(',', 1))
        if family and given and not re.fullmatch(r'(?:jr\.?|sr\.?|ii|iii|iv)', given, re.I):
            return given + ' ' + family
    return value


def tokens(s):
    return re.findall(r'[a-z]+', unicodedata.normalize('NFKD', canonical_name(s)).encode('ascii', 'ignore').decode().lower())


def signature(s):
    parts = tokens(s)
    return (parts[-1], ''.join(p[0] if len(p) > 2 else p for p in parts[:-1])) if parts else ('', '')


def possible_same(a, b):
    if norm(canonical_name(a)) == norm(canonical_name(b)):
        return True
    aa, bb = tokens(a), tokens(b)
    if not aa or not bb or aa[-1] != bb[-1]:
        return False
    # A different spelled-out given name proves a different name. Initials
    # provide candidate matches only and never prove author identity.
    def given_form(value):
        # Preserve uppercase packed initials (ABC), but take one initial
        # from spelled-out short names (Yu-Lin -> YL). This aligns bylines;
        # report.py's classification convention remains unchanged.
        raw = unicodedata.normalize('NFKD', canonical_name(value)).encode('ascii', 'ignore').decode()
        given = re.findall(r'[a-z]+', raw, re.I)[:-1]
        full_first = bool(given and len(given[0]) > 1 and not given[0].isupper())
        initials = ''.join(word.lower() if word.isupper() else word[0].lower() for word in given)
        return full_first, initials
    a_full, sa = given_form(a)
    b_full, sb = given_form(b)
    if a_full and b_full:
        return aa[:-1] == bb[:-1]
    return not sa or not sb or sa.startswith(sb) or sb.startswith(sa)


def visible_authors(item):
    info = item.get('publication_info') or {}
    clean = lambda name: re.sub(r'…|\.\.\.|\bet\s+al\.?', '', name, flags=re.I).strip(' ,;')
    authors = [dict(a, name=clean(a['name'])) for a in info.get('authors', []) if clean(a.get('name', ''))]
    for name in (info.get('summary') or '').split(' - ')[0].split(','):
        name = clean(name)
        if name and norm(name) not in {norm(a['name']) for a in authors}:
            authors.append({'name': name})
    return authors


def validate_authors(item, names):
    """Check an automatic supplement against the visible byline, not identity."""
    if not names or any(not isinstance(n, str) or not n.strip() for n in names):
        return 'Missing author names'
    if any(re.search(r'…|\.\.\.|\bet\s+al\b|\band\s+others\b', n, re.I) for n in names):
        return 'Truncated metadata author list'
    if any(re.fullmatch(r'\s*(?:anonymous(?:\s+authors?|\s+submission)?|unknown)\s*', n, re.I) for n in names):
        return 'Anonymous author placeholder'
    options = [[i for i, name in enumerate(names) if possible_same(a['name'], name)] for a in visible_authors(item)]
    occupied = {}
    def assign(slot, seen):
        for candidate in options[slot]:
            if candidate not in seen:
                seen.add(candidate)
                if candidate not in occupied or assign(occupied[candidate], seen):
                    occupied[candidate] = slot
                    return True
        return False
    if not all(assign(slot, set()) for slot in sorted(range(len(options)), key=lambda i: len(options[i]))):
        return 'Metadata does not cover every visible author with a distinct byline slot'
    return ''


def align_authors(visible, names):
    """Keep explicit Scholar IDs only on uniquely aligned metadata authors."""
    authors = [{'name': canonical_name(name)} for name in names]
    for entry in visible:
        if not entry.get('author_id'):
            continue
        exact = [a for a in authors if norm(a['name']) == norm(canonical_name(entry['name']))]
        matches = exact or [a for a in authors if possible_same(a['name'], entry['name'])]
        if len(matches) != 1:
            return [], 'Cannot uniquely preserve a visible Scholar author ID'
        author = matches[0]
        if author.get('author_id') and author['author_id'] != entry['author_id']:
            return [], 'Conflicting visible Scholar author IDs'
        author['author_id'] = entry['author_id']
    return authors, ''


def title_match(a, b):
    return norm(a) == norm(b) or SequenceMatcher(None, norm(a), norm(b)).ratio() >= .97


def arxiv_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname not in ('arxiv.org', 'www.arxiv.org', 'export.arxiv.org'):
        return None
    match = re.match(r'/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?)', parsed.path, re.I)
    return 'https://arxiv.org/abs/' + match.group(1) if match else None


def doi_from_url(url):
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.unquote(parsed.path)
    if parsed.hostname == 'direct.mit.edu' and '/article-pdf/doi/' in path:
        match = re.search(r'/article-pdf/doi/(10\.\d{4,9}/[^/]+)', path, re.I)
        return match.group(1).casefold() if match else None
    match = re.search(r'(10\.\d{4,9}/[^?#\s]+)', path, re.I)
    if not match:
        return None
    doi = re.sub(r'/(?:abstract|full|pdf)$', '', match.group(1).rstrip('/'), flags=re.I)
    if parsed.hostname == 'link.springer.com' and '/content/pdf/' in path and doi.casefold().endswith('.pdf'):
        doi = doi[:-4]
    return doi.casefold()


def ieee_article_id(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != 'ieeexplore.ieee.org':
        return ''
    match = re.search(r'/document/(\d+)', parsed.path)
    return match.group(1) if match else (urllib.parse.parse_qs(parsed.query).get('arnumber') or [''])[0]


def ieee_metadata(url, body):
    identifier = ieee_article_id(url)
    if not identifier:
        return None
    assignment = re.search(r'\bxplGlobal\.document\.metadata\s*=\s*', body)
    if not assignment:
        return None
    work, _ = json.JSONDecoder().raw_decode(body[assignment.end():].lstrip())
    if not isinstance(work, dict) or not identifier or str(work.get('articleId', '')) != identifier:
        raise ValueError('IEEE metadata does not identify the requested article')
    return {'authors': [a['name'] for a in work.get('authors', []) if isinstance(a, dict) and a.get('name')],
            'source': url, 'matched_title': work.get('title', ''), 'method': 'ieee_record_verified',
            'ieee_article_id': identifier, 'doi': work.get('doi', '')}


def openreview_note_id(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname not in ('openreview.net', 'www.openreview.net'):
        return ''
    query = urllib.parse.parse_qs(parsed.query)
    identifier = (query.get('id') or query.get('noteId') or [''])[0]
    return identifier if re.fullmatch(r'[A-Za-z0-9_-]+', identifier) else ''


def openreview_metadata(payload, identifier):
    unwrap = lambda value: value.get('value') if isinstance(value, dict) else value
    for note in payload.get('notes', []):
        if note.get('id') != identifier:
            continue
        content = note.get('content', {})
        authors = unwrap(content.get('authors', []))
        if not isinstance(authors, list) or any(not isinstance(name, str) for name in authors):
            continue
        return {'authors': authors, 'matched_title': unwrap(content.get('title', '')) or '',
                'source': 'https://openreview.net/forum?id=' + identifier,
                'method': 'openreview_note_verified', 'openreview_note_id': identifier,
                'openreview_author_ids': unwrap(content.get('authorids', [])) or []}
    return None


def openreview_routes(identifier, title):
    hosts = ('https://api2.openreview.net', 'https://api.openreview.net')
    direct = [host + '/notes?' + urllib.parse.urlencode({'id': identifier}) for host in hosts]
    terms = list(dict.fromkeys([title, ' '.join(title.split()[-3:])]))
    searches = [host + '/notes/search?' + urllib.parse.urlencode({'term': term, 'content': 'title', 'source': 'forum', 'limit': limit})
                for term in terms for host in hosts for limit in (20, 100)]
    return direct + searches


def validate_supplement(item, evidence):
    reason = validate_authors(item, evidence.get('authors', []))
    if reason:
        return reason
    for expected, supplied, label in [
        (ieee_article_id(item.get('link', '')),
         [evidence.get('ieee_article_id'), ieee_article_id(evidence.get('source', ''))], 'IEEE article'),
        (openreview_note_id(item.get('link', '')),
         [evidence.get('openreview_note_id'), openreview_note_id(evidence.get('source', ''))], 'OpenReview note')]:
        supplied = [str(identifier) for identifier in supplied if identifier]
        if expected and supplied:
            if any(identifier != expected for identifier in supplied):
                return 'Supplement identifies a different ' + label
            return ''
    if evidence.get('matched_title') and not title_match(item.get('title', ''), evidence['matched_title']):
        return 'Metadata title differs'
    return ''


class Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        a = dict(attrs)
        name = (a.get('name') or a.get('property') or '').lower()
        if a.get('content'):
            self.values.setdefault(name, []).append(a['content'])


def safe_url(url):
    parsed = urllib.parse.urlsplit(url)
    secret = {'api_key', 'access_token', 'token', 'authorization', 'auth'}
    query = [(key, 'REDACTED' if key.casefold() in secret else value)
             for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path,
                                   urllib.parse.urlencode(query), parsed.fragment))


class MetadataUnavailable(RuntimeError):
    pass


def get(url):
    """Bound metadata retries; do not change paid Scholar collection behavior."""
    request = urllib.request.Request(url, headers={'User-Agent': 'ScholarCitationAudit/1.0 (bibliographic metadata verification)'})
    deadline = time.monotonic() + 30
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=min(25, max(.1, deadline - time.monotonic()))) as response:
                body = response.read(4_000_000).decode('utf-8', 'replace')
            start = body[:4000].casefold()
            if any(title in start for title in ('<title>verifying your browser', '<title>just a moment', '<title>attention required')):
                raise MetadataUnavailable('Browser verification page; metadata not obtained through this route')
            return body
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code not in (429, 500, 502, 503, 504):
                raise
            if isinstance(error, urllib.error.URLError) and isinstance(error.reason, ssl.SSLCertVerificationError):
                raise
            delay = .25 * (2 ** attempt)
            retry_after = error.headers.get('Retry-After') if isinstance(error, urllib.error.HTTPError) and error.headers else None
            if retry_after:
                try:
                    delay = max(0, float(retry_after))
                except ValueError:
                    try:
                        delay = max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
            if attempt == 2 or delay > 3 or time.monotonic() + delay >= deadline:
                raise
            time.sleep(delay)


def enrich(item, root, retry_unresolved=False):
    title = item.get('title', '')
    key = hashlib.sha256((norm(title) + '|' + (item.get('link') or '')).encode()).hexdigest()[:20]
    path = root / 'metadata' / (key + '.json')
    evidence, errors, attempts = None, [], []
    def note(provider, url, stage, reason, status=None):
        entry = {'provider': provider, 'url': safe_url(url), 'stage': stage, 'reason': reason}
        if status is not None:
            entry['status'] = status
        attempts.append(entry)
    def lookup(url, provider):
        try:
            body = get(url)
        except Exception as error:
            note(provider, url, 'retrieval', str(error) if isinstance(error, MetadataUnavailable) else type(error).__name__, getattr(error, 'code', None))
            raise
        note(provider, url, 'retrieval', 'Response received', 200)
        return body
    if path.exists():
        cached = json.loads(path.read_text())
        reason = validate_supplement(item, cached) if cached.get('complete') else ''
        if not reason and (cached.get('complete') or not retry_unresolved):
            return cached
        if reason:
            errors.append('Cached supplement rejected: ' + reason)
            note('cache', cached.get('source') or item.get('link', ''), 'validation', reason)
    resources = item.get('resources', [])
    original_urls = [item.get('link', '')] + [r.get('link', '') for r in resources]
    original_urls = list(dict.fromkeys(u for u in original_urls if isinstance(u, str) and u.startswith(('https://', 'http://'))))
    original_note = openreview_note_id(item.get('link', ''))
    note_ids = [original_note] if original_note else list(dict.fromkeys(filter(None, (openreview_note_id(u) for u in original_urls))))
    for identifier in note_ids[:1]:
        for route in openreview_routes(identifier, title):
            try:
                candidate = openreview_metadata(json.loads(lookup(route, 'openreview')), identifier)
                if not candidate:
                    note('openreview', route, 'discovery', 'Exact original note ID not returned')
                    continue
                reason = validate_authors(item, candidate['authors'])
                if not reason and identifier != original_note and not title_match(title, candidate['matched_title']):
                    reason = 'Resource-note title differs from the original result'
                if reason:
                    note('openreview', route, 'validation', reason)
                    continue
                evidence = dict(candidate, complete=True, retrieval_url=route)
                break
            except Exception as error:
                errors.append(type(error).__name__)
                if not attempts or attempts[-1].get('reason') == 'Response received':
                    note('openreview', route, 'parsing', type(error).__name__)
        if evidence:
            break
    doi = next((value for value in (doi_from_url(u) for u in original_urls) if value), None)
    pdf_resources = {r.get('link') for r in resources if str(r.get('file_format', '')).upper() == 'PDF'}
    urls = []
    for original_url in original_urls:
        abstract = arxiv_url(original_url)
        if abstract:
            urls.append(abstract)
        elif (original_url not in pdf_resources
              and not (openreview_note_id(original_url) and urllib.parse.urlsplit(original_url).path == '/pdf')
              and not re.search(r'\.pdf(?:[?#]|$)|/doi/(?:e?pdf)/', original_url, re.I)):
            urls.append(original_url)
    urls = list(dict.fromkeys(urls))
    for url in urls[:3]:
        if evidence is not None:
            break
        if original_note and openreview_note_id(url) and openreview_note_id(url) != original_note:
            note('openreview', url, 'matching', 'Resource points to a different original note')
            continue
        try:
            body = lookup(url, 'publisher')
            embedded = ieee_metadata(url, body)
            if embedded:
                reason = validate_authors(item, embedded['authors'])
                if not reason:
                    evidence = dict(embedded, complete=True)
                    break
                note('ieee', url, 'validation', reason)
                doi = doi or embedded.get('doi') or None
            parser = Metadata()
            parser.feed(body)
            citation_doi = next(iter(parser.values.get('citation_doi', [])), '')
            doi = doi or doi_from_url(citation_doi if citation_doi.startswith(('http://', 'https://')) else 'https://doi.org/' + citation_doi)
            authors = parser.values.get('citation_author', [])
            candidate_title = next(iter(parser.values.get('citation_title', [])), '')
            if authors and title_match(title, candidate_title):
                reason = validate_authors(item, authors)
                if not reason:
                    evidence = {'authors': authors, 'complete': True, 'source': url,
                                'matched_title': candidate_title, 'method': 'publisher_citation_meta'}
                    break
                errors.append('Publisher supplement rejected: ' + reason)
                note('publisher', url, 'validation', reason)
            elif authors:
                note('publisher', url, 'matching', 'Metadata title differs')
            else:
                note('publisher', url, 'parsing', 'No citation-author byline extracted by this parser')
        except Exception as exc:
            errors.append(type(exc).__name__)
            if not attempts or attempts[-1].get('reason') == 'Response received':
                note('publisher', url, 'parsing', type(exc).__name__)
    if evidence is None:
        # Use an explicit DOI if available, otherwise strict title matching.
        try:
            if doi:
                url = 'https://api.crossref.org/works/' + urllib.parse.quote(doi, safe='')
                candidates = [json.loads(lookup(url, 'crossref'))['message']]
            else:
                url = 'https://api.crossref.org/works?' + urllib.parse.urlencode({'query.title': title, 'rows': 3})
                candidates = json.loads(lookup(url, 'crossref'))['message']['items']
            accepted = []
            for candidate in candidates:
                ct = next(iter(candidate.get('title', [])), '')
                if not title_match(title, ct) or (doi and candidate.get('DOI', '').casefold() != doi.casefold()):
                    note('crossref', url, 'matching', 'Candidate title or explicit DOI differs')
                    continue
                authors = [' '.join(filter(None, [a.get('given'), a.get('family')])) for a in candidate.get('author', [])]
                reason = validate_authors(item, authors)
                if reason:
                    errors.append('Crossref supplement rejected: ' + reason)
                    note('crossref', url, 'validation', reason)
                elif candidate.get('DOI'):
                    accepted.append({'authors': authors, 'complete': True,
                                     'source': 'https://doi.org/' + candidate['DOI'],
                                     'matched_title': ct, 'method': 'crossref_title_verified',
                                     'doi': candidate['DOI']})
            signatures = {tuple(norm(canonical_name(n)) for n in entry['authors']) for entry in accepted}
            if len(signatures) > 1:
                errors.append('Multiple title-matched Crossref records have conflicting author lists')
                note('crossref', url, 'validation', errors[-1])
            elif accepted:
                evidence = accepted[0]
            elif not candidates:
                note('crossref', url, 'discovery', 'No candidate records returned')
        except Exception as exc:
            errors.append(type(exc).__name__)
            if not attempts or attempts[-1].get('reason') == 'Response received':
                note('crossref', url, 'parsing', type(exc).__name__)
    if evidence is None:
        info = item.get('publication_info', {})
        authors = [a['name'] for a in info.get('authors', [])]
        summary = info.get('summary', '')
        if not authors:
            authors = [a.strip() for a in re.split(r',\s*', summary.split(' - ')[0]) if a.strip()]
        # Scholar can omit authors without a dependable completeness indicator.
        evidence = {'authors': authors, 'complete': False, 'source': item.get('link', ''),
                    'method': 'scholar_visible_authors_only', 'errors': errors}
    evidence['source'] = safe_url(evidence.get('source') or '')
    evidence['attempts'] = attempts
    evidence['looked_up_at_utc'] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return evidence

