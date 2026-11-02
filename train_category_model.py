#!/usr/bin/env python3
"""Train complaint category model from Grand Mobile JSONL.GZ.

Important: category labels are weakly inferred from admin-side fields
(violation, rule_refs_raw, verdict). Report metrics are agreement with these
weak labels, NOT ground-truth category accuracy. Do not present them as
official benchmark scores.
"""
from __future__ import annotations
import argparse, gzip, json, re, os
from collections import Counter
from pathlib import Path

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.svm import LinearSVC
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import accuracy_score, f1_score, classification_report
import joblib


def norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.]+", " ", s, flags=re.UNICODE)).strip()


def player_text(row: dict) -> str:
    return " ".join(m.get("text", "") for m in row.get("messages", [])
                    if m.get("role") in ("player", "reported_player"))


def weak_label(row: dict) -> str:
    text = norm(player_text(row))
    admin_hints = norm(str(row.get("violation") or "") + " " + str(row.get("rule_refs_raw") or ""))
    t = text + " " + admin_hints
    def has(pattern: str) -> bool:
        return bool(re.search(pattern, t, re.I))
    if has(r"\b5\.2\b|чит|аим|aimbot|wallhack|софт|flyhack"):
        return "complaint.cheat"
    if has(r"\b4\.[12]\b|скам|мошенн|обман|не вернул|не отдал|вымог"):
        return "complaint.scam"
    if has(r"\b(2\.3|4\.4)\b|rmt|реал.?мани|продаж.{0,20}(аккаунт|голд)|аккаунт.{0,20}продаж|за реальные деньги"):
        return "complaint.rmt_ads"
    if has(r"\b3\.4\b|админ.{0,25}(наруш|выдал|оскорб|неправ)|жалоб.{0,15}на админ"):
        return "complaint.admin"
    if has(r"\b(5\.3|5\.4|1\.1)\b|дм\b|db\b|дмят|убил без|убийств без|стрелял без|зелен.{0,10}зон"):
        return "complaint.dm"
    if has(r"\b(2\.1|2\.4|2\.6)\b|оскорб|оск\b|мат\b|родн|токсич|расизм"):
        return "complaint.insult"
    if row.get("verdict") == "invalid_format":
        return "complaint.invalid"
    return "complaint.other"


def load_rows(path: str):
    rows = []
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") != "complaint":
                continue
            text = norm(player_text(row))
            if not text:
                continue
            rows.append((text, weak_label(row), row.get("reporter_hash") or row.get("ticket_id")))
    return rows


def make_vectorizer():
    return FeatureUnion([
        ("word", TfidfVectorizer(ngram_range=(1, 2), min_df=3,
                                 max_features=90000, sublinear_tf=True,
                                 token_pattern=r"(?u)\b\w+\b")),
        ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5),
                                 min_df=3, max_features=120000,
                                 sublinear_tf=True)),
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="tickets_train.jsonl or .jsonl.gz")
    ap.add_argument("--output-dir", default="backend/models")
    args = ap.parse_args()
    rows = load_rows(args.input)
    texts = [x[0] for x in rows]
    labels = [x[1] for x in rows]
    groups = [x[2] for x in rows]
    splitter = GroupShuffleSplit(n_splits=1, test_size=.2, random_state=42)
    train_idx, test_idx = next(splitter.split(texts, labels, groups))

    vectorizer = make_vectorizer()
    x_train = vectorizer.fit_transform([texts[i] for i in train_idx])
    x_test = vectorizer.transform([texts[i] for i in test_idx])
    model = LinearSVC(C=1.2, class_weight="balanced", max_iter=6000)
    model.fit(x_train, [labels[i] for i in train_idx])
    predicted = model.predict(x_test)
    report = classification_report([labels[i] for i in test_idx], predicted,
                                   output_dict=True, zero_division=0)

    # Refit on all available training records before shipping the artifact.
    x_all = vectorizer.fit_transform(texts)
    model.fit(x_all, labels)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump({"vectorizer": vectorizer, "model": model},
                out / "complaint_category.joblib", compress=3)
    metadata = {
        "model": "TF-IDF word+char n-grams + LinearSVC",
        "training_rows": len(rows),
        "label_counts": dict(Counter(labels)),
        "split": "GroupShuffleSplit by reporter_hash, test_size=0.2, random_state=42",
        "weak_label_accuracy": accuracy_score([labels[i] for i in test_idx], predicted),
        "weak_label_macro_f1": f1_score([labels[i] for i in test_idx], predicted, average="macro"),
        "classification_report": report,
        "label_quality_note": "Weak labels inferred from admin-side violation, rule_refs_raw and verdict. Metrics measure agreement with noisy inferred labels, not official ground-truth category accuracy. Validate with a manually annotated holdout before claiming benchmark performance.",
        "privacy_note": "Training data is not copied into the model package; keep raw ticket data private and delete it according to the competition rules."
    }
    with open(out / "model_metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: metadata[k] for k in ("training_rows", "label_counts", "weak_label_accuracy", "weak_label_macro_f1")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
