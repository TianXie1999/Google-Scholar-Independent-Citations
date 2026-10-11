#!/usr/bin/env python3
"""Enrich Scholar results with full authors; export conservative classifications."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from difflib import SequenceMatcher
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import unicodedata
import urllib.parse
import urllib.request


def norm(s):
    value = unicodedata.normalize('NFKD', s).casefold()
    return ''.join(c for c in value if c.isalnum() and not unicodedata.combining(c))


def tokens(s):
    return re.findall(r'[a-z]+', unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower())


def signature(s):
    parts = tokens(s)
    return (parts[-1], ''.join(p[0] if len(p) > 2 else p for p in parts[:-1])) if parts else ('', '')


def possible_same(a, b):
    aa, bb = tokens(a), tokens(b)
    if not aa or not bb or aa[-1] != bb[-1]:
        return False
    # A different spelled-out given name proves a different name. Initials
    # provide candidate matches only and never prove author identity.
    if len(aa) > 1 and len(bb) > 1 and len(aa[0]) > 2 and len(bb[0]) > 2:
        return aa[0] == bb[0]
    sa, sb = signature(a)[1], signature(b)[1]
    return not sa or not sb or sa.startswith(sb) or sb.startswith(sa)


def title_match(a, b):
    return norm(a) == norm(b) or SequenceMatcher(None, norm(a), norm(b)).ratio() >= .97


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


def get(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'ScholarCitationAudit/1.0 (bibliographic metadata verification)'})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read(4_000_000).decode('utf-8', 'replace')


def enrich(item, root):
    title = item.get('title', '')
    key = hashlib.sha256((norm(title) + '|' + (item.get('link') or '')).encode()).hexdigest()[:20]
    path = root / 'metadata' / (key + '.json')
    if path.exists():
        return json.loads(path.read_text())
    evidence, errors = None, []
    urls = [item.get('link', '')] + [r.get('link', '') for r in item.get('resources', [])]
    # Bibliographic HTML only. PDF resources are left for manual review.
    urls = list(dict.fromkeys(u for u in urls if isinstance(u, str) and u.startswith(('https://', 'http://')) and
                             not re.search(r'\.pdf(?:[?#]|$)', u, re.I)))
    for url in urls[:3]:
        if 'arxiv.org' in url:
            url = re.sub(r'/pdf/', '/abs/', url).removesuffix('.pdf')
        try:
            parser = Metadata()
            parser.feed(get(url))
            authors = parser.values.get('citation_author', [])
            candidate_title = next(iter(parser.values.get('citation_title', [])), '')
            if authors and title_match(title, candidate_title):
                # Fail closed for obviously truncated metadata lists.
                complete = not any(re.search(r'…|\.\.\.|et al', a, re.I) for a in authors)
                evidence = {'authors': authors, 'complete': complete, 'source': url,
                            'matched_title': candidate_title, 'method': 'publisher_citation_meta'}
                break
        except Exception as exc:
            errors.append(type(exc).__name__)
    if evidence is None:
        # Use an explicit DOI if available, otherwise strict title matching.
        doi = None
        for u in urls:
            match = re.search(r'(10\.\d{4,9}/[^?#]+)', urllib.parse.unquote(u))
            if match:
                doi = re.sub(r'/(?:abstract|full|pdf)$', '', match.group(1), flags=re.I)
                break
        try:
            if doi:
                url = 'https://api.crossref.org/works/' + urllib.parse.quote(doi, safe='')
                candidates = [json.loads(get(url))['message']]
            else:
                url = 'https://api.crossref.org/works?' + urllib.parse.urlencode({'query.title': title, 'rows': 3})
                candidates = json.loads(get(url))['message']['items']
            for candidate in candidates:
                ct = next(iter(candidate.get('title', [])), '')
                if not title_match(title, ct):
                    continue
                authors = [' '.join(filter(None, [a.get('given'), a.get('family')])) for a in candidate.get('author', [])]
                if authors and all(authors):
                    evidence = {'authors': authors, 'complete': True,
                                'source': 'https://doi.org/' + candidate['DOI'],
                                'matched_title': ct, 'method': 'crossref_title_verified',
                                'doi': candidate['DOI']}
                    break
        except Exception as exc:
            errors.append(type(exc).__name__)
    if evidence is None:
        info = item.get('publication_info', {})
        authors = [a['name'] for a in info.get('authors', [])]
        summary = info.get('summary', '')
        if not authors:
            authors = [a.strip() for a in re.split(r',\s*', summary.split(' - ')[0]) if a.strip()]
        # Scholar can omit authors without a dependable completeness indicator.
        evidence = {'authors': authors, 'complete': False, 'source': item.get('link', ''),
                    'method': 'scholar_visible_authors_only', 'errors': errors}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    return evidence

