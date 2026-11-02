"""Release-candidate tests: full operator workflow, safe migration and optional auth."""
import sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi.testclient import TestClient
from app import main

class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(main,'DB_PATH',str(Path(self.tmp.name)/'cases.sqlite3'))
        self.db_patch.start()
        self.auth_patch=patch.object(main,'AUTH_MODE','off');self.auth_patch.start()
        self.client=TestClient(main.app)
    def tearDown(self):
        self.client.close();self.auth_patch.stop();self.db_patch.stop();self.tmp.cleanup()
    def create(self,text,kind='report'):
        res=self.client.post('/triage',json={'kind':kind,'messages':[{'role':'player','text':text}],'save_to_queue':True})
        self.assertEqual(res.status_code,200,res.text)
        return res.json()['case_id']
    def test_operator_assignment_notes_and_event_history(self):
        case_id=self.create('Застрял в текстурах около больницы')
        resp=self.client.patch('/cases/'+case_id,json={'assignee':'operator-1'})
        self.assertEqual(resp.status_code,200,resp.text)
        self.assertEqual(resp.json()['case']['assignee'],'operator-1')
        resp=self.client.post('/cases/'+case_id+'/notes',json={'text':'Проверил координаты на сервере'})
        self.assertEqual(resp.status_code,200,resp.text)
        events=self.client.get('/cases/'+case_id).json()['events']
        self.assertIn('note',[x['action'] for x in events])
        self.assertEqual(self.client.post('/cases/'+case_id+'/notes',json={'text':' '}).status_code,422)
    def test_human_case_needs_resolution_note(self):
        case_id=self.create('Списали деньги, донат не пришёл')
        self.assertEqual(self.client.patch('/cases/'+case_id,json={'status':'resolved'}).status_code,400)
        r=self.client.patch('/cases/'+case_id,json={'resolution_note':'Проверено человеком, запрос передан платёжной службе', 'status':'resolved'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['case']['status'],'resolved')
        self.assertIn('resolution_note',[x['action'] for x in self.client.get('/cases/'+case_id).json()['events']])
    def test_bulk_action_is_atomic_and_audited(self):
        a=self.create('Как восстановить настройки интерфейса?')
        b=self.create('Помогите разобраться с картой')
        r=self.client.post('/cases/bulk',json={'case_ids':[a,b],'assignee':'shift-A'})
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['updated'],2)
        for case_id in (a,b):
            self.assertEqual(self.client.get('/cases/'+case_id).json()['case']['assignee'],'shift-A')
        self.assertEqual(self.client.post('/cases/bulk',json={'case_ids':[a,'missing'],'status':'resolved'}).status_code,404)
        self.assertEqual(self.client.get('/cases/'+a).json()['case']['status'],'new')
        self.assertEqual(self.client.post('/cases/bulk',json={'case_ids':[a,b],'status':'in_progress'}).status_code,200)
        self.assertEqual(self.client.post('/cases/bulk',json={'case_ids':[a,b],'status':'invalid'}).status_code,400)
    def test_similar_ticket_ranking(self):
        a=self.create('Игрок летает через стены, использует читы и аим')
        b=self.create('Игрок летает через стены использует читы и аим')
        self.create('Сколько стоит аренда дома в игре?')
        r=self.client.get('/cases/'+a+'/similar')
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['matches'][0]['id'],b)
        self.assertGreater(r.json()['matches'][0]['similarity'],0.5)
        self.assertEqual(self.client.get('/cases/missing/similar').status_code,404)
    def test_jsonl_export_and_stats(self):
        case_id=self.create('Застрял на сервере, помогите выйти')
        resp=self.client.get('/cases/export/jsonl')
        self.assertEqual(resp.status_code,200,resp.text)
        import json
        self.assertEqual(json.loads(resp.text.strip())['id'],case_id)
        self.assertIn('application/x-ndjson',resp.headers['content-type'])
        st=self.client.get('/stats').json()
        self.assertIn('human_pending',st)
        self.assertIn('by_assignee',st)
    def test_optional_token_protection(self):
        with patch.object(main,'API_TOKEN','topsecret'):
            self.assertEqual(self.client.get('/cases').status_code,401)
            self.assertEqual(self.client.get('/health').status_code,200)
            self.assertEqual(self.client.get('/cases',headers={'X-Copilot-Token':'wrong'}).status_code,401)
            self.assertEqual(self.client.get('/cases',headers={'X-Copilot-Token':'topsecret'}).status_code,200)
            self.assertEqual(self.client.post('/triage',json={'kind':'report','messages':[]}).status_code,401)
    def test_migrates_existing_case_table_without_loss(self):
        import sqlite3
        con=sqlite3.connect(main.DB_PATH)
        con.execute('''CREATE TABLE cases (id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL,
        attachments INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'new', category TEXT,
        priority TEXT, lang TEXT, confidence REAL, needs_human INTEGER, draft_reply TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        con.execute("INSERT INTO cases(id,kind,text,created_at,updated_at) VALUES('old','report','сохранённое обращение','2026-01-01','2026-01-01')")
        con.commit();con.close()
        r=self.client.get('/cases/old')
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['case']['text'],'сохранённое обращение')
        self.assertEqual(r.json()['case']['assignee'],'')

if __name__=='__main__':unittest.main()
