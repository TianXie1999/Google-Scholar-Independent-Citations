#!/usr/bin/env python3
"""Resolve complete author metadata without treating initials as author identity.

The resolver uses standard-library HTTP, exact record/title matching, visible
author agreement, a durable response cache, and explicit rejection reasons.
Fetching an author list once never collapses citation relationships.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from enrich_authors import canonical_name as structured_name, safe_url, get as metadata_get

RESOLUTION_POLICY = "full-author-audit-v1"


def norm(value):
    value = html.unescape(re.sub(r"<[^>]*>", "", str(value)))
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(c for c in value if c.isalnum() and not unicodedata.combining(c))


def canonical_name(value):
    return structured_name(str(value)).strip("* ")


def words(value):
    value = unicodedata.normalize("NFKD", canonical_name(value))
    return re.findall(r"[^\W\d_]+", "".join(c for c in value if not unicodedata.combining(c)), re.UNICODE)


def is_truncated(value):
    return bool(re.search(r"…|\.\.\.|\bet\s+al\b|\band\s+others\b", str(value), re.I))


def suspicious_author_name(value):
    """Flag known non-person byline fragments; absence of a flag is not verification."""
    return bool(re.search(r"\b(?:anonymous|unknown|university|institute|department|laborator(?:y|ies)|school|faculty|MPI[- ]SWS)\b", str(value), re.I))


def initial_word(value):
    return len(value) == 1 or (value.isupper() and len(value) <= 8)


def family_variants(value):
    # Used only to align a byline, never as proof of author identity.
    return {norm(value), norm(value.casefold().translate(str.maketrans({"ö": "oe", "ä": "ae", "ü": "ue"})))}


def compatible_name(left, right):
    """Compatibility is for aligning names, never proof of shared author identity."""
    if norm(canonical_name(left)) == norm(canonical_name(right)): return True
    aa, bb = words(left), words(right)
    if len(aa) < 2 or len(bb) < 2: return False
    # Keep the original diacritics for the optional transliteration alignment.
    left_family = re.findall(r"[^\W\d_]+", canonical_name(left), re.UNICODE)[-1]
    right_family = re.findall(r"[^\W\d_]+", canonical_name(right), re.UNICODE)[-1]
    if not family_variants(left_family).intersection(family_variants(right_family)): return False
    if not initial_word(aa[0]) and not initial_word(bb[0]):
        return aa[0].casefold() == bb[0].casefold()
    def initials(parts):
        return "".join(p.casefold() if initial_word(p) else p[0].casefold() for p in parts[:-1])
    a, b = initials(aa), initials(bb)
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def visible_authors(item):
    info = item.get("publication_info") or {}
    summary = (info.get("summary") or "").split(" - ")[0]
    cut = is_truncated(summary)
    result = []
    for entry in info.get("authors", []):
        cut = cut or is_truncated(entry.get("name", ""))
        name = re.sub(r"…|\.\.\.|\bet\s+al\.?", "", entry.get("name", ""), flags=re.I).strip(" ,;*")
        if words(name): result.append(dict(entry, name=canonical_name(name)))
    for entry in summary.split(","):
        name = re.sub(r"…|\.\.\.|\bet\s+al\.?", "", entry, flags=re.I).strip(" ,;*")
        if words(name) and norm(name) not in {norm(a["name"]) for a in result}:
            result.append({"name": name})
    return result, cut


def visible_covered(visible, authors):
    """Require an injective alignment: two visible authors need two byline slots."""
    options = [[i for i, a in enumerate(authors) if compatible_name(v["name"], a["name"])] for v in visible]
    if any(not values for values in options): return False
    occupied = {}
    def assign(slot, seen):
        for author in options[slot]:
            if author in seen: continue
            seen.add(author)
            if author not in occupied or assign(occupied[author], seen):
                occupied[author] = slot
                return True
        return False
    return all(assign(slot, set()) for slot in sorted(range(len(options)), key=lambda i: len(options[i])))


def preserve_scholar_ids(visible, authors):
    result = [dict(a, name=canonical_name(a["name"])) for a in authors]
    conflicts = []
    for v in visible:
        if not v.get("author_id"): continue
        exact = [a for a in result if norm(a["name"]) == norm(canonical_name(v["name"]))]
        matches = exact or [a for a in result if compatible_name(v["name"], a["name"])]
        if len(matches) == 1:
            existing = matches[0].get("author_id")
            if existing and existing != v["author_id"]:
                conflicts.append("Conflicting visible Scholar IDs for " + matches[0]["name"])
            else:
                matches[0]["author_id"] = v["author_id"]
    return result, conflicts


def doi_from_url(value):
    value = urllib.parse.unquote(value or "")
    if urllib.parse.urlsplit(value).netloc == "direct.mit.edu" and "/article-pdf/doi/" in value:
        value = value.split("/article-pdf/doi/", 1)[1]
        match = re.match(r"(10\.\d{4,9}/[^/]+)(?:/|$)", value, re.I)
        if match: return match.group(1).casefold()
    match = re.search(r"10\.\d{4,9}/[^?#\s]+", value, re.I)
    if not match: return None
    doi = re.sub(r"/(?:abstract|full|pdf)$", "", match.group(0).rstrip("/"), flags=re.I).casefold()
    if "/content/pdf/" in value.casefold() and doi.endswith(".pdf"): doi = doi[:-4]
    return doi


def arxiv_url(value):
    if urllib.parse.urlsplit(value or "").hostname not in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"): return None
    match = re.search(r"/(?:abs|pdf|html)/((?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?)", value, re.I)
    return "https://arxiv.org/abs/" + match.group(1) if match else None


def record_key(url):
    doi = doi_from_url(url)
    if doi: return "doi:" + doi
    arxiv = arxiv_url(url)
    if arxiv: return arxiv
    parsed = urllib.parse.urlsplit(url or "")
    query = urllib.parse.parse_qs(parsed.query)
    if parsed.hostname and (parsed.hostname == "ieee.org" or parsed.hostname.endswith(".ieee.org")):
        ieee = re.search(r"/document/(\d+)", parsed.path)
        identifier = ieee.group(1) if ieee else (query.get("arnumber") or [""])[0]
        if identifier.isdigit(): return "ieee:" + identifier
    if parsed.hostname in ("openreview.net", "www.openreview.net"):
        identifier = (query.get("id") or query.get("noteId") or [""])[0]
        if identifier: return "openreview:" + identifier
    if parsed.hostname in ('www.sciencedirect.com', 'sciencedirect.com', 'api.elsevier.com'):
        pii = re.search(r'/(?:pii/|PII:)([A-Z0-9]+)', urllib.parse.unquote(parsed.path), re.I)
        if pii: return 'pii:' + pii.group(1).upper()
    if parsed.hostname in ("researchgate.net", "www.researchgate.net"):
        publication = re.search(r"/publication/(\d+)", parsed.path)
        if publication: return "researchgate:" + publication.group(1)
    return parsed.netloc.casefold().removeprefix("www.") + parsed.path.rstrip("/")


def publisher_record_agrees(item, candidate):
    key = record_key(item.get("link", ""))
    links = [candidate.get("source", "")] + candidate.get("record_links", [])
    return key.startswith(("ieee:", "pii:")) and key in {record_key(u) for u in links}


def ieee_metadata(url, body):
    """Read IEEE's embedded JSON as data, never execute the page's JavaScript."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if not (host == "ieeexplore.ieee.org" or host.endswith(".ieee.org")): return None
    match = re.search(r"\bxplGlobal\.document\.metadata\s*=\s*", body)
    if not match: return None
    try: work, _ = json.JSONDecoder().raw_decode(body[match.end():].lstrip())
    except (ValueError, TypeError): return None
    if record_key(url) != "ieee:" + str(work.get("articleId", "")): return None
    authors = [{"name": canonical_name(a["name"])} for a in work.get("authors", []) if isinstance(a, dict) and a.get("name")]
    return {"provider": "ieee_embedded_json", "source": url, "title": work.get("title", ""),
            "doi": str(work.get("doi", "")).casefold() or None, "authors": authors, "ieee_article_id": str(work["articleId"])}


def openreview_metadata(payload, note_id, retrieval_url):
    """A search result must contain the exact, case-sensitive original note ID."""
    unwrap = lambda value: value.get("value") if isinstance(value, dict) else value
    for note in payload.get("notes", []):
        if note.get("id") != note_id: continue
        content = note.get("content", {})
        names = unwrap(content.get("authors", [])) or []
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names): continue
        return {"provider": "openreview_search" if "/notes/search?" in retrieval_url else "openreview",
                "source": "https://openreview.net/forum?id=" + note_id, "retrieval_url": retrieval_url,
                "title": unwrap(content.get("title", "")), "authors": [{"name": canonical_name(a)} for a in names],
                "openreview_note_id": note_id, "authorids": unwrap(content.get("authorids", [])),
                "venue": unwrap(content.get("venue", ""))}
    return None


def openreview_search_urls(title):
    # A full title can be a poor search query for an older API. Try a bounded
    # distinctive suffix as a second route; never accept a result by ranking.
    terms = list(dict.fromkeys([title, " ".join(title.split()[-3:])]))
    return [api + urllib.parse.urlencode({"term": term, "content": "title", "source": "forum", "limit": limit})
            for term in terms for api in ("https://api2.openreview.net/notes/search?", "https://api.openreview.net/notes/search?") for limit in (20, 100)]


class Metadata(HTMLParser):
    def __init__(self): super().__init__(); self.values = defaultdict(list)
    def handle_starttag(self, tag, attrs):
        if tag != "meta": return
        attrs = dict(attrs)
        name = (attrs.get("name") or attrs.get("property") or "").casefold()
        if attrs.get("content"): self.values[name].append(attrs["content"])


def browser_challenge(body):
    beginning = html.unescape(body[:4000]).casefold()
    return any(value in beginning for value in ("<title>verifying your browser", "<title>just a moment", "<title>attention required! | cloudflare", "<title>making sure you're not a bot", "<title>oh noes!"))


def evidence_body(entry):
    path = Path(entry.get("path", ""))
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
        raise ValueError("Identity bridge evidence hash mismatch")
    return path.read_text(encoding="utf-8", errors="replace")


def corrected_visible(item, candidate, visible):
    """Apply only explicit, reviewed, hash-bound corrections of non-author roles."""
    names = candidate.get("scholar_name_corrections")
    if names:
        if candidate.get("provider") != "retained_text_byline" or not candidate.get("byline_verified") or not names.get("verified") or not names.get("review_note"):
            raise ValueError("Name annotation correction requires reviewed retained text")
        if record_key(item.get("link", "")) != record_key(candidate.get("source", "")) or norm(item["title"]) != norm(candidate.get("title", "")):
            raise ValueError("Name annotation correction must identify the exact original record")
        body = re.sub(r"\s+", " ", evidence_body(candidate["evidence"]))
        visible = [dict(v) for v in visible]
        for entry in names.get("entries", []):
            raw, full, annotation = entry["scholar_name"], entry["full_author_name"], entry["affiliation_marker"]
            if not re.fullmatch(r"[a-z]", annotation) or not raw.endswith(annotation): raise ValueError("Unsupported attached affiliation marker")
            if full + " " + annotation not in body or not any(norm(a["name"]) == norm(full) for a in candidate.get("authors", [])):
                raise ValueError("Corrected author and affiliation marker missing from primary byline")
            if not compatible_name(raw[:-1], full): raise ValueError("Correction changes more than the attached marker")
            matches = [v for v in visible if norm(v["name"]) == norm(raw)]
            if len(matches) != 1: raise ValueError("Correction does not uniquely identify a visible name")
            matches[0]["name"] = full
    correction = candidate.get("scholar_byline_correction")
    if not correction: return visible
    if candidate.get("provider") not in ("retained_pdf_byline", "retained_text_byline") or not candidate.get("byline_verified"):
        raise ValueError("Byline correction requires a checked original document")
    if not correction.get("verified") or not correction.get("review_note") or candidate.get("source") != item.get("link") or norm(candidate.get("title")) != norm(item["title"]):
        raise ValueError("Byline correction lacks exact original record and reviewed title agreement")
    body = Path(candidate["evidence_text_path"]).read_text(encoding="utf-8") if candidate["provider"] == "retained_pdf_byline" else evidence_body(candidate["evidence"])
    exclusions = correction.get("excluded_visible_authors", [])
    removed = set()
    for entry in exclusions:
        if entry.get("role") not in ("affiliation", "advisory_committee"): raise ValueError("Unsupported non-author role")
        name, context, literal = entry["scholar_name"], entry["context_fragment"], entry["primary_role_text"]
        if not literal or context not in body or literal not in context: raise ValueError("Non-author role context is not in the retained document")
        if not any(norm(v["name"]) == norm(name) for v in visible): raise ValueError("Excluded Scholar author is absent from this record")
        if entry["role"] == "advisory_committee" and ("Advisory Committee:" not in context or not compatible_name(name, literal)):
            raise ValueError("Committee exclusion lacks matching name and explicit committee heading")
        if entry["role"] == "affiliation" and (not suspicious_author_name(literal) or norm(literal) not in norm(name)):
            raise ValueError("Affiliation exclusion lacks an explicit organization fragment")
        if any(compatible_name(name, a["name"]) for a in candidate.get("authors", [])): raise ValueError("Cannot exclude a name that matches the actual byline")
        removed.add(norm(name))
    if not removed: raise ValueError("Empty byline correction")
    return [v for v in visible if norm(v["name"]) not in removed]


def identity_bridge_kind(item, candidate):
    """Validate concrete record links/container evidence, never a fuzzy title score."""
    bridge = candidate.get("identity_bridge") or {}
    if not isinstance(bridge, dict): return None
    if not bridge.get("verified") or bridge.get("original_url") != (item.get("link") or ""): return None
    if record_key(bridge.get("candidate_url", "")) != record_key(candidate.get("source", "")): return None
    try:
        if bridge.get("kind") == "single_publication_links":
            body = evidence_body(bridge["evidence"])
            fragment = bridge["fragment"]
            if fragment not in body or norm(candidate.get("title", "")) not in norm(fragment): return None
            links = [record_key(html.unescape(u)) for u in re.findall(r'href=[\"\']([^\"\']+)[\"\']', fragment)]
            if record_key(item.get("link", "")) in links and record_key(candidate.get("source", "")) in links: return "explicit_primary_publication_links"
        if bridge.get("kind") == "container_chapter":
            book = json.loads(evidence_body(bridge["book_evidence"]))["message"]
            chapter = json.loads(evidence_body(bridge["chapter_evidence"]))["message"]
            if book.get("type") not in ("book", "proceedings") or chapter.get("type") != "book-chapter": return None
            if doi_from_url(item.get("link")) != book.get("DOI", "").casefold() or candidate.get("doi") != chapter.get("DOI", "").casefold(): return None
            if norm(candidate.get("title", "")) != norm(next(iter(chapter.get("title", [])), "")): return None
            if set(book.get("ISBN", [])).intersection(chapter.get("ISBN", [])) and norm(item["title"]) == norm(candidate.get("title", "")): return "explicit_book_chapter_relation"
        if bridge.get("kind") == "google_book_chapter":
            body = evidence_body(bridge["book_evidence"])
            chapter = json.loads(evidence_body(bridge["chapter_evidence"]))["message"]
            parsed = urllib.parse.urlsplit(item.get("link", "")); identifier = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
            if parsed.netloc != "books.google.com" or not identifier or "id=" + identifier not in body: return None
            if chapter.get("type") != "book-chapter" or candidate.get("doi") != chapter.get("DOI", "").casefold(): return None
            if norm(candidate.get("title", "")) != norm(next(iter(chapter.get("title", [])), "")): return None
            same_book = any(re.sub(r"[^0-9X]", "", isbn) in body for isbn in chapter.get("ISBN", []))
            if same_book and len(norm(item["title"])) >= 15 and norm(candidate.get("title", "")).startswith(norm(item["title"])): return "book_isbn_and_chapter_title_prefix"
    except (ValueError, KeyError, TypeError): return None
    return None


def cached_record_identity(url):
    """Only bibliographic identifiers with a provider-specific identity rule."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname == 'api.crossref.org' and parsed.path.startswith('/works/') and not parsed.query:
        # DOI names are case-insensitive; retain percent decoding of the ID.
        return 'crossref:' + urllib.parse.unquote(parsed.path[len('/works/'):]).translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))
    abstract = arxiv_url(url)
    if abstract and parsed.path.startswith('/abs/') and not parsed.query:
        # Reuse a retained page for the same explicit article/version, including
        # a successful bare-? route. This is not general HTTP URL equivalence.
        return 'arxiv:' + abstract
    return None


class HTTPStore:
    def __init__(self, directory, seed=None, failure_ttl=900, offline=False, retry_unavailable=False):
        self.directory = Path(directory)
        seeds = seed if isinstance(seed, (list, tuple)) else [seed] if seed else []
        self.seeds = [Path(s) for s in seeds]
        self.failure_ttl = failure_ttl
        self.offline = offline
        self.retry_unavailable = retry_unavailable
        self.directory.mkdir(parents=True, exist_ok=True)
        self.locks = defaultdict(threading.Lock)
        self.host_limits = defaultdict(lambda: threading.Semaphore(3))
        self.attempts = 0
        self._counter_lock = threading.Lock()
        self.record_aliases = defaultdict(list)
        for directory in [self.directory] + self.seeds:
            for path in directory.glob('*.json'):
                entry = json.loads(path.read_text())
                identity = cached_record_identity(entry.get('url', ''))
                body = directory / entry.get('body_file', path.with_suffix('.body').name)
                if identity and entry.get('ok') and body.is_file():
                    self.record_aliases[identity].append((path, body))

    def log_access(self, **event):
        if "url" in event: event["url"] = safe_url(event["url"])
        event["at_utc"] = datetime.now(timezone.utc).isoformat()
        with self._counter_lock:
            with (self.directory / "cache-access.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def get(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        with self.locks[key]: return self._get(url, key)

    def _get(self, url, key):
        unavailable = []
        for directory in [self.directory] + self.seeds:
            if not directory: continue
            record_path, body_path = directory / (key + ".json"), directory / (key + ".body")
            if record_path.exists():
                entry = json.loads(record_path.read_text())
                if entry.get("ok") and body_path.exists():
                    body = body_path.read_bytes()
                    if hashlib.sha256(body).hexdigest() != entry.get("body_sha256"):
                        raise ValueError("Cached metadata body hash mismatch")
                    decoded = body.decode("utf-8", errors="replace")
                    if not browser_challenge(decoded):
                        self.log_access(url=url, outcome="cached_success", cache=str(record_path))
                        return decoded
                    self.log_access(url=url, outcome="cached_browser_challenge", cache=str(record_path))
                    unavailable.append((record_path, True))
                    continue
                permanent = entry.get("status") in (401, 403, 404, 410) or entry.get("PDF_not_retained") or entry.get("binary_PDF_not_retained") or entry.get("browser_verification_required")
                try:
                    ended = datetime.fromisoformat(entry.get("ended_at_utc", ""))
                    recent = (datetime.now(timezone.utc) - ended).total_seconds() < self.failure_ttl
                except (ValueError, TypeError): recent = False
                if permanent or recent:
                    unavailable.append((record_path, bool(permanent)))
        for record_path, body_path in self.record_aliases.get(cached_record_identity(url), []):
            entry = json.loads(record_path.read_text())
            body = body_path.read_bytes()
            if hashlib.sha256(body).hexdigest() != entry.get('body_sha256'):
                raise ValueError('Cached metadata body hash mismatch')
            decoded = body.decode('utf-8', 'replace')
            if not body.startswith(b'%PDF') and not browser_challenge(decoded):
                self.log_access(url=url, outcome='cached_same_provider_record', cache=str(record_path))
                return decoded
        # An older failure must not hide a later successful retained response.
        if unavailable and not self.retry_unavailable:
            record_path, permanent = unavailable[0]
            self.log_access(url=url, outcome="cached_unavailable", cache=str(record_path), permanent=permanent)
            raise RuntimeError("Cached unavailable metadata response")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("https", "http"): raise ValueError("Unsupported metadata URL")
        if self.offline:
            self.log_access(url=url, outcome="offline_unavailable")
            raise RuntimeError("No usable retained metadata response; offline mode forbids acquisition")
        entry = {"url": safe_url(url), "started_at_utc": datetime.now(timezone.utc).isoformat()}
        try:
            with self.host_limits[parsed.netloc]:
                with self._counter_lock: self.attempts += 1
                request_url = url
                api_key = os.environ.get('OPENALEX_API_KEY') if parsed.hostname == 'api.openalex.org' else None
                if api_key:
                    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
                    query = [(k, v) for k, v in query if k != 'api_key'] + [('api_key', api_key)]
                    request_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment))
                decoded = metadata_get(request_url)
                if api_key:
                    decoded = decoded.replace(api_key, 'REDACTED')
            if decoded.lstrip().startswith("%PDF"):
                raise ValueError("PDF requires a separately retained and reviewed byline")
            if browser_challenge(decoded):
                raise ValueError("Browser verification page is not bibliographic metadata")
            body = decoded.encode("utf-8")
            (self.directory / (key + ".body")).write_bytes(body)
            entry.update(ok=True, status=200, body_sha256=hashlib.sha256(body).hexdigest(), encoding="utf-8")
            self.log_access(url=safe_url(url), outcome="network_success")
            return decoded
        except Exception as error:
            entry.update(ok=False, exception_type=type(error).__name__)
            if isinstance(error, urllib.error.HTTPError): entry["status"] = error.code
            self.log_access(url=safe_url(url), outcome="network_failure", exception_type=type(error).__name__)
            raise
        finally:
            entry["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
            (self.directory / (key + ".json")).write_text(json.dumps(entry, indent=2))

    def json(self, url): return json.loads(self.get(url))


class Resolver:
    def __init__(self, root, http, native_cache=None, discoveries=None, supplements=None, retry_unresolved=False, resolution_seed=None):
        self.root, self.http = Path(root), http
        self.native_cache = Path(native_cache) if native_cache else None
        self.discoveries, self.supplements = discoveries or {}, supplements or {}
        self.retry_unresolved = retry_unresolved
        self.resolution_seed = Path(resolution_seed) if resolution_seed else None
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate(item, candidate):
        if candidate.get("provider") == "retained_pdf_byline":
            for path_field, hash_field in (("evidence_pdf_path", "evidence_pdf_sha256"), ("evidence_text_path", "evidence_text_sha256")):
                evidence_path = Path(candidate.get(path_field, ""))
                if not candidate.get("byline_verified") or not evidence_path.is_file() or hashlib.sha256(evidence_path.read_bytes()).hexdigest() != candidate.get(hash_field):
                    return "Missing verified PDF/text evidence or source hash mismatch"
        if candidate.get("provider") == "retained_text_byline":
            if not candidate.get("byline_verified"): return "Text byline has not been checked"
            try: evidence_body(candidate["evidence"])
            except (ValueError, KeyError): return "Text byline evidence hash mismatch"
        if candidate.get("provider") == "retained_html_byline":
            if not candidate.get("byline_verified"): return "HTML byline has not been checked"
            try: evidence_body(candidate["evidence"])
            except (ValueError, KeyError): return "HTML byline evidence hash mismatch"
        visible, _ = visible_authors(item)
        try: visible = corrected_visible(item, candidate, visible)
        except (ValueError, KeyError, TypeError): return "Unsubstantiated correction of Scholar author roles"
        authors = candidate.get("authors", [])
        if not authors or candidate.get("truncated") or any(is_truncated(a.get("name", "")) for a in authors):
            return "Missing or truncated supplement authors"
        if any(not words(a.get("name", "")) for a in authors): return "Supplement contains an unnamed author"
        if any(suspicious_author_name(a.get("name", "")) for a in authors): return "Supplement contains an anonymous or organization author; membership unresolved"
        if len(authors) >= 100 and candidate.get("provider") == "openalex": return "OpenAlex 100-author completeness limit"
        if not visible_covered(visible, authors): return "Full metadata does not cover visible Scholar authors"
        same_title = norm(item["title"]) == norm(candidate.get("title", ""))
        explicit = doi_from_url(item.get("link"))
        bridge_kind = identity_bridge_kind(item, candidate)
        if explicit and candidate.get("doi") and explicit != candidate["doi"] and bridge_kind != "explicit_book_chapter_relation": return "Conflicting explicit DOI"
        identifier_agrees = bool(explicit and explicit == candidate.get("doi"))
        source_agrees = record_key(item.get("link", "")) == record_key(candidate.get("source", ""))
        arxiv_agrees = bool(arxiv_url(item.get("link")) and arxiv_url(item.get("link")) == arxiv_url(candidate.get("source")))
        ieee_agrees = publisher_record_agrees(item, candidate)
        openreview_agrees = bool(source_agrees and record_key(item.get("link", "")).startswith("openreview:") and candidate.get("openreview_note_id") == record_key(item["link"]).split(":", 1)[1])
        if record_key(item.get("link", "")).startswith("openreview:") and candidate.get("openreview_note_id") and record_key(item['link']) != 'openreview:' + candidate['openreview_note_id']:
            return 'Conflicting OpenReview note identifier'
        if candidate.get("openreview_note_id") and record_key(candidate.get("source", "")) != "openreview:" + candidate["openreview_note_id"]: return "Conflicting OpenReview note identifier"
        pdf_prefix = candidate.get("provider") == "retained_pdf_byline" and candidate.get("byline_verified") and source_agrees and len(norm(item["title"])) >= 45 and norm(candidate.get("title", "")).startswith(norm(item["title"]))
        if not same_title and not identifier_agrees and not arxiv_agrees and not ieee_agrees and not openreview_agrees and not pdf_prefix and not bridge_kind: return "Exact title or stable record identifier agreement missing"
        candidate["record_match"] = bridge_kind or ("explicit_doi" if identifier_agrees else ("explicit_ieee_record" if record_key(item.get("link", "")).startswith("ieee:") else "explicit_pii_record") if ieee_agrees else "explicit_openreview_record" if openreview_agrees else "explicit_arxiv_record" if arxiv_agrees else "direct_pdf_title_prefix" if pdf_prefix and not same_title else "exact_title_direct_link" if source_agrees else "exact_title_and_visible_authors")
        return None

    def resolve(self, item):
        primary_link = item.get("link") or ""
        result_id = item.get("result_id") or hashlib.sha256((item["title"] + primary_link).encode()).hexdigest()
        path = self.root / (re.sub(r"[^A-Za-z0-9_-]", "_", result_id) + ".json")
        discovery = self.discoveries.get(result_id, {})
        supplements = self.supplements.get(result_id, [])
        input_sha = hashlib.sha256(json.dumps({"item": item, "discovery": discovery, "supplements": supplements}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if path.exists():
            old_result = json.loads(path.read_text())
            unchanged = old_result.get("resolution_policy") == RESOLUTION_POLICY and old_result.get("input_sha256") == input_sha
            valid = not old_result.get("complete") or self.validate(item, dict(old_result)) is None
            if unchanged and valid and not (self.retry_unresolved and not old_result.get("complete")): return old_result
            history = self.root / "history"; history.mkdir(exist_ok=True)
            previous_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            (history / (path.stem + "-" + previous_sha + ".json")).write_bytes(path.read_bytes())
        rejected = []
        visible, cut = visible_authors(item)
        def accept(candidate):
            for bridge in discovery.get("record_bridges", []):
                if record_key(bridge.get("candidate_url", "")) == record_key(candidate.get("source", "")):
                    candidate["identity_bridge"] = bridge
            error = self.validate(item, candidate)
            if error:
                rejected.append({"provider": candidate.get("provider"), "source": candidate.get("source"), "reason": error})
                return None
            authors, conflicts = preserve_scholar_ids(corrected_visible(item, candidate, visible), candidate["authors"])
            if conflicts:
                rejected.append({"provider": candidate.get("provider"), "reason": "; ".join(conflicts)})
                return None
            return dict(candidate, authors=authors, complete=True, result_id=result_id,
                        rejected_candidates=rejected, resolved_at_utc=datetime.now(timezone.utc).isoformat())

        found = None
        for candidate in supplements:
            found = accept(dict(candidate))
            if found: break
        if self.resolution_seed and not found:
            seed_path = self.resolution_seed / path.name
            if seed_path.exists():
                candidate = json.loads(seed_path.read_text())
                if candidate.get("complete"):
                    candidate["reused_resolution_path"] = str(seed_path)
                    found = accept(candidate)
        if self.native_cache and not found:
            key = hashlib.sha256((norm(item["title"]) + "|" + primary_link).encode()).hexdigest()[:20]
            old_path = self.native_cache / "metadata" / (key + ".json")
            if old_path.exists():
                old = json.loads(old_path.read_text())
                if old.get("complete"):
                    found = accept({"provider": "native_metadata_revalidated", "source": old.get("source", ""),
                                    "title": old.get("matched_title", ""), "doi": old.get("doi"),
                                    "authors": [{"name": canonical_name(a)} for a in old.get("authors", [])]})
        urls = list(dict.fromkeys(u for u in discovery.get("candidate_urls", []) + [primary_link] + [r.get("link") for r in item.get("resources", []) if isinstance(r, dict)] if isinstance(u, str) and u))
        summary = (item.get("publication_info") or {}).get("summary", "")
        for identifier in re.findall(r"arxiv\s*:\s*((?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?)", summary, re.I):
            url = "https://arxiv.org/abs/" + identifier
            if url not in urls: urls.insert(0, url)
        dois = list(dict.fromkeys(filter(None, (doi_from_url(u) for u in urls))))
        if not found:
            pending, examined = list(urls[:8]), set()
            while pending and len(examined) < 12:
                url = pending.pop(0)
                if not url.startswith(("http://", "https://")): continue
                url = arxiv_url(url) or url
                if url in examined: continue
                examined.add(url)
                if re.search(r"\.pdf(?:[?#]|$)", url, re.I): continue
                try:
                    parsed = urllib.parse.urlsplit(url)
                    if parsed.netloc in ("dblp.org", "dblp.uni-trier.de", "dblp1.uni-trier.de") and parsed.path.endswith(".xml"):
                        tree = ET.fromstring(self.http.get(url))
                        work = next(iter(tree), tree)
                        title_node = work.find("title")
                        candidate = {"provider": "dblp_xml", "source": url,
                                     "title": "".join(title_node.itertext()) if title_node is not None else "",
                                     "authors": [{"name": canonical_name(a.text or ""), "dblp_pid": a.get("pid")} for a in work.findall("author")],
                                     "record_links": [a.text for a in work.findall("ee")]}
                        found = accept(candidate)
                        if found: break
                        continue
                    if parsed.hostname in ("openreview.net", "www.openreview.net"):
                        query = urllib.parse.parse_qs(parsed.query)
                        note_id = (query.get("id") or query.get("noteId") or [""])[0]
                        for api in ("https://api2.openreview.net/notes?id=", "https://api.openreview.net/notes?id="):
                            try:
                                retrieval = api + urllib.parse.quote(note_id)
                                candidate = openreview_metadata(self.http.json(retrieval), note_id, retrieval)
                                if not candidate: continue
                                found = accept(candidate)
                                if found: break
                            except Exception as error:
                                rejected.append({"provider": "openreview_direct", "source": api + urllib.parse.quote(note_id), "stage": "retrieval", "reason": "Direct lookup unavailable: " + type(error).__name__})
                        if not found:
                            for retrieval in openreview_search_urls(item["title"]):
                                try:
                                    payload = self.http.json(retrieval)
                                    candidate = openreview_metadata(payload, note_id, retrieval)
                                    if candidate:
                                        found = accept(candidate)
                                        if found: break
                                    else:
                                        rejected.append({"provider": "openreview_search", "source": retrieval, "stage": "discovery", "reason": "Returned results do not contain the exact original note ID", "returned_notes": len(payload.get("notes", []))})
                                except Exception as error:
                                    rejected.append({"provider": "openreview_search", "source": retrieval, "stage": "retrieval", "reason": "Search lookup unavailable: " + type(error).__name__})
                        if found: break
                    # Known blocked publisher pages can still yield metadata via DOI.
                    body = self.http.get(url)
                    embedded = ieee_metadata(url, body)
                    if record_key(url).startswith('ieee:') and 'xplGlobal.document.metadata' in body and not embedded:
                        raise ValueError('IEEE body does not identify the requested article')
                    if embedded:
                        found = accept(embedded)
                        if found: break
                    parser = Metadata(); parser.feed(body)
                    values = parser.values
                    names = values.get("citation_author") or values.get("dc.creator") or values.get("dc.creator.personalname") or []
                    title = next(iter(values.get("citation_title") or values.get("dc.title") or []), "")
                    if arxiv_url(url) and norm(title) != norm(item["title"]):
                        versions = list(dict.fromkeys("https://arxiv.org" + v for v in re.findall(r'href=[\"\'](/abs/[^\"\']+v\d+)[\"\']', body)))
                        pending.extend(v for v in versions[:5] if v not in examined and v not in pending)
                    for value in values.get("citation_doi", []):
                        doi = doi_from_url(value)
                        if doi and doi not in dois: dois.append(doi)
                    if names:
                        found = accept({"provider": "primary_html", "source": url, "title": title,
                                        "authors": [{"name": canonical_name(a)} for a in names]})
                    else:
                        rejected.append({"provider": "primary_html", "source": url, "stage": "parsing", "reason": "No author byline extracted by the supported page parsers; this does not establish that the page lacks authors"})
                    if found: break
                except Exception as error:
                    rejected.append({"provider": "primary_retrieval", "source": url, "reason": "Retrieval or parsing unavailable: " + type(error).__name__})

        if not found:
            candidates = []
            for doi in dois[:2]:
                try: candidates.append(self.http.json("https://api.crossref.org/works/" + urllib.parse.quote(doi, safe=""))["message"])
                except Exception: pass
            if not candidates:
                try:
                    url = "https://api.crossref.org/works?" + urllib.parse.urlencode({"query.title": item["title"], "rows": 3})
                    candidates = self.http.json(url).get("message", {}).get("items", [])
                except Exception: pass
            matching = []
            for candidate in candidates:
                links = [x["URL"] for x in candidate.get("link", []) if x.get("URL")]
                evidence = {"source": "https://doi.org/" + candidate.get("DOI", ""), "record_links": links}
                if norm(next(iter(candidate.get("title", [])), "")) == norm(item["title"]) or candidate.get("DOI", "").casefold() in dois or publisher_record_agrees(item, evidence):
                    matching.append(candidate)
                else:
                    rejected.append({"provider": "crossref", "source": evidence["source"], "stage": "matching", "candidate_title": next(iter(candidate.get("title", [])), ""), "reason": "Candidate title differs and no matching explicit publisher identifier was found"})
            # Reject versions that cannot account for the actual visible byline
            # before deciding whether an exact-title collision is ambiguous.
            covered = [c for c in matching if visible_covered(visible, [{"name": " ".join(filter(None, [a.get("given"), a.get("family")]))} for a in c.get("author", [])])]
            if covered: matching = covered
            anchored = [c for c in matching if publisher_record_agrees(item, {
                'source': 'https://doi.org/' + c.get('DOI', ''),
                'record_links': [link['URL'] for link in c.get('link', []) if link.get('URL')]})]
            if anchored: matching = anchored
            # An exact-title collision with differing authors is not resolved by ranking.
            signatures = {tuple(norm(a.get("given", "") + " " + a.get("family", "")) for a in c.get("author", [])) for c in matching}
            if len(signatures) > 1 and not dois:
                rejected.append({"provider": "crossref", "reason": "Multiple exact-title works with different author lists"})
            else:
                for candidate in matching:
                    authors = [{"name": canonical_name(" ".join(filter(None, [a.get("given"), a.get("family")]))),
                                **({"orcid": a["ORCID"]} if a.get("ORCID") else {})} for a in candidate.get("author", [])]
                    found = accept({"provider": "crossref", "source": "https://doi.org/" + candidate["DOI"],
                                    "title": next(iter(candidate.get("title", [])), ""), "doi": candidate["DOI"].casefold(), "authors": authors,
                                    "record_links": [x["URL"] for x in candidate.get("link", []) if x.get("URL")]})
                    if found: break

        if not found:
            try:
                url = "https://api.openalex.org/works?" + urllib.parse.urlencode({"search": item["title"], "per-page": 3})
                matches = [c for c in self.http.json(url).get("results", []) if norm(c.get("title", "")) == norm(item["title"])]
                if len(matches) > 1:
                    exact = [c for c in matches if doi_from_url(c.get("doi")) in dois]
                    covered = [c for c in matches if visible_covered(visible, [{"name": canonical_name(a.get("raw_author_name") or (a.get("author") or {}).get("display_name", ""))} for a in c.get("authorships", [])])]
                    matches = exact if exact else covered if len(covered) == 1 else []
                    if not matches: rejected.append({"provider": "openalex", "reason": "Multiple exact-title works; no explicit matching DOI"})
                for candidate in matches:
                    # Fetch the individual record, but still guard the current cap of 100.
                    work = self.http.json(candidate["id"].replace("https://openalex.org/", "https://api.openalex.org/works/"))
                    authors = []
                    for entry in work.get("authorships", []):
                        author = entry.get("author") or {}
                        authors.append({"name": canonical_name(entry.get("raw_author_name") or author.get("display_name", "")),
                                        "openalex_id": author.get("id"), "orcid": entry.get("raw_orcid") or author.get("orcid")})
                    found = accept({"provider": "openalex", "source": work["id"], "title": work.get("title", ""),
                                    "doi": doi_from_url(work.get("doi")), "authors": authors,
                                    "truncated": work.get("is_authors_truncated", False)})
                    if found: break
            except Exception: pass

        if not found:
            found = {"result_id": result_id, "complete": False, "authors": visible, "provider": "unresolved_metadata",
                     "source": item.get("link", ""), "title": item["title"], "scholar_truncated": cut,
                     "rejected_candidates": rejected, "resolved_at_utc": datetime.now(timezone.utc).isoformat()}
        found.update({"resolution_policy": RESOLUTION_POLICY, "input_sha256": input_sha})
        found["resolution_state"] = "matched_full_byline" if found["complete"] else "lookup_incomplete_using_attempted_routes"
        path.write_text(json.dumps(found, ensure_ascii=False, indent=2) + "\n")
        return found


def classify_pair(originals, citing, source_ids, incomplete=False):
    possible = []
    for original in originals:
        original = canonical_name(original)
        source_id = source_ids.get(norm(original))
        for author in citing:
            citing_id = author.get("author_id")
            if source_id and citing_id:
                if source_id == citing_id: return "non_independent", original, "Shared Scholar author ID"
                continue
            if norm(original) == norm(canonical_name(author["name"])):
                return "non_independent", original, "Shared full normalized author name"
            if compatible_name(original, author["name"]): possible.append(original + " / " + author["name"])
    if incomplete: possible.append("Complete citing author byline not obtained through the attempted routes")
    if possible: return "unresolved", "", "; ".join(dict.fromkeys(possible))
    return "independent", "", "No shared author in matched full metadata"
