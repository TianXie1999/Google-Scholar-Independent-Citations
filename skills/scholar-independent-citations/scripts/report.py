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
from enrich_authors import enrich, canonical_name, validate_supplement, align_authors, visible_authors


def words(name):
    name = unicodedata.normalize('NFKD', canonical_name(name)).casefold()
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


def generate(data, cache, output, offline=False, retry_unresolved=False):
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
            lookup_attempts = []
            # External checks only for explicit author-list ellipses. Initials
            # and a lack of external confirmation never cause uncertainty.
            if status == 'unresolved' and truncated and not ellipsis(paper['detail']['authors']):
                key = item.get('result_id') or item.get('link') or item['title']
                if key not in supplemented:
                    checked = reviewed.get(item['title'])
                    if checked:
                        ev = dict(checked, complete=True, method='reviewed_author_list')
                    elif offline:
                        import hashlib
                        from enrich_authors import norm
                        digest = hashlib.sha256((norm(item['title'])+'|'+(item.get('link') or '')).encode()).hexdigest()[:20]
                        path = cache/'metadata'/(digest+'.json')
                        ev = json.loads(path.read_text()) if path.exists() else {}
                    else:
                        ev = enrich(item, cache, retry_unresolved=True) if retry_unresolved else enrich(item, cache)
                    supplemented[key] = ev
                ev = supplemented[key]
                lookup_attempts = list(ev.get('attempts', []))
                if ev.get('complete') and ev.get('authors'):
                    validation = '' if ev.get('method') == 'reviewed_author_list' else validate_supplement(item, ev)
                    expanded, conflict = align_authors(visible_authors(item), ev['authors'])
                    if not validation and not conflict:
                        authors = expanded
                        status, overlap, reason = classify(originals, authors, ids, False)
                        evidence_url = ev['source']
                        method = 'Full author list supplement for Scholar ellipsis'
                    else:
                        lookup_attempts.append({'provider': 'author_alignment', 'stage': 'validation',
                                                'reason': validation or conflict})
                if status == 'unresolved' and lookup_attempts:
                    latest = lookup_attempts[-1]
                    reason += '; attempted ' + latest['provider'] + ': ' + latest['reason']
            years = re.findall(r'\b(?:19|20)\d{2}\b', (item.get('publication_info') or {}).get('summary', ''))
            row = {'author_lookup_attempts':lookup_attempts, 'source_title':paper['paper']['title'], 'source_authors':'; '.join(originals),
                   'source_scholar_url':paper['paper']['link'], 'citing_title':item['title'],
                   'citing_authors':'; '.join(a['name'] for a in authors),
                   'citing_year':years[-1] if years else '', 'citing_url':item.get('link') or '',
                   'status':status, 'overlapping_author':overlap, 'classification_basis':reason,
                   'author_source':method, 'author_evidence_url':evidence_url,
                   'scholar_result_id':item.get('result_id') or ''}
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
    e=lambda value:html.escape(str(value),quote=True)
    labels={'independent':'Independent','non_independent':'Non-independent','unresolved':'Unresolved (author ellipsis)'}
    cards=''.join(f'<div class="card"><strong>{r["classification"][k]}</strong>{label}</div>' for k,label in labels.items())
    table=''.join('<tr>'+''.join('<td>'+e(p[k])+'</td>' for k in ('title','displayed','retrieved','independent','non_independent','unresolved'))+'</tr>' for p in r['papers'])
    details=''.join(f'<details data-status="{row["status"]}"><summary><span class="tag">{labels[row["status"]]}</span> {e(row["citing_title"])}</summary><p>Cites: {e(row["source_title"])}</p><p>Authors: {e(row["citing_authors"])}</p><p>Overlapping author: {e(row["overlapping_author"] or "None")}</p><p>Basis: {e(row["classification_basis"])} · {e(row["author_source"])}</p><a href="{e(row["author_evidence_url"])}" target="_blank" rel="noopener">Author record</a> · <a href="{e(row["citing_url"])}" target="_blank" rel="noopener">Article</a></details>' for row in r['citation_edges'])
    coverage='All per-paper retrieved counts match the Scholar snapshot.' if r['complete_snapshot'] else 'INCOMPLETE COLLECTION: some displayed counts differ from retrieved counts or a retrieval limit was reached. Counts below describe retrieved citation relationships only.'
    path.write_text(f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(r['author'])} — Independent citations</title><style>body{{font:16px/1.65 system-ui,sans-serif;background:#f5f7fa;color:#233044;max-width:1160px;margin:36px auto;padding:0 24px}}h1{{font-size:32px}}.cards{{display:flex;gap:20px;flex-wrap:wrap}}.card{{background:white;border:1px solid #dde3eb;border-radius:14px;padding:18px 30px}}strong{{display:block;font-size:36px}}table{{border-collapse:collapse;background:white;width:100%;font-size:14px}}th,td{{padding:12px;border-bottom:1px solid #e3e7ef;text-align:left}}details{{background:white;padding:12px 16px;margin:8px 0;border-radius:8px}}summary{{cursor:pointer}}.tag{{font-size:12px;color:#546c86}}a{{color:#236baf}}input,select{{font:inherit;padding:8px;margin:12px 8px 12px 0}}.scroll{{overflow:auto}}</style></head><body><h1>{e(r['author'])} · Independent citations</h1><p>Snapshot: {e(r['date'])}. Scholar displayed: {r['displayed']}. Retrieved citation relationships: {r['retrieved']}.</p><div class="cards">{cards}</div><p>{e(coverage)}</p><p>An independent citation shares no author with the cited paper, including the profile owner. Classification is per source paper. One citing article citing multiple source papers counts multiple times. Independent citing articles deduplicated by normalized title: approximately {r['unique_independent_by_title']}.</p><p>Scholar is the authoritative record. Compare Scholar author IDs when both are available; otherwise compare names, including conventional initials. Initials or lack of publisher confirmation do not cause uncertainty. Only an explicit ellipsis in an author list requires further checking; venue-name ellipses do not. Additional author sources are consulted only to fill those omitted authors.</p><p><a href="independent_citations.csv" download>Download independent_citations.csv</a></p><div class="scroll"><table><thead><tr><th>Source paper</th><th>Scholar displayed</th><th>Retrieved</th><th>Independent</th><th>Non-independent</th><th>Unresolved</th></tr></thead><tbody>{table}</tbody></table></div><h2>Citation details</h2><input id="search" placeholder="Search titles or authors"><select id="filter"><option value="">All citations</option><option value="independent">Independent</option><option value="non_independent">Non-independent</option><option value="unresolved">Unresolved</option></select>{details}<script>function filter(){{const q=document.getElementById('search').value.toLowerCase(),s=document.getElementById('filter').value;document.querySelectorAll('details').forEach(d=>d.hidden=Boolean((s&&d.dataset.status!==s)||!d.textContent.toLowerCase().includes(q)))}}document.getElementById('search').oninput=filter;document.getElementById('filter').onchange=filter;</script></body></html>''')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scholar-url')
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--cache-dir',type=Path)
    parser.add_argument('--snapshot',type=Path,help='Reuse a collected.json snapshot without SerpApi calls.')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--retry-unresolved',action='store_true',help='Retry incomplete metadata caches using the existing Scholar snapshot.')
    parser.add_argument('--request-budget',type=int,default=200)
    args=parser.parse_args()
    if args.offline and args.retry_unresolved:
        parser.error('--retry-unresolved requires metadata access; omit --offline.')
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
    result=generate(data,cache,args.output_dir,args.offline,args.retry_unresolved)
    print(json.dumps({k:v for k,v in result.items() if k not in ('papers','citation_edges')},ensure_ascii=False))


if __name__=='__main__':
    main()
