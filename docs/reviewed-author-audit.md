# Full author audit and reviewed replay

The recommended workflow checks cited and citing bylines, flags unresolved
initials, and retains explicit review decisions. A historical replay additionally
freezes which author evidence was used. A fresh audit can discover newer or
stronger evidence and may legitimately differ from an earlier replay.

## Prerequisites

Use Python 3.9+ with working TLS trust. The scripts use the standard library.
New Scholar collection needs `SERPAPI_API_KEY` in the process environment and
sufficient account quota. Public metadata requests do not use SerpApi collection
credits. OpenAlex is an optional fallback with its own service allowance; set
`OPENALEX_API_KEY` in the process environment if you have a key. It is attached
only to OpenAlex live requests and is not retained in caches or reports.
Historical replay requires retained evidence files and makes no network
calls; it requires no API key or browser login.

Store profile snapshots, author evidence and review decisions outside the public
repository. A bundle can contain personal research data and local source paths.
No user's profile, evidence packet or name-specific identity rule is bundled
with the software.

## Fresh audit

```sh
python3 skills/scholar-independent-citations/scripts/report.py \
  --scholar-url 'https://scholar.google.com/citations?user=AUTHOR_ID' \
  --full-author-audit --output-dir '../scholar-audits/new-audit' \
  --export-evidence-bundle '../scholar-private/reviewed-evidence.json'
```

This inspects every distinct citing record and each cited source byline, then
classifies every original source/result relationship separately. Reusing a
metadata fetch never merges result IDs, separately indexed versions or sources.
Full names and trusted Scholar IDs drive classification; initials compatibility
does not prove identity. Exact same-name exclusions still receive warnings.
The original Scholar-convention CLI behavior remains available without the
audit flag or bundle, for compatibility.

For existing reviewed inputs, add `--evidence-bundle '../scholar-private/inputs.json'`.
The bundle itself enables full-audit mode. When collecting a new profile, its
new profile/coauthor record is used; an old bundled profile does not override it.
Record-bound decisions fail if their titles, bylines, URLs or expected
classification have changed.

For a retained collection, use `--snapshot './collected.json'` and, if needed,
`--profile-snapshot './profile.json'`. `--offline` checks only retained responses.
In full-author mode, `--retry-unresolved` reconsiders unsuccessful author and HTTP
cache entries; combined with `--offline` it still forbids network access.

## One-command historical replay

```sh
python3 skills/scholar-independent-citations/scripts/report.py \
  --evidence-bundle '../scholar-private/reviewed-evidence.json' --replay \
  --output-dir '../scholar-audits/replayed-audit'
```

The manifest supplies the original collection and profile, frozen author
metadata, source-author evidence, and approved relationship decisions. The
command needs no cache-directory preparation or further mode flags. It validates
file hashes, checks complete bylines against their original records, classifies
every relationship again, then applies only explicitly approved pair-specific
decisions. Stored status fields or totals in metadata are not used as results.

`--replay` forbids fresh collection and metadata retry flags. A different
snapshot/profile, missing record, altered evidence file or stale review decision
fails rather than silently producing a purported reproduction. It checks all
original result IDs; matching only aggregate counts is insufficient validation.

Replay can preserve an earlier Scholar-only evidence tier even when a later
fresh audit finds a full byline. It preserves the historical evidence boundary
instead of claiming that old source coverage was better than recorded. It is
not an automatic way to settle new same-name identity questions.

## Evidence-bundle schema

The JSON manifest uses `schema_version: 1`. Paths may be absolute or relative to
the manifest. Exported frozen author-record files resolve their nested evidence
paths relative to those files. A file descriptor has `path` and `sha256`.

| Field | Meaning |
| --- | --- |
| `author_id` | Optional binding to the target Scholar profile. |
| `snapshot`, `profile` | File descriptors for the exact historical collection and profile. Required for replay. |
| `inputs.native_cache` | Optional legacy metadata cache; complete bylines are revalidated, never imported as classifications. |
| `inputs.http_caches` | Retained HTTP response directories. Successful body hashes are checked before reuse. |
| `inputs.discoveries` | Result-ID mapping to `candidate_urls`, provenance and verified `record_bridges`. |
| `inputs.supplements` | Result-ID mapping to lists of checked complete-byline candidates. |
| `inputs.source_identities` | Checked source names and Scholar IDs, with primary `evidence_body_path` / `evidence_body_sha256`, `verified: true` and source URL. |
| `replay.authors`, `replay.sources` | File descriptors for frozen author-evidence mappings. Required only for replay. |
| `review_decisions` | Explicit human decisions bound to individual source/result pairs. |
| `files` | Additional retained evidence file descriptors to check before analysis. |

A normal byline candidate records `provider`, `source`, `title`, `authors`
(objects with `name` and any retained identifiers), and completeness. Supported
reviewed providers add the following evidence:

- `retained_pdf_byline`: `byline_verified`, original PDF/text paths and SHA-256
  values, reviewed physical pages and reviewer provenance. The script checks
  existing retained files; it does not extract, change or download PDFs.
- `retained_html_byline` / `retained_text_byline`: `byline_verified` and an
  `evidence` file descriptor. The source URL and title must identify the record.
- `scholar_byline_correction`: verified literal role evidence for an affiliation
  or committee member misparsed as an author. Unsupported roles are rejected.
- `scholar_name_corrections`: verified literal affiliation-marker evidence tied
  to the same original record and byline. Surname endings are never stripped by
  a generic guessing rule.
- `identity_bridge`: reviewed publication-link or book/chapter evidence with
  hashes and identifiers. A bridge supports metadata reuse without merging
  citation relationships.

The tests include minimal synthetic examples of each validation shape. The old
title-keyed `reviewed_authors.json` hook remains available in compatibility mode;
it is not a substitute for full-audit evidence or relationship decisions.

## Approved relationship decisions

Each decision requires:

```json
{
  "source_citation_id": "AUTHOR_ID:SOURCE_ID",
  "scholar_result_id": "RESULT_ID",
  "source_title": "Exact cited title",
  "citing_title": "Exact citing title",
  "citing_url": "https://publisher.example/work",
  "source_authors": ["Alex Example"],
  "citing_authors": ["Alex Example"],
  "expected_status": "non_independent",
  "status": "independent",
  "reason": "Reviewed evidence identifies distinct people.",
  "reviewer": "Reviewer name or role",
  "reviewed_at_utc": "2000-01-07T00:00:00+00:00",
  "evidence": [{"path": "review-evidence.txt", "sha256": "FILE_SHA256"}]
}
```

This is a schema illustration; replace identifiers, evidence and hash with the
reviewed case. An independent decision requires complete cited and citing
bylines. Shared-author decisions must identify `overlapping_author` in the cited
byline. Unknown/duplicate pairs, changed authors, missing evidence, undated
reviews and unexpected prior statuses are rejected. Decisions are not global
name aliases, inferred identities or automatic affiliation disambiguation.

Reports mark these decisions as reviewed. A run with any such decision does not
describe all classifications as automatic. Name-based exclusions without an
approved correction continue to receive the identity warning.

## Export and verification

Use `--export-evidence-bundle '<new path.json>'` on a successful full audit or
replay. It writes snapshot/profile and frozen author metadata JSON next to a new
manifest. It records source file hashes and relative references. It refuses to
overwrite an existing manifest. PDFs and other original source files remain in
place; keep all referenced files available. This is a reproducibility manifest,
not a self-contained archive or permission to publish the evidence.

Operational timestamps and current analyzer hashes change between runs.
Verification should compare ordered relationship keys, statuses, cited/citing
authors and IDs, overlap, evidence levels and source references, plus all
per-source counts and evidence-tier totals. A fresh acquisition can improve
evidence while preserving classifications; report that difference explicitly.

The only default visible deliverables remain `report.html` and
`independent_citations.csv`, with the original thirteen CSV columns. Rich author
evidence, review decisions, source provenance, mode, input/bundle hashes and
analyzer hashes live in the internal audit. The paired-author HTML change must
be included so that warnings and the displayed policy match full-audit mode.

Provider-record cache reuse is restricted to explicit article/version IDs and
ASCII case variants of a Crossref DOI. It does not equate arbitrary URL queries
or similarly titled works. See the [DOI Handbook](https://www.doi.org/doi-handbook/DOI_Handbook_Final.pdf)
for the ASCII case-insensitivity rule. Publisher PII links can establish the
original journal record instead of selecting a same-title preprint by rank.
