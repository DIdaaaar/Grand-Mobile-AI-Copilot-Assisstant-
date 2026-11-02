import argparse, json
from .main import TriageRequest, triage

def main():
    p=argparse.ArgumentParser(); p.add_argument('--input',required=True); p.add_argument('--output',required=True); a=p.parse_args()
    n=0
    with open(a.input,encoding='utf-8') as src, open(a.output,'w',encoding='utf-8') as dst:
        for line in src:
            if not line.strip(): continue
            row=json.loads(line)
            req=TriageRequest(**{k:v for k,v in row.items() if k in TriageRequest.model_fields})
            result=triage(req)
            dst.write(json.dumps({'ticket_id':row.get('ticket_id'),**result},ensure_ascii=False)+'\n'); n+=1
    print(f'Wrote {n} predictions to {a.output}')
if __name__=='__main__': main()
