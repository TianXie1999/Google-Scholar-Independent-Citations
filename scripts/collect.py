#!/usr/bin/env python3
"""Scholar citation collector. Standard library only; key comes from environment."""
import argparse
import csv
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def safe_data(value):
    if isinstance(value, dict):
        return {k: safe_data(v) for k, v in value.items()
                if k not in ('api_key', 'search_metadata', 'search_parameters')}
    if isinstance(value, list):
        return [safe_data(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'([?&]api_key=)[^&\s]+', r'\1REDACTED', value)
    return value


class Scholar:
    def __init__(self, root, budget=40):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget, self.requests = budget, 0

    def get(self, path, **params):
        cache = self.root / path
        if cache.exists():
            return json.loads(cache.read_text())
        if self.requests >= self.budget:
            raise RuntimeError('SerpApi request budget reached; rerun to resume.')
        key = os.environ.get('SERPAPI_API_KEY')
        if not key:
            raise RuntimeError('Set SERPAPI_API_KEY before making new requests.')
        self.requests += 1
        url = 'https://serpapi.com/search.json?' + urllib.parse.urlencode(
            dict(params, api_key=key, hl='en'))
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'SerpApi HTTP {exc.code}; no URL logged.') from None
        except urllib.error.URLError:
            raise RuntimeError('SerpApi network request failed; no URL logged.') from None
        error = data.get('error')
        if error and not (params.get('engine') == 'google_scholar' and
                          'no results' in error.lower()):
            raise RuntimeError('SerpApi error: ' + str(error).replace(key, 'REDACTED'))
        data = safe_data(data)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        return data


def collect(author_id, root='data', budget=40):
    api = Scholar(root, budget)
    profile_path = f'profile_{author_id}.json'
    profile = api.get(profile_path, engine='google_scholar_author', author_id=author_id, num=100)
    papers = list(profile.get('articles', []))
    page = profile
    start = 100
    while (page.get('serpapi_pagination') or {}).get('next'):
        page = api.get(f'profile_{author_id}_{start}.json', engine='google_scholar_author',
                       author_id=author_id, num=100, start=start)
        papers.extend(page.get('articles', []))
        start += 100
    output = []
    for i, paper in enumerate(papers):
        cid = paper['citation_id']
        detail = api.get(f"details/{cid.replace(':', '_')}.json",
                         engine='google_scholar_author', author_id=author_id,
                         view_op='view_citation', citation_id=cid).get('citation', {})
        if not detail.get('authors'):
            raise RuntimeError('Missing source-paper authors for ' + paper['title'])
        cited = detail.get('total_citations', {}).get('cited_by', {})
        cites = cited.get('cites_id') or paper.get('cited_by', {}).get('cites_id')
        displayed = paper.get('cited_by', {}).get('value') or 0
        results, seen, end_reason = [], set(), 'no_citations'
        if cites:
            for offset in range(0, 1000, 20):
                response = api.get(f'citing/{cites}_{offset}.json', engine='google_scholar',
                                   cites=cites, start=offset, num=20)
                batch = response.get('organic_results', [])
                before = len(results)
                for item in batch:
                    # Scholar result ID identifies the actual result. Do not collapse
                    # distinct Scholar results by similar titles during collection.
                    rid = item.get('result_id') or item.get('link') or item.get('title')
                    if rid not in seen:
                        seen.add(rid)
                        results.append(item)
                if not batch:
                    end_reason = 'empty_page'
                    break
                if len(results) == before:
                    end_reason = 'repeated_page'
                    break
                if not (response.get('serpapi_pagination') or {}).get('next'):
                    end_reason = 'last_page'
                    break
            else:
                end_reason = 'scholar_1000_limit'
        output.append({'paper': paper, 'detail': detail, 'displayed': displayed,
                       'cites_id': cites, 'retrieved': len(results),
                       'end_reason': end_reason, 'citing': results})
        print(f"{i+1}/{len(papers)}: {len(results)}/{displayed} | {paper['title']}", flush=True)
    result = {'author_id': author_id, 'author': profile.get('author'),
              'snapshot_date': __import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo(os.environ.get('REPORT_TIMEZONE', 'America/New_York'))).date().isoformat(), 'papers': output,
              'new_serpapi_requests': api.requests}
    (Path(root) / 'collected.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--author-id', required=True)
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--request-budget', type=int, default=40)
    args = parser.parse_args()
    collect(args.author_id, args.data_dir, args.request_budget)
