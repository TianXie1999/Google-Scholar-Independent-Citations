"""Full author audit: resolve citing and cited bylines and classify each edge.

The legacy Scholar-convention report remains the default. This optional mode
compares full names, treats compatible initials as unresolved, and never infers
person identities from affiliation, topic or title similarity.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
from audit_resolver import (HTTPStore, Resolver, canonical_name, classify_pair,
    compatible_name, corrected_visible, initial_word, is_truncated, norm,
    preserve_scholar_ids, suspicious_author_name, visible_authors, words)

def known_ids(data, profile, identities=None):
    candidates = {}
    for author in profile.get("co_authors", []):
        if author.get("name") and author.get("author_id"):
            candidates.setdefault(norm(canonical_name(author["name"])), set()).add(author["author_id"])
    mapping = {key: next(iter(ids)) for key, ids in candidates.items() if len(ids) == 1}
    for identity in identities or []:
        evidence = Path(identity.get("evidence_body_path", ""))
        if not identity.get("verified") or not evidence.is_file() or hashlib.sha256(evidence.read_bytes()).hexdigest() != identity.get("evidence_body_sha256"):
            raise ValueError("Source author identity requires verified hash-bound primary evidence")
        key, identifier = norm(canonical_name(identity["name"])), identity["author_id"]
        body = html.unescape(evidence.read_text())
        if "user=" + identifier not in body or key not in norm(body): raise ValueError("Primary identity evidence does not contain the full name and Scholar ID")
        if key in mapping and mapping[key] != identifier: raise ValueError("Conflicting source author identity IDs")
        if key == norm(canonical_name(data["author"]["name"])) and identifier != data["author_id"]: raise ValueError("Owner identity cannot be replaced")
        mapping[key] = identifier
    mapping[norm(canonical_name(data["author"]["name"]))] = data["author_id"]
    return mapping



def source_item(source, ids):
    names = [canonical_name(n.strip(" *")) for n in source["detail"]["authors"].split(",") if n.strip(" *")]
    return {"title": source["paper"]["title"], "result_id": "source_" + hashlib.sha256(source["paper"]["citation_id"].encode()).hexdigest()[:20],
            "link": source["detail"].get("link"), "resources": source["detail"].get("resources", []),
            "publication_info": {"authors": [{"name": n, **({"author_id":ids[norm(n)]} if ids.get(norm(n)) else {})} for n in names]}}



def analyze(data, profile, resolutions, source_resolutions=None, identities=None):
    ids = known_ids(data, profile, identities)
    rows, papers = [], []
    for source in data["papers"]:
        originals = [canonical_name(a.strip(" *")) for a in source["detail"]["authors"].split(",") if a.strip(" *")]
        source_evidence = dict((source_resolutions or {}).get(source["paper"]["citation_id"], {}))
        if source_evidence.get("complete"):
            error = Resolver.validate(source_item(source, ids), source_evidence)
            if error:
                source_evidence["complete"] = False
                source_evidence["rejected_candidates"] = source_evidence.get("rejected_candidates", []) + [{"provider":"source_revalidation", "reason":error}]
        source_ids = dict(ids)
        original_scholar_authors = "; ".join(originals)
        if source_evidence.get("complete"):
            enriched = []
            for author in source_evidence["authors"]:
                name = canonical_name(author["name"])
                aligned = [n for n in originals if compatible_name(n, name)]
                if len(aligned) == 1 and initial_word(words(name)[0]) and not initial_word(words(aligned[0])[0]): name = aligned[0]
                enriched.append(name)
                if author.get("author_id"): source_ids[norm(name)] = author["author_id"]
            originals = enriched
        local = []
        source_cut = not originals or (not source_evidence.get("complete") and is_truncated(source["detail"]["authors"]))
        for item in source["citing"]:
            visible, cut = visible_authors(item)
            ev = dict(resolutions.get(item.get("result_id"), {}))
            evidence_conflict = False
            if ev.get("complete"):
                # Reusing a metadata fetch never bypasses validation of this edge.
                error = Resolver.validate(item, ev)
                effective_visible = corrected_visible(item, ev, visible) if not error else visible
                authors, conflicts = preserve_scholar_ids(effective_visible, ev["authors"])
                if error or conflicts:
                    evidence_conflict = True
                    ev["complete"] = False
                    ev["rejected_candidates"] = ev.get("rejected_candidates", []) + [{"reason": error or "; ".join(conflicts), "provider": "per_relationship_validation"}]
                else: ev["authors"] = authors
            authors = ev.get("authors") if ev.get("complete") else visible
            suspect_visible = any(suspicious_author_name(a["name"]) for a in visible)
            incomplete = source_cut or evidence_conflict or (not ev.get("complete") and (cut or not visible or suspect_visible))
            status, overlap, basis = classify_pair(originals, authors, source_ids, incomplete)
            if status == "independent" and not ev.get("complete"): basis = "No shared author or ambiguous match in untruncated Scholar list; metadata unconfirmed"
            row = {"source_citation_id": source["paper"]["citation_id"], "source_title": source["paper"]["title"],
                   "source_authors": "; ".join(originals), "source_scholar_url": source["paper"]["link"],
                   "source_scholar_authors_original": original_scholar_authors,
                   "source_author_evidence_provider": source_evidence.get("provider", "scholar_detail"),
                   "source_author_evidence_url": source_evidence.get("source", source["paper"]["link"]),
                   "source_author_metadata_complete": bool(source_evidence.get("complete")),
                   "source_metadata_rejections": json.dumps(source_evidence.get("rejected_candidates", []), ensure_ascii=False),
                   "source_known_author_ids": json.dumps({n: source_ids[norm(n)] for n in originals if norm(n) in source_ids}, ensure_ascii=False),
                   "citing_title": item["title"], "citing_authors": "; ".join(a["name"] for a in authors),
                   "citing_author_identities": json.dumps(authors, ensure_ascii=False), "citing_url": item.get("link", ""),
                   "scholar_result_id": item.get("result_id", ""), "status": status,
                   "overlapping_author": overlap, "classification_basis": basis,
                   "author_evidence_level": "matched_full_metadata" if ev.get("complete") else "scholar_visible_only",
                   "author_evidence_provider": ev.get("provider", ""), "author_evidence_url": ev.get("source", ""),
                   "author_evidence_retrieval_url": ev.get("retrieval_url", ev.get("source", "")),
                   "metadata_resolution_state": ev.get("resolution_state", "retained_baseline_full_byline" if ev.get("complete") else "lookup_incomplete_using_attempted_routes"),
                   "metadata_record_match": ev.get("record_match", ""), "metadata_title": ev.get("title", ""),
                   "scholar_byline_correction": json.dumps(ev.get("scholar_byline_correction"), ensure_ascii=False) if ev.get("complete") and ev.get("scholar_byline_correction") else "",
                   "scholar_name_corrections": json.dumps(ev.get("scholar_name_corrections"), ensure_ascii=False) if ev.get("complete") and ev.get("scholar_name_corrections") else "",
                   "original_scholar_summary": (item.get("publication_info") or {}).get("summary", ""),
                   "original_scholar_truncated": cut, "metadata_rejections": json.dumps(ev.get("rejected_candidates", []), ensure_ascii=False)}
            local.append(row); rows.append(row)
        counts = Counter(r["status"] for r in local)
        papers.append({"source_citation_id": source["paper"]["citation_id"], "title": source["paper"]["title"],
                       "scholar_year": source["paper"].get("year"), "displayed": source["displayed"], "retrieved": len(local),
                       **{key: counts[key] for key in ("independent", "non_independent", "unresolved")}, "end_reason": source["end_reason"]})
    total = next((entry["citations"].get("all") for entry in profile.get("cited_by", {}).get("table", []) if "citations" in entry), None)
    counts = Counter(r["status"] for r in rows)
    evidence = Counter((r["status"], r["author_evidence_level"]) for r in rows)
    report = {"author": data["author"]["name"], "author_id": data["author_id"], "scholar_snapshot_date": data["snapshot_date"],
              "analysis_date_utc": datetime.now(timezone.utc).isoformat(), "profile_total": total, "retrieved": len(rows),
              "classification": {key: counts[key] for key in ("independent", "non_independent", "unresolved")},
              "metadata_evidence": [{"status": status, "level": level, "relationships": count} for (status, level), count in evidence.items()],
              "papers": papers, "citation_edges": rows,
              "complete_retrieval_counts": bool(papers or total == 0) and total == len(rows) == sum(p["displayed"] for p in papers) and all(p["displayed"] == p["retrieved"] and p["end_reason"] not in ("scholar_1000_limit", "repeated_page") for p in papers),
              "count_unit": "Each (cited source paper, citing Scholar result) relationship, without deduplication across source papers",
              "identity_policy": "Trusted Scholar IDs first, normalized full names next; compatible initials remain unresolved. Full author metadata requires exact title, explicit DOI/arXiv/IEEE/OpenReview record or a reviewed hash-bound record bridge, and distinct coverage of every visible Scholar author. OpenReview search results must match the original case-sensitive note ID. Exceptions for misparsed affiliations/committee members require a checked original document and literal non-author-role evidence. No author IDs are inferred by title.",
              "classification_is_automated": True}
    report["verified_source_identities"] = identities or []
    assert sum(report["classification"].values()) == len(rows)
    assert all(sum(p[k] for k in ("independent", "non_independent", "unresolved")) == p["retrieved"] for p in papers)
    return report


def run_audit(data, profile, cache, offline=False, native_cache=None,
              http_caches=None, discoveries=None, supplements=None,
              identities=None, workers=4, retry_unresolved=False):
    """Acquire bylines for every distinct result; classify every original edge."""
    if not 1 <= workers <= 16:
        raise ValueError('Author-audit workers must be between 1 and 16')
    cache = Path(cache)
    store = HTTPStore(cache / 'http', http_caches, offline=offline, retry_unavailable=retry_unresolved)
    resolver = Resolver(cache / 'authors', store, native_cache, discoveries,
                        supplements, retry_unresolved)
    items = {}
    for paper in data['papers']:
        for item in paper['citing']:
            if not item.get('result_id'):
                raise ValueError('Full author audit requires original Scholar result IDs')
            items.setdefault(item['result_id'], item)
    resolutions = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolver.resolve, item): key for key, item in items.items()}
        for future in as_completed(futures):
            resolutions[futures[future]] = future.result()
    ids = known_ids(data, profile, identities)
    sources = {paper['paper']['citation_id']: resolver.resolve(source_item(paper, ids))
               for paper in data['papers']}
    (cache / 'source-authors.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2))
    result = analyze(data, profile, resolutions, sources, identities)
    result['new_metadata_lookups'] = store.attempts  # Logical lookups, excluding transport retries.
    result['snapshot_content_sha256'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result['profile_content_sha256'] = hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return native_report(result, data)


def native_report(result, data):
    """Keep the original HTML/CSV contract while retaining richer audit fields."""
    result.update(date=data['snapshot_date'], displayed=sum(p['displayed'] for p in result['papers']),
                  complete_snapshot=result['complete_retrieval_counts'],
                  unique_independent_by_title=len({norm(r['citing_title']) for r in result['citation_edges']
                                                   if r['status'] == 'independent'}),
                  policy=result['identity_policy'], analysis_mode='full_author_audit',
                  policy_description='Full author audit checks citing and cited bylines. Trusted Scholar IDs take precedence; full normalized names are compared next. Compatible initials remain unresolved. Reviewed evidence and record links are validated before use. Name matches still require identity review.')
    source_records = {p['paper']['citation_id']: p for p in data['papers']}
    for row in result['citation_edges']:
        paper = source_records[row['source_citation_id']]
        years = re.findall(r'\b(?:19|20)\d{2}\b', row['original_scholar_summary'])
        row.update(citing_year=years[-1] if years else '',
                   author_source=('Full author metadata' if row['author_evidence_level'] == 'matched_full_metadata'
                                  else 'Scholar author record'),
                   source_year=paper['paper'].get('year', ''),
                   source_article_url=paper['detail'].get('link') or paper['paper']['link'],
                   author_evidence_kind=row['author_evidence_provider'],
                   author_match_kind='', matched_citing_author='')
        if row['overlapping_author']:
            ids = {norm(k): v for k, v in json.loads(row['source_known_author_ids']).items()}
            for author in json.loads(row['citing_author_identities']):
                if row['classification_basis'] == 'Shared Scholar author ID':
                    match = author.get('author_id') == ids.get(norm(row['overlapping_author']))
                    kind = 'scholar_id'
                else:
                    match = norm(author['name']) == norm(row['overlapping_author'])
                    kind = 'full_name'
                if match:
                    row.update(author_match_kind=kind, matched_citing_author=author['name'])
                    break
    return result
