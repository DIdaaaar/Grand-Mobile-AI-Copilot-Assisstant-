#!/usr/bin/env python3
"""Evaluate model predictions against manually completed annotation CSV."""
import argparse, csv, json, sys
from pathlib import Path
from sklearn.metrics import accuracy_score, f1_score, classification_report, confusion_matrix

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.main import classify, priority_for, normalize


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True, help='Completed manual_review_sample.csv')
    ap.add_argument('--output', default='manual_evaluation.json')
    args = ap.parse_args()
    category_true, category_pred, priority_true, priority_pred = [], [], [], []
    skipped = 0
    with open(args.input, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            category, priority = row.get('category', '').strip(), row.get('priority', '').strip()
            if not category or not priority:
                skipped += 1
                continue
            kind = row.get('kind', 'report')
            text = row.get('text', '')
            pred_cat, _, _ = classify(kind, text)
            pred_pri = priority_for(pred_cat, text, int(row.get('images_count') or 0) > 0)
            category_true.append(category); category_pred.append(pred_cat)
            priority_true.append(priority); priority_pred.append(pred_pri)
    if not category_true:
        raise SystemExit('No completed labels found. Fill category and priority columns first.')
    result = {
        'evaluated_rows': len(category_true),
        'skipped_unlabeled_rows': skipped,
        'category': {
            'accuracy': accuracy_score(category_true, category_pred),
            'macro_f1': f1_score(category_true, category_pred, average='macro', zero_division=0),
            'report': classification_report(category_true, category_pred, output_dict=True, zero_division=0),
            'confusion_matrix': confusion_matrix(category_true, category_pred, labels=sorted(set(category_true) | set(category_pred))).tolist(),
            'labels': sorted(set(category_true) | set(category_pred))
        },
        'priority': {
            'accuracy': accuracy_score(priority_true, priority_pred),
            'macro_f1': f1_score(priority_true, priority_pred, average='macro', zero_division=0),
            'report': classification_report(priority_true, priority_pred, output_dict=True, zero_division=0)
        },
        'note': 'Scores use human-entered labels in this worksheet; do not mix this sample into training if it is reserved as a holdout.'
    }
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('evaluated_rows','skipped_unlabeled_rows','category','priority') if k != 'category' and k != 'priority'}, ensure_ascii=False, indent=2))
    print('Category accuracy:', round(result['category']['accuracy'], 4), 'macro-F1:', round(result['category']['macro_f1'], 4))
    print('Priority accuracy:', round(result['priority']['accuracy'], 4), 'macro-F1:', round(result['priority']['macro_f1'], 4))
    print('Saved:', args.output)

if __name__ == '__main__':
    main()
