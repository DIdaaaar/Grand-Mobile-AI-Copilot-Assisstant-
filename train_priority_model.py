#!/usr/bin/env python3
"""Priority model trained with weak labels, not independently verified ground truth.

Human-safe deterministic overrides in app/main.py always take precedence.
"""
from __future__ import annotations
import argparse, gzip, json, re
from collections import Counter
from pathlib import Path
import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.pipeline import FeatureUnion
from sklearn.svm import LinearSVC
from sklearn.model_selection import GroupShuffleSplit
from sklearn.metrics import accuracy_score, f1_score, classification_report

def norm(s):
    s=(s or "").lower().replace("ё","е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.]+"," ",s)).strip()

def text_from(row):
    return " ".join((m.get("text") or "") for m in row.get("messages",[])
                    if m.get("role") in ("player","reported_player"))

def weak_label(row, raw):
    t=norm(raw)
    if not t: return None
    if re.search(r"взлом|угнал.{0,12}аккаунт|потерял.{0,10}доступ|не могу войти|донат.{0,20}не приш|оплатил|списал.{0,12}деньг|угрож.{0,20}реальн|массов.{0,8}баг|hacked account|payment issue",t):
        return "p0"
    hints=norm(str(row.get("violation") or "")+" "+str(row.get("rule_refs_raw") or ""))
    if re.search(r"чит|аимбот|aimbot|wallhack|скам|мошенн|rmt|реал.?мани|продаж.{0,12}аккаунт",t+" "+hints):
        return "p1"
    if row.get("kind") == "complaint":
        if int(row.get("images_count") or 0)>0: return "p1"
        if re.search(r"оскорб|мат\b|дм\b|dm\b|убил без|админ|администратор|жалоб.{0,15}на админ",t):
            return "p2"
        return "p3"
    if re.search(r"оскорб|дм\b|dm\b|убили без|наруш|правил|жалоб|админ",t):
        return "p2"
    return "p3"

def features(row, raw):
    evidence=int(row.get("images_count") or 0)>0 or any(int(m.get("attachments") or 0)>0 for m in row.get("messages",[]))
    return f"kind_{row.get('kind','report')} evidence_{int(evidence)} "+norm(raw)

def load(path):
    rows=[]
    op=gzip.open if str(path).endswith(".gz") else open
    with op(path,"rt",encoding="utf-8") as f:
        for line in f:
            if not line.strip():continue
            row=json.loads(line)
            raw=text_from(row)
            label=weak_label(row,raw)
            if label is None:continue
            rows.append((features(row,raw),label,row.get("reporter_hash") or row.get("ticket_id")))
    return rows

def train(input_path,output_dir):
    rows=load(input_path)
    texts=[r[0] for r in rows]; labels=[r[1] for r in rows]; groups=[r[2] for r in rows]
    splitter=GroupShuffleSplit(test_size=.2,n_splits=1,random_state=42)
    a,b=next(splitter.split(texts,labels,groups))
    vec=FeatureUnion([
        ("word",TfidfVectorizer(ngram_range=(1,2),min_df=3,max_features=75000,sublinear_tf=True,token_pattern=r"(?u)\b\w+\b")),
        ("char",TfidfVectorizer(analyzer="char_wb",ngram_range=(3,5),min_df=3,max_features=85000,sublinear_tf=True)),
    ])
    xtrain=vec.fit_transform([texts[i] for i in a])
    xtest=vec.transform([texts[i] for i in b])
    clf=LinearSVC(C=.9,class_weight="balanced",max_iter=3500)
    clf.fit(xtrain,[labels[i] for i in a])
    preds=clf.predict(xtest)
    metadata={
       "model":"TF-IDF word+char ngrams and LinearSVC (weak priority labels)",
       "training_rows":len(rows),"label_counts":dict(Counter(labels)),
       "weak_label_accuracy":float(accuracy_score([labels[i] for i in b],preds)),
       "weak_label_macro_f1":float(f1_score([labels[i] for i in b],preds,average="macro",zero_division=0)),
       "classification_report":classification_report([labels[i] for i in b],preds,output_dict=True,zero_division=0),
       "split":"GroupShuffleSplit by reporter_hash, random_state 42, 20% held out",
       "label_quality_note":"Priority targets are heuristic weak labels from complaint type, text, evidence, and administrative hints. Metrics are agreement with weak labels, NOT expert verification or challenge benchmark.",
       "security_note":"Critical p0 routing, human escalation and safe replies are deterministic and override model predictions."
    }
    del xtrain,xtest
    xall=vec.fit_transform(texts)
    clf.fit(xall,labels)
    out=Path(output_dir);out.mkdir(parents=True,exist_ok=True)
    joblib.dump({"vectorizer":vec,"model":clf},out/"priority_model.joblib",compress=3)
    with open(out/"priority_model_metadata.json","w",encoding="utf8") as f:
        json.dump(metadata,f,ensure_ascii=False,indent=2)
    return metadata

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--input",required=True)
    p.add_argument("--output-dir",default="backend/models")
    args=p.parse_args()
    meta=train(args.input,args.output_dir)
    print(json.dumps({k:meta[k] for k in ("training_rows","label_counts","weak_label_accuracy","weak_label_macro_f1")},ensure_ascii=False,indent=2))
