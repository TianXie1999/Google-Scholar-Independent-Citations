# Scholar Independent Citations

A reusable Codex skill that turns a Google Scholar author profile and a user-provided SerpApi key into:

- **`report.html`** — an English report with totals, per-paper counts, searchable citation details, and classification evidence.
- **`independent_citations.csv`** — one row per independent citation relationship, ready to open in Excel or process in Python.

Note: one or two other repos (e.g., https://github.com/guaguakai/Automated-Independent-Citations) aimed at the same thing. The existing tool relies on exact author-name matching and treats truncated author lists as complete, which can misclassify citations. This skill prioritizes Scholar author IDs, handles name variations, flags incomplete author lists, and exports an auditable HTML report and independent-citation CSV. Our skill relies on SerpApi avoiding repeatedly crawling by ourselves, with a user-provided request budget; metadata-source access is handled separately.

## What counts as an independent citation?

A citing paper is independent of a source paper when they share **no authors**, including the profile owner. Classification is per source paper: a citing article citing two source papers contributes two citation relationships. The report separately shows an approximate count of distinct independent citing articles.

The recommended full-author audit compares trusted Scholar IDs first, then full
normalized names. Compatible initials remain unresolved until fuller bylines or
checked identity evidence settle the case. It checks both citing and cited
papers, including untruncated lists and earlier initial-based exclusions.
Name-based exclusions carry review warnings; the tool does not automatically
disambiguate people. Every archived result remains separate for each source.

The original Scholar naming-convention mode remains available by omitting
`--full-author-audit`. In that compatibility mode, conventional initials count
as shared-author matches and only unresolved author ellipses trigger enrichment.
The report states which policy produced its counts.

## Reproduce a reviewed analysis

Use a retained evidence bundle for a one-command historical replay:

```sh
python3 skills/scholar-independent-citations/scripts/report.py \
  --evidence-bundle '../scholar-private/reviewed-evidence.json' --replay \
  --output-dir './replayed-output'
```

The bundle supplies the frozen snapshot/profile, author evidence, source
identities and explicitly approved relationship decisions. The tool recomputes
every relationship; it does not import old classification totals. Replay makes
no network or SerpApi calls and checks evidence hashes and review bindings.
Your bundle and referenced evidence stay outside the public repository.

A fresh audit can use the same reviewed inputs while reacquiring bylines by
omitting `--replay`. Its evidence may improve or change; it is not a historical
replay. See [the full workflow and bundle format](docs/reviewed-author-audit.md).

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
  --output-dir './output' --full-author-audit
```

The CSV uses UTF-8 with BOM for Excel compatibility. The HTML includes a link to the CSV, so keep the two files together.

The HTML places the cited and citing author lists together, highlights the
recorded match, and distinguishes Scholar-ID, full-name and initials matches.
Name-based exclusions carry a potential false-positive warning: different people
can share a name or initials. Use the identity-review filter and “Show author
lists” to inspect those cases. Warnings do not change classifications or counts;
the tool does not automatically disambiguate people or merge separately indexed
versions. Linkless Scholar citation records remain visible with an explicitly
labeled title-search link. Audit JSON retains record identifiers and evidence
types; the CSV schema and two default deliverables are unchanged.

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
The original Scholar-ID and initials rules remain available in compatibility
mode; full-author audit uses the documented stronger rule. Neither mode performs
automatic person disambiguation.

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
public metadata access in compatibility mode, where `--retry-unresolved` cannot
be combined with `--offline`. Full-author audit can reconsider retained metadata
offline; `--replay` always forbids acquisition and retry flags. Metadata caches retain sanitized lookup attempts and timestamps;
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

All person names, paper titles, publication identifiers and profile records in
test fixtures are fictional. The fixtures preserve name shapes (including
initials, accents and multiple given names) without using a researcher's archive.
The tests create their own temporary evidence files and mock external services;
they need no API key, account, network access or local research data.

## License

[MIT](LICENSE)

## Full author audit (optional)

Use `--full-author-audit` to inspect citing and cited bylines beyond the normal
ellipsis-only supplement workflow. It includes records already classified from
initials and untruncated lists. Source bylines can establish full names and middle
initials. This mode uses trusted Scholar IDs first, then full normalized names;
compatible initials remain unresolved. It does not infer person identities from
affiliations, coauthors, topics or approximate titles. Exact same-name exclusions
still need the review warnings from the paired-author interface.

```sh
python3 skills/scholar-independent-citations/scripts/report.py \
  --scholar-url 'https://scholar.google.com/citations?user=AUTHOR_ID' \
  --output-dir './audited-output' --full-author-audit
```

The audit checks primary metadata, exact OpenReview notes, explicit identifiers,
Crossref and OpenAlex. Author-list completeness limits and conflicting candidates
remain visible. DBLP XML and alternate/version URLs can be supplied as discovery
evidence. Already reviewed document bylines, source identities and record links
can be supplied through an external evidence bundle. Full-audit caches retain author evidence and rejected
candidates. No PDF extraction or automatic same-name disambiguation is performed.

The standard-library implementation keeps Python 3.9 support and the same two
default report files and thirteen CSV columns. Richer evidence levels and source
author provenance stay in the internal audit. `--offline` never acquires metadata.
The legacy Scholar-convention mode remains the default for compatibility.
Use the paired-author interface change with this mode so the HTML presents the
correct policy and evidence levels.
