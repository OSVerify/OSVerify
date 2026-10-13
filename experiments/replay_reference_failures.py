"""Replay a stratified sample of frozen failed fetches into a new directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

from osverifier.storage import atomic_json
from osverifier_reports.references import ReferenceStore


def choose(rows):
    budgets = {'not_pdf_or_tex': 30, 'subscription_or_permission': 4,
               'timeout': 4, 'tls_certificate': 4, 'http_404': 3}
    selected, used = [], set()
    groups = defaultdict(list)
    for row in rows:
        if row['category'] != 'available':
            groups[row['category']].append(row)
    for category, values in sorted(groups.items()):
        domains = defaultdict(list)
        for row in sorted(values, key=lambda r: r['url']):
            if row['url'] not in used:
                domains[urlsplit(row['url']).hostname].append(row)
        n = 0
        while domains and n < budgets.get(category, len(values)):
            for domain in sorted(list(domains)):
                row = domains[domain].pop(0)
                if row['url'] not in used:
                    selected.append(row)
                    used.add(row['url'])
                    n += 1
                if not domains[domain]:
                    del domains[domain]
                if n >= budgets.get(category, len(values)):
                    break
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    original = json.loads(args.manifest.read_text())
    sample = choose(original['receipts'])
    output = args.output.resolve()
    for run in original['runs']:
        if output.is_relative_to(Path(run).resolve()):
            raise ValueError('Output must be outside every historical run')
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output/'frozen-failure-sample.json', {
        'source_manifest': str(args.manifest.resolve()),
        'source_manifest_sha256': hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        'selection': 'Domain round-robin by failure category: 30 format, 4 access, 4 timeout, 4 TLS, 3 404, all others. Unique URLs.',
        'samples': sample})
    code = Path(__file__).resolve().parents[1]/'src/osverifier_reports'
    atomic_json(output/'implementation.json', {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in code.glob('reference*.py')})
    store = ReferenceStore(output/'references')
    started = time.monotonic()

    def run(row):
        begin = time.monotonic()
        result = store.fetch(row['url'])
        html = result.get('html_path')
        answer = {'url': row['url'], 'old_category': row['category'],
                  'old_receipt_path': row['receipt_path'], 'old_receipt_sha256': row['receipt_sha256'],
                  'status': result['status'], 'access_status': result.get('access_status'),
                  'reason': result['reason'], 'format': result.get('format'),
                  'resolved_url': result.get('resolved_url'),
                  'same_response_old_format_rejects': bool(html and '<html' in Path(html).read_text().lower()),
                  'landing_hops': sum(r.get('html_kind') == 'landing_page' for r in result['retrievals']),
                  'elapsed_seconds': round(time.monotonic()-begin, 3),
                  'receipt': str(store.directory/hashlib.sha256(row['url'].encode()).hexdigest()/'receipt.json')}
        atomic_json(output/'samples'/(hashlib.sha256(row['url'].encode()).hexdigest()+'.json'), answer)
        return answer

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run, row) for row in sample]
        for future in as_completed(futures):
            value = future.result()
            results.append(value)
            print(json.dumps({k: value[k] for k in ('url','status','reason')}, ensure_ascii=False), flush=True)
    changed = [r['receipt_path'] for r in original['receipts'] if hashlib.sha256(Path(r['receipt_path']).read_bytes()).hexdigest() != r['receipt_sha256']]
    per_category = {}
    for category in sorted({r['old_category'] for r in results}):
        group = [r for r in results if r['old_category'] == category]
        per_category[category] = {'tested': len(group), 'available': sum(r['status'] == 'available' for r in group)}
    summary = {'scope': 'Retrieval recovery, not mathematical verification. Historical and live network conditions differ.',
               'tested': len(results), 'available': sum(r['status'] == 'available' for r in results),
               'same_response_old_format_rejects': sum(r['same_response_old_format_rejects'] for r in results),
               'recovered_via_landing': sum(r['landing_hops'] > 0 and r['status'] == 'available' for r in results),
               'elapsed_seconds': round(time.monotonic()-started, 2), 'per_category': per_category,
               'status_counts': dict(Counter(r['access_status'] for r in results)),
               'original_receipts_checked': len(original['receipts']), 'changed_original_receipts': changed,
               'results': sorted(results, key=lambda r: r['url'])}
    atomic_json(output/'summary.json', summary)
    print(json.dumps({k: v for k,v in summary.items() if k != 'results'}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
