#!/usr/bin/env python3
"""Export a reproducible, stratified manual-labeling worksheet from valid JSONL.

The worksheet intentionally leaves category and priority blank. Never use the
model's predictions as ground truth; reviewers must label from the ticket text
and the competition taxonomy.
"""
import argparse, csv, json, random
from collections import defaultdict
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True, help='tickets_valid.jsonl')
    ap.add_argument('--output', default='manual_review_sample.csv')
    ap.add_argument('--n', type=int, default=300)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    groups = defaultdict(list)
    with open(args.input, encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            player_text = ' '.join(m.get('text', '') for m in row.get('messages', [])
                                   if m.get('role') in ('player', 'reported_player'))
            groups[(row.get('kind', 'report'), row.get('lang', 'und'))].append({
                'ticket_id': row.get('ticket_id', ''),
                'kind': row.get('kind', 'report'),
                'lang': row.get('lang', 'und'),
                'server_lang': row.get('server_lang', ''),
                'text': player_text,
                'images_count': row.get('images_count', 0) or 0,
                'category': '',
                'priority': '',
                'reviewer_notes': ''
            })
    total = sum(map(len, groups.values()))
    if total == 0:
        raise SystemExit('No records found in input')
    # Proportional allocation, then distribute rounding remainder.
    allocations = {key: int(args.n * len(rows) / total) for key, rows in groups.items()}
    remainder = min(args.n, total) - sum(allocations.values())
    for key in sorted(groups, key=lambda k: len(groups[k]) / total - allocations[k] / max(args.n, 1), reverse=True)[:remainder]:
        allocations[key] += 1
    selected = []
    for key, rows in groups.items():
        rng.shuffle(rows)
        selected.extend(rows[:min(allocations[key], len(rows))])
    rng.shuffle(selected)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ['ticket_id','kind','lang','server_lang','text','images_count','category','priority','reviewer_notes']
    with out.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(selected)
    print(json.dumps({'output': str(out), 'rows': len(selected), 'source_rows': total,
                      'strata': {f'{k[0]}|{k[1]}': allocations[k] for k in sorted(allocations)}},
                     ensure_ascii=False, indent=2))

if __name__ == '__main__':
    main()
