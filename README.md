# Scholar Independent Citations

A reusable Codex skill that turns a Google Scholar author profile and a user-provided SerpApi key into:

- **`report.html`** — an English report with totals, per-paper counts, searchable citation details, and classification evidence.
- **`independent_citations.csv`** — one row per independent citation relationship, ready to open in Excel or process in Python.

Note: one or two other repos (e.g., https://github.com/guaguakai/Automated-Independent-Citations) aimed at the same thing. The existing tool relies on exact author-name matching and treats truncated author lists as complete, which can misclassify citations. This skill prioritizes Scholar author IDs, handles name variations, flags incomplete author lists, and exports an auditable HTML report and independent-citation CSV. Our skill relies on SerpApi avoiding repeatedly crawling by ourselves, and the free api key can support weekly query of an author with a few thousand citations.

## What counts as an independent citation?

A citing paper is independent of a source paper when they share **no authors**, including the profile owner. Classification is per source paper: a citing article citing two source papers contributes two citation relationships. The report separately shows an approximate count of distinct independent citing articles.

Google Scholar is the authoritative record:

1. Compare Scholar author IDs when both sides provide them.
2. Otherwise compare normalized names and conventional initials: for example, `J Example` matches `John Example`.
3. Only explicit **author-list ellipses** require additional checking. Ellipses in venue names do not matter. Short names or missing publisher confirmation do not create an unresolved classification.
4. For omitted authors, attempt to retrieve a complete author list from the article page or a title-matched Crossref record. Retain unresolved status if this fails.

## Install as a Codex skill

In Codex, provide this repository's GitHub link and ask:

> Install the skill from this GitHub repository, path `skills/scholar-independent-citations`.

Alternatively, clone or download this repository and copy the `skills/scholar-independent-citations` directory into your personal Codex skills directory, normally `~/.codex/skills/`.

## Use in Codex

Invoke `$scholar-independent-citations`, provide a Google Scholar **author profile** URL, and provide your own SerpApi key. For example:

> Use $scholar-independent-citations with this Scholar profile and my SerpApi key. Generate the English HTML report and independent_citations.csv.

The skill runs the bundled scripts and returns links to both files. Each user supplies their own SerpApi credentials.

## Run directly

Requires **Python 3.9+**. The scripts use only the standard library; no Python packages are required. Use a new output directory for a fresh snapshot:

```sh
export SERPAPI_API_KEY='YOUR_SERPAPI_KEY'
python3 skills/scholar-independent-citations/scripts/report.py \
  --scholar-url 'https://scholar.google.com/citations?user=AUTHOR_ID' \
  --output-dir './output'
```

The CSV uses UTF-8 with BOM for Excel compatibility. The HTML includes a link to the CSV, so keep the two files together.

Internal caches and the classification audit live in `output/.scholar-cache/AUTHOR_ID/`. Rerunning the command reuses this snapshot and resumes interrupted collection. To refresh the data, choose a new output directory.

The default per-run budget is 200 new SerpApi requests. Set `--request-budget` to adjust it. A normal first run needs approximately one request per 100 source papers, one detail request per source paper, and one request per 20 citing results. Author-list supplements do not consume SerpApi search requests.

## Limitations

- Scholar can expose at most 1,000 results for a single citing-paper query. This collector does not yet split larger queries by date; such reports explicitly mark incomplete coverage.
- Name matching follows Scholar naming conventions when IDs are unavailable. The result is a Scholar-based statistic, not an independent author-identity audit.
- A missing author record or API failure stops collection rather than silently guessing. Successful requests are cached for resuming.
- Some truncated author lists cannot be supplemented; these remain visibly unresolved and are excluded from the independent CSV.

## Development

### Author metadata recovery

Enrichment preserves uniquely aligned Scholar author IDs when expanding names,
normalizes surname-first author fields, and checks automatic supplements against
the visible byline. Anonymous placeholders, omitted visible authors and
conflicting title-matched bylines do not establish a complete author list.
The existing Scholar-ID and initials classification rules remain in place;
this is metadata validation, not automatic person disambiguation.

Publisher lookup supports arXiv abstract pages and IEEE's embedded author JSON.
Explicit DOI information is retained from known PDF URL forms without fetching
the PDF. OpenReview lookup uses the original case-sensitive note ID on official
API hosts, with a bounded search fallback when direct lookup is unavailable.
OpenReview profile identifiers are not treated as Scholar author IDs. A matching
record identifier can support a title variant without choosing another paper by
search rank. Separately indexed Scholar records remain separately counted.

Metadata HTTP requests have bounded retries for temporary failures. Long
Retry-After delays, permanent errors and browser verification are not repeatedly
retried in place. Failed derived metadata stays cached until explicitly retried;
successful metadata is reused and revalidated. To reconsider incomplete metadata
without recollecting Scholar, reuse the snapshot:

```sh
python3 skills/scholar-independent-citations/scripts/report.py \
  --snapshot './output/.scholar-cache/AUTHOR_ID/collected.json' \
  --cache-dir './output/.scholar-cache/AUTHOR_ID' \
  --output-dir './retried-report' --retry-unresolved
```

This metadata-only command makes no new SerpApi collection searches. It requires
public metadata access, so `--retry-unresolved` cannot be combined with
`--offline`. Metadata caches retain sanitized lookup attempts and timestamps;
unresolved report entries summarize the last attempted route. A failed route
means the byline was not obtained through that route, not that authors are
unavailable everywhere. The documented `reviewed_authors.json` input remains
available for checked manual bylines.

Run local tests without making API requests:

```sh
python3 -m unittest discover \
  -s skills/scholar-independent-citations/scripts \
  -p 'test_*.py'
```

The tests cover ID precedence, initial matching, author ellipses, per-paper classification, Unicode names, empty profiles, credential sanitization, and pagination.

## License

[MIT](LICENSE)
