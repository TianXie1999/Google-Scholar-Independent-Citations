#!/usr/bin/env python3
"""Generate an English Scholar-based citation report and independent CSV."""
import argparse
from collections import Counter
import csv
import html
import json
from pathlib import Path
import re
import unicodedata
import urllib.parse
from collect import collect
from enrich_authors import enrich


def words(name):
    name = unicodedata.normalize('NFKD', name).casefold()
    name = ''.join(c for c in name if not unicodedata.combining(c))
    return re.findall(r'[^\W\d_]+', name, re.UNICODE)


def name_key(name):
    return ''.join(words(name))


def abbreviation(name):
    p = words(name)
    return (p[-1], ''.join(x if len(x) <= 2 else x[0] for x in p[:-1])) if p else ('', '')


def name_matches(a, b):
    if name_key(a) == name_key(b):
        return True
    aa, bb = words(a), words(b)
    if len(aa) < 2 or len(bb) < 2 or aa[-1] != bb[-1]:
        return False
    # Both spelled-out first names: do not merge different given names.
    if len(aa[0]) > 2 and len(bb[0]) > 2:
        return aa[:-1] == bb[:-1]
    sa, sb = abbreviation(a)[1], abbreviation(b)[1]
    return bool(sa and sb) and (sa.startswith(sb) or sb.startswith(sa))


def ellipsis(text):
    return bool(re.search(r'…|\.\.\.', text))


def scholar_record(item):
    info = item.get('publication_info') or {}
    author_text = (info.get('summary') or '').split(' - ')[0]
    authors = [a for a in info.get('authors', []) if a.get('name')]
    if not authors:
        authors = [{'name':a.strip()} for a in author_text.split(',') if a.strip()]
    if not authors:
        # Missing data is a collection failure, not a guessed independent row.
        raise ValueError('Scholar returned no author record: ' + item['title'])
    return authors, ellipsis(author_text + ' '.join(a['name'] for a in authors))


def known_ids(data, profile):
    mapping = {name_key(data['author']['name']): data['author_id']}
    mapping.update({name_key(a['name']):a['author_id'] for a in profile.get('co_authors', [])})
    own = {name_key(p['paper']['title']):p for p in data['papers']}
    for p in data['papers']:
        for item in p['citing']:
            source = own.get(name_key(item['title']))
            if source:
                for a in (item.get('publication_info') or {}).get('authors', []):
                    matches = [n.strip() for n in source['detail']['authors'].split(',')
                               if abbreviation(n) == abbreviation(a['name'])]
                    if len(matches) == 1 and a.get('author_id'):
                        mapping[name_key(matches[0])] = a['author_id']
    return mapping


def match_evidence(overlap, citing, ids, reason):
    """Describe the recorded match without making another classification."""
    if not overlap:
        return '', ''
    original_id = ids.get(name_key(overlap))
    for author in citing:
        if original_id and author.get('author_id'):
            if original_id == author['author_id'] and reason == 'Shared Scholar author ID':
                return 'scholar_id', author['name']
            continue
        if name_matches(overlap, author['name']):
            return ('full_name' if name_key(overlap) == name_key(author['name']) else 'initials'), author['name']
    return '', ''


def classify(originals, citing, ids, truncated):
    for original in originals:
        original_id = ids.get(name_key(original))
        for author in citing:
            citing_id = author.get('author_id')
            if original_id and citing_id:
                if original_id == citing_id:
                    return 'non_independent', original, 'Shared Scholar author ID'
                continue
            if name_matches(original, author['name']):
                return 'non_independent', original, 'Shared author name (Scholar naming convention)'
    if truncated:
        return 'unresolved', '', 'Explicit ellipsis in author list'
    return 'independent', '', 'No shared author in Scholar record'


def generate(data, cache, output, offline=False):
    profile_path = cache / f"profile_{data['author_id']}.json"
    if not profile_path.exists():
        profile_path = cache / 'profile.json'  # legacy snapshot support
    profile = json.loads(profile_path.read_text())
    ids = known_ids(data, profile)
    reviewed_path = cache / 'reviewed_authors.json'
    reviewed = json.loads(reviewed_path.read_text()) if reviewed_path.exists() else {}
    rows, summaries, supplemented = [], [], {}
    for paper in data['papers']:
        originals = [a.strip() for a in paper['detail']['authors'].split(',')]
        local = []
        for item in paper['citing']:
            authors, truncated = scholar_record(item)
            status, overlap, reason = classify(originals, authors, ids,
                                              truncated or ellipsis(paper['detail']['authors']))
            evidence_url = 'https://scholar.google.com/scholar?' + urllib.parse.urlencode({'q':'"'+item['title']+'"'})
            method = 'Scholar author record'
            # External checks only for explicit author-list ellipses. Initials
            # and a lack of external confirmation never cause uncertainty.
            if status == 'unresolved' and truncated and not ellipsis(paper['detail']['authors']):
                key = item.get('result_id') or item.get('link') or item['title']
                if key not in supplemented:
                    checked = reviewed.get(item['title'])
                    if checked:
                        ev = dict(checked, complete=True)
                    elif offline:
                        import hashlib
                        from enrich_authors import norm
                        digest = hashlib.sha256((norm(item['title'])+'|'+(item.get('link') or '')).encode()).hexdigest()[:20]
                        path = cache/'metadata'/(digest+'.json')
                        ev = json.loads(path.read_text()) if path.exists() else {}
                    else:
                        ev = enrich(item, cache)
                    supplemented[key] = ev
                ev = supplemented[key]
                if ev.get('complete') and ev.get('authors'):
                    authors = [{'name':a} for a in ev['authors']]
                    status, overlap, reason = classify(originals, authors, ids, False)
                    evidence_url = ev['source']
                    method = 'Full author list supplement for Scholar ellipsis'
            years = re.findall(r'\b(?:19|20)\d{2}\b', (item.get('publication_info') or {}).get('summary', ''))
            row = {'source_title':paper['paper']['title'], 'source_authors':'; '.join(originals),
                   'source_scholar_url':paper['paper']['link'], 'citing_title':item['title'],
                   'citing_authors':'; '.join(a['name'] for a in authors),
                   'citing_year':years[-1] if years else '', 'citing_url':item.get('link') or '',
                   'status':status, 'overlapping_author':overlap, 'classification_basis':reason,
                   'author_source':method, 'author_evidence_url':evidence_url,
                   'scholar_result_id':item.get('result_id') or '',
                   'source_citation_id':paper['paper'].get('citation_id', ''), 'source_year':paper['paper'].get('year', ''),
                   'source_article_url':paper['detail'].get('link') or paper['paper']['link'],
                   'author_evidence_kind':('scholar_visible' if method == 'Scholar author record' else
                                           'reviewed_author_list' if item['title'] in reviewed else ev.get('method', 'full_author_supplement')),
                   'original_result_type':item.get('type', ''),
                   'original_scholar_summary':(item.get('publication_info') or {}).get('summary', '')}
            match_kind, matched_name = match_evidence(overlap, authors, ids, reason)
            row.update(author_match_kind=match_kind, matched_citing_author=matched_name)
            rows.append(row)
            local.append(row)
        counts = Counter(r['status'] for r in local)
        summaries.append({'title':paper['paper']['title'], 'displayed':paper['displayed'],
                          'retrieved':len(local), **{k:counts[k] for k in ('independent','non_independent','unresolved')},
                          'end_reason':paper['end_reason']})
    counts = Counter(r['status'] for r in rows)
    report = {'author':data['author']['name'], 'author_id':data['author_id'], 'date':data['snapshot_date'],
              'displayed':sum(p['displayed'] for p in summaries), 'retrieved':len(rows),
              'classification':{k:counts[k] for k in ('independent','non_independent','unresolved')},
              'unique_independent_by_title':len({name_key(r['citing_title']) for r in rows if r['status']=='independent'}),
              'papers':summaries, 'citation_edges':rows,
              'policy':'Scholar authoritative; IDs first, names otherwise; only explicit author ellipses remain uncertain.'}
    report['complete_snapshot'] = all(p['displayed']==p['retrieved'] and p['end_reason'] not in ('scholar_1000_limit','repeated_page') for p in summaries)
    output.mkdir(parents=True, exist_ok=True)
    fields = ['source_title','source_authors','source_scholar_url','citing_title','citing_authors','citing_year','citing_url','status','overlapping_author','classification_basis','author_source','author_evidence_url','scholar_result_id']
    with (output/'independent_citations.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: r[key] for key in fields} for r in rows if r['status']=='independent')
    cache.mkdir(parents=True, exist_ok=True)
    (cache/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    render(report, output/'report.html')
    return report


def render(r, path):
    e = lambda value: html.escape(str(value), quote=True)
    labels = {'independent': 'Independent', 'non_independent': 'Non-independent',
              'unresolved': 'Unresolved' if r.get('analysis_mode') == 'full_author_audit' else 'Unresolved (author ellipsis)'}
    kinds = {'scholar_id': 'Scholar ID match', 'full_name': 'Full name match', 'initials': 'Initials match', 'name': 'Name match'}
    policy_description = r.get('policy_description',
        "Scholar IDs take precedence; otherwise the tool compares full names and conventional initials. "
        "Only explicit author-list ellipses require supplementation; missing publisher confirmation does not itself change classification.")
    evidence_summary = ''
    if r.get('analysis_mode') == 'full_author_audit':
        backed = sum(row['status'] == 'independent' and row.get('author_evidence_level') == 'matched_full_metadata'
                     for row in r['citation_edges'])
        visible = r['classification']['independent'] - backed
        evidence_summary = f'<p>Full author audit: {backed} independent candidates with full metadata; {visible} with Scholar-only bylines.</p>'
    evidence_labels = {'scholar_visible': 'Visible Scholar byline', 'publisher_citation_meta': 'Publisher author metadata',
                       'crossref_title_verified': 'Crossref author metadata', 'reviewed_author_list': 'Reviewed author list'}
    def match_kind(row):
        return row.get('author_match_kind') or ('scholar_id' if row['classification_basis'] == 'Shared Scholar author ID'
                                               else 'name' if row['status'] == 'non_independent' else '')
    def is_name_match(row):
        return row['status'] == 'non_independent' and match_kind(row) in ('full_name', 'initials', 'name')
    def source_key(row):
        return row.get('source_citation_id') or row['source_scholar_url']
    def byline(value, highlight):
        return '; '.join('<mark>' + e(name.strip()) + '</mark>' if highlight and name_key(name) == name_key(highlight)
                         else e(name.strip()) for name in value.split(';'))
    def citation(row):
        kind = match_kind(row)
        warning = ('<p class="identity-warning"><strong>Potential false positive:</strong> names or initials can belong to different people. '
                   'Check the authors\' identities before confirming this exclusion.</p>') if is_name_match(row) else ''
        source_title = row['source_title'] + (f" ({row['source_year']})" if row.get('source_year') else '')
        article = ('<a href="' + e(row['citing_url']) + '" target="_blank" rel="noopener">' + e(row['citing_title']) + '</a>') if row['citing_url'] else e(row['citing_title'])
        missing = ''
        if not row['citing_url']:
            search = 'https://scholar.google.com/scholar?' + urllib.parse.urlencode({'q': '"' + row['citing_title'] + '"'})
            description = 'This is a linkless Scholar citation record.' if row.get('original_result_type', '').casefold() == 'citation' else 'The archived Scholar result has no article URL.'
            missing = f'<p class="muted">{description} <a href="{e(search)}" target="_blank" rel="noopener">Search this title on Scholar</a>.</p>'
        evidence_kind = row.get('author_evidence_kind') or ('scholar_visible' if row['author_source'] == 'Scholar author record' else 'full_author_supplement')
        metadata = f'<a href="{e(row["author_evidence_url"])}" target="_blank" rel="noopener">Citing author evidence</a>' if row['author_evidence_url'] else ''
        review = row.get('reviewed_identity_correction')
        reviewed_note = (f'<p class="muted">Reviewed relationship decision: {e(review.get("reviewer", ""))} '
                         f'({e(review.get("reviewed_at_utc", ""))}).</p>') if isinstance(review, dict) and review else ''
        source_metadata = (f'<a href="{e(row["source_author_evidence_url"])}" target="_blank" rel="noopener">Cited author evidence</a>') if row.get('source_author_evidence_url') else ''
        return f'''<details data-status="{e(row['status'])}" data-match="{e(kind)}" data-source="{e(source_key(row))}">
<summary><span class="tag">{e(labels[row['status']])} · {e(kinds.get(kind, ''))}</span> {e(row['citing_title'])}{' <span class="warning-label">Review identity</span>' if warning else ''}</summary>
<div class="pair"><section><h3>Cited paper</h3><p class="paper-title"><a href="{e(row['source_scholar_url'])}" target="_blank" rel="noopener">{e(source_title)}</a></p><p>{byline(row['source_authors'], row['overlapping_author'])}</p></section>
<section><h3>Citing paper</h3><p class="paper-title">{article}</p><p>{byline(row['citing_authors'], row.get('matched_citing_author') or row['overlapping_author'])}</p>{missing}</section></div>
<p>Recorded overlap: <b>{e(row['overlapping_author'] or 'None')}</b>. {e(row['classification_basis'])}.</p>{warning}
<p class="muted">{e(evidence_labels.get(evidence_kind, 'Supplemented author list'))}. {metadata} {source_metadata}</p>{reviewed_note}
<p class="records">Cited record: {e(row.get('source_citation_id') or row['source_scholar_url'])}<br>Citing result: {e(row.get('scholar_result_id', ''))}</p></details>'''
    name_count = sum(is_name_match(row) for row in r['citation_edges'])
    cards = ''.join(f'<div class="card"><strong>{r["classification"][k]}</strong>{label}</div>' for k, label in labels.items())
    table = ''.join('<tr>' + ''.join('<td>' + e(p[k]) + '</td>' for k in ('title', 'displayed', 'retrieved', 'independent', 'non_independent', 'unresolved')) + '</tr>' for p in r['papers'])
    sources = {}
    for row in r['citation_edges']:
        sources.setdefault(source_key(row), row['source_title'] + (f" ({row['source_year']})" if row.get('source_year') else ''))
    options = ''.join(f'<option value="{e(key)}">{e(title)}</option>' for key, title in sources.items())
    details = ''.join(citation(row) for row in r['citation_edges'])
    coverage = 'All per-paper retrieved counts match the Scholar snapshot.' if r['complete_snapshot'] else 'INCOMPLETE COLLECTION: displayed and retrieved counts differ or a retrieval limit was reached. Counts below describe retrieved relationships only.'
    path.write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(r['author'])} — Independent citations</title>
<style>
body{{font:16px/1.6 system-ui,sans-serif;background:#f5f7fa;color:#233044;max-width:1240px;margin:30px auto;padding:0 24px}}h1{{font-size:32px}}.cards{{display:flex;gap:20px;flex-wrap:wrap}}.card{{background:white;border:1px solid #dde3eb;border-radius:12px;padding:16px 28px}}.card strong{{display:block;font-size:34px}}table{{border-collapse:collapse;background:white;width:100%;font-size:14px}}th,td{{padding:10px;border-bottom:1px solid #e3e7ef;text-align:left}}details{{background:white;padding:14px 18px;margin:10px 0;border:1px solid #dde3eb;border-radius:8px}}summary{{cursor:pointer}}.tag{{font-size:12px;color:#52677e}}a{{color:#0754a1;overflow-wrap:anywhere}}input,select{{font:inherit;padding:8px}}.scroll{{overflow:auto}}.controls{{position:sticky;top:0;background:#f5f7faf5;padding:12px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:center}}.controls select{{max-width:380px}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:28px}}.pair section{{min-width:0}}h3{{font-size:13px;text-transform:uppercase;color:#52677e;margin:18px 0 8px}}.paper-title{{font-weight:650}}.identity-warning{{background:#fff2cc;border-left:4px solid #a16c00;padding:10px 12px}}.warning-label{{font-size:12px;background:#fff2cc;padding:3px 6px;border-radius:4px}}mark{{background:#fff0ad;color:inherit;padding:1px 3px}}.muted,.records{{font-size:14px;color:#52677e}}.records{{overflow-wrap:anywhere}}details[hidden]{{display:none}}@media(max-width:760px){{.pair{{grid-template-columns:1fr;gap:0}}.controls{{position:static}}.controls select{{max-width:100%}}}}@media print{{.controls{{display:none}}details{{break-inside:avoid}}}}
</style></head><body>
<h1>{e(r['author'])} · Independent citations</h1><p>Snapshot: {e(r['date'])}. Scholar displayed: {r['displayed']}. Retrieved citation relationships: {r['retrieved']}.</p>
<div class="cards">{cards}</div>{evidence_summary}<p>{e(coverage)}</p>
<p>Independence is assessed against each cited paper's authors, including the profile owner. Main totals preserve every archived Scholar result separately for each cited paper, including separately indexed versions. Approximate distinct independent titles: {r['unique_independent_by_title']}.</p>
<p>{e(policy_description)} Name-based exclusions can join different people. <a href="#citation-details" onclick="reviewNames()">Review {name_count} name-based matches</a>.</p>
<p>Author overlap and Scholar-list membership do not establish the context or significance of a citation or verify its reference list.</p>
<p><a href="independent_citations.csv" download>Download independent_citations.csv</a></p>
<div class="scroll"><table><thead><tr><th>Source paper</th><th>Scholar displayed</th><th>Retrieved</th><th>Independent</th><th>Non-independent</th><th>Unresolved</th></tr></thead><tbody>{table}</tbody></table></div>
<h2 id="citation-details">Citation details</h2>
<div class="controls"><input id="search" aria-label="Search titles or authors" placeholder="Search titles or authors"><select id="filter" aria-label="Classification"><option value="">All citations</option><option value="independent">Independent</option><option value="non_independent">Non-independent</option><option value="unresolved">Unresolved</option></select><select id="match" aria-label="Author match"><option value="">All match evidence</option><option value="name">Names or initials needing review</option><option value="full_name">Full name</option><option value="initials">Initials</option><option value="scholar_id">Scholar ID</option></select><select id="source" aria-label="Cited paper"><option value="">All cited papers</option>{options}</select><label><input id="expand" type="checkbox">Show author lists</label></div>
<p id="shown" aria-live="polite">{r['retrieved']} relationships shown</p>{details}
<script>
function filter(){{const q=document.getElementById('search').value.toLowerCase(),s=document.getElementById('filter').value,m=document.getElementById('match').value,p=document.getElementById('source').value,expand=document.getElementById('expand').checked;let shown=0;document.querySelectorAll('details[data-status]').forEach(d=>{{const matches=m==='name'?['name','full_name','initials'].includes(d.dataset.match):d.dataset.match===m;d.hidden=Boolean((q&&!d.textContent.toLowerCase().includes(q))||(s&&d.dataset.status!==s)||(m&&!matches)||(p&&d.dataset.source!==p));d.open=expand&&!d.hidden;if(!d.hidden)shown++;}});document.getElementById('shown').textContent=shown+' relationships shown';}}
function reviewNames(){{document.getElementById('filter').value='non_independent';document.getElementById('match').value='name';document.getElementById('expand').checked=true;filter();}}
for(const id of ['search','filter','match','source','expand'])document.getElementById(id).addEventListener(id==='search'?'input':'change',filter);
</script></body></html>''', encoding='utf-8')




def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scholar-url')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--cache-dir',type=Path)
    parser.add_argument('--snapshot',type=Path,help='Reuse a collected.json snapshot without SerpApi calls.')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--request-budget',type=int,default=200)
    args=parser.parse_args()
    if args.snapshot:
        data=json.loads(args.snapshot.read_text())
        cache=args.cache_dir or args.snapshot.parent
    else:
        if not args.scholar_url:
            parser.error('--scholar-url is required unless --snapshot is supplied')
        url=urllib.parse.urlparse(args.scholar_url)
        if url.hostname not in ('scholar.google.com','scholar.google.co.uk','scholar.google.com.hk') or url.path != '/citations':
            parser.error('Supply a Google Scholar author profile URL.')
        author_id=urllib.parse.parse_qs(url.query).get('user',[''])[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]+',author_id):
            parser.error('Profile URL must contain a valid user ID.')
        cache=args.cache_dir or args.output_dir/'.scholar-cache'/author_id
        if args.offline:
            data=json.loads((cache/'collected.json').read_text())
        else:
            data=collect(author_id,str(cache),args.request_budget)
    result=generate(data,cache,args.output_dir,args.offline)
    print(json.dumps({k:v for k,v in result.items() if k not in ('papers','citation_edges')},ensure_ascii=False))


if __name__=='__main__':
    main()
