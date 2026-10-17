---
name: scholar-independent-citations
description: Given a Google Scholar author profile URL and a SerpApi key, collect citing papers, exclude citations sharing any author with each cited paper, and produce an English HTML report and independent_citations.csv. Use for Scholar-based independent citation counts and citation-list exports.
---

# Scholar Independent Citations

Produce two user-facing files: `report.html` and `independent_citations.csv`. All report labels, explanations, statuses, and CSV headers are English. Preserve original article titles and author names. Return clickable links to both files and the independent/non-independent counts.

## Inputs and execution

Obtain a Scholar author profile URL (`https://scholar.google.com/citations?user=...`) and SerpApi key. Ask only for missing inputs; otherwise proceed with this authorized read-only workflow.

Run with Python 3.9+ (standard library only):

```sh
SERPAPI_API_KEY='<provided key>' python3 <skill-directory>/scripts/report.py \
  --scholar-url '<provided profile URL>' \
  --output-dir '<absolute writable output directory>'
```

Pass the key through the process environment; never save it in skill files, code, reports, logs, or caches, and do not repeat it in user-facing text. Use the environment's network approval mechanism when required. No dependency installation or browser login is needed.

Use a new output directory for each fresh snapshot. Internal caches live in `<output>/.scholar-cache/<author-id>/`; reuse that directory to resume, not to refresh. `REPORT_TIMEZONE` overrides the default `America/New_York` snapshot date. Default budget: 200 new SerpApi requests per run, adjustable with `--request-budget`. If reached, report saved progress and resume within the user's authorized budget; never silently drop remaining papers. Authentication/network errors stop without logging the key or request URL.

## Classification policy — preserve exactly

- Exclude citations sharing **any** author with the cited paper, including the profile owner. Compare per paper, not against all lifetime collaborators.
- **Scholar is authoritative.** Do not require external confirmation of an untruncated Scholar author record.
- Compare Scholar author IDs when both sides have them. Equal IDs mean shared authors; different IDs take precedence over similar names.
- Otherwise compare normalized names and conventional surname/initial forms. `T Example` matches `Taren Example`; `X Sample` matches `Xerin Sample`. These are definitive non-independent matches under the user's chosen convention. Initials, same-name possibilities, and lack of publisher confirmation do **not** cause uncertainty.
- **Only explicit author-list ellipses (`…` or `...`) justify unresolved classification.** Ignore ellipses in venues, titles, or snippets. A visible shared author already proves non-independence despite omitted remaining authors.
- For author ellipses only, the script attempts full authors from the article page or a strictly title-matched Crossref record. If successful, classify normally; otherwise retain unresolved status. Never guess omitted authors.
- Missing author data is a collection failure to investigate, not a guessed independent citation. Incomplete retrieval is a coverage problem, distinct from author uncertainty.
- Count citation relationships per cited paper. One article citing two source papers counts twice. Separately show approximate independent article count deduplicated by normalized title.

## Collection and delivery

The bundled script gets paginated papers, full source-paper Scholar authors, and accessible `Cited by` pages through SerpApi's `google_scholar_author` and `google_scholar` engines. It caches sanitized results. No actual keys, fixed author IDs, titles, or prior results are embedded in this skill.

The English HTML contains totals, per-paper counts, searchable/filterable citation details, reasons, evidence links, and a CSV download link. CSV rows include both paper titles, authors, citing year, links, and classification basis.

Validate that status counts sum to retrieved citation relationships; per-paper counts reconcile; CSV row count equals independent count. Compare retrieved and displayed counts. Mark incomplete coverage explicitly when they differ or Scholar's 1,000-result limit is reached. The collector does not yet implement date partitions beyond that limit; never claim complete collection then.

Offline regeneration without API or metadata requests:

```sh
python3 <skill-directory>/scripts/report.py \
  --snapshot '<cache-directory>/collected.json' \
  --cache-dir '<cache-directory>' \
  --output-dir '<output-directory>' --offline
```

Optional verified full author lists belong in `<cache-directory>/reviewed_authors.json`, keyed by exact citing title with `authors` and `source` fields. Use them only for actual author ellipses, after checking that source. Internal `audit.json` records all classifications for verification. The only default user-facing deliverables are the HTML and independent CSV.
