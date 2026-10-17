---
name: scholar-independent-citations
description: Collect a Google Scholar profile, audit cited and citing author lists, apply evidence-bound reviewed decisions, and export independent citation candidates with an English HTML report. Replay an existing reviewed analysis from its private evidence bundle.
---

# Scholar Independent Citations

Produce `report.html` and `independent_citations.csv`. Keep all original source/result
relationships separate, including separately indexed versions. Return both links,
counts, evidence tiers and any unresolved cases. Do not describe candidates as a
complete identity or citation-context audit.

## Choose the workflow

For a fresh profile, use the recommended full-author audit:

```sh
python3 <skill-directory>/scripts/report.py \
  --scholar-url '<profile URL>' --full-author-audit \
  --output-dir '<new writable output directory>'
```

Use Python 3.9+ with the standard library and a configured `SERPAPI_API_KEY`.
Use an existing environment variable or user-designated credential file; do not
print or save the key in code, logs, reports or evidence bundles. Ask only for
missing inputs. TLS verification remains enabled. No browser login is required.
A fresh collection requires SerpApi quota; metadata lookup and historical replay
use no new SerpApi collection searches.

If the user supplies reviewed evidence, add `--evidence-bundle '<manifest.json>'`.
For reproducing that exact historical reviewed result, use only:

```sh
python3 <skill-directory>/scripts/report.py \
  --evidence-bundle '<reviewed manifest.json>' --replay \
  --output-dir '<new writable output directory>'
```

Replay takes its snapshot and profile from the manifest, validates retained
sources, reclassifies each pair, and reapplies explicit reviewed decisions.
It never recollects Scholar or fetches public metadata. Do not substitute a fresh
live profile and claim it reproduces the same historical relationship set.
Keep the referenced evidence files available and private.

## Full-author policy

- Compare per cited paper, including its profile owner and coauthors. A paper
  citing two source papers contributes two relationships.
- Inspect complete citing and source bylines, including untruncated Scholar lists
  and records already excluded from initials. Prefer trusted Scholar IDs where
  both sides have them. Different explicit IDs take precedence over similar names.
- Compare full normalized names next. Compatible initials alone remain unresolved.
  Distinct spelled-out names such as short given names must not be treated as
  initials. Never infer an author ID from a matching title, affiliation or topic.
- A recovered byline must cover visible authors and identify the correct record.
  Anonymous lists, candidate collisions and unaccounted-for missing authors stay
  unresolved. A failed route does not prove that no public byline exists.
- Use reviewed document bylines, source identities, non-author-role corrections
  and record links only with retained, hash-bound evidence. Never guess omitted
  authors or strip suspected affiliation letters without explicit source evidence.
- Same-name matches receive identity warnings. Apply an approved relationship
  correction only to its exact source/result pair and matching bylines. Do not
  turn one reviewed name collision into a global identity rule.
- Record links can support metadata reuse; they never merge archived citations.

The original naming-convention policy remains available by omitting the full-audit
flag and bundle. Use it only if that compatibility policy is specifically wanted;
its initials-as-shared-author rule and ellipsis-only enrichment differ from the
recommended audit. Label comparisons by their actual mode.

## Resume, review and retain

Use a new output directory for a fresh snapshot. Reusing a cache resumes it.
`--request-budget` controls new collection searches (default 200). Respect the
user's authorized quota. Coverage limits, including Scholar's 1,000-result query
cap, remain separate from author uncertainty; never hide missing relationships.

In full-author mode, `--retry-unresolved` reconsiders incomplete metadata and
negative HTTP cache entries. With `--offline` it uses retained responses only.
Historical `--replay` does not accept retry or fresh-collection flags.

Use `--export-evidence-bundle '<new manifest.json>'` to freeze author metadata
and the replay inputs. Referenced source documents remain in place and are not
modified or copied. The legacy `reviewed_authors.json` title-keyed hook applies
only to the original compatibility workflow; use evidence bundles for full-audit
bylines and pair-specific identity decisions.

Check all relationship keys, per-paper counts, evidence tiers, CSV rows and
classification sums. Report explicit manual decisions as reviewed rather than
automatically discovered. The default visible outputs remain the two report
files; rich evidence, rejection reasons and provenance live in the internal audit.
For the bundle schema and full commands see `docs/reviewed-author-audit.md` in
the repository.
