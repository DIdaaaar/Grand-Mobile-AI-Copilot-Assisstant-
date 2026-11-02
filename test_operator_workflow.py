"""Regression tests for the locally persisted operator workflow (v0.5)."""
import os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from fastapi.testclient import TestClient
from app import main

class OperatorWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir=tempfile.TemporaryDirectory()
        self.db_path=str(Path(self.tempdir.name)/'isolated.sqlite3')
        self.patch=patch.object(main,'DB_PATH',self.db_path)
        self.patch.start()
        self.auth_patch=patch.object(main,'AUTH_MODE','off');self.auth_patch.start()
        self.client=TestClient(main.app)
    def tearDown(self):
        self.client.close();self.auth_patch.stop();self.patch.stop();self.tempdir.cleanup()
    def make(self,kind='report',text='Застрял возле больницы, нужна помощь'):
        return self.client.post('/triage',json={'kind':kind,'messages':[{'role':'player','text':text}], 'save_to_queue':True})
    def test_case_is_persisted_and_listed(self):
        resp=self.make()
        self.assertEqual(resp.status_code,200,resp.text)
        case_id=resp.json()['case_id']
        cases=self.client.get('/cases').json()['cases']
        self.assertEqual(len(cases),1)
        self.assertEqual(cases[0]['id'],case_id)
        self.assertEqual(cases[0]['status'],'new')
        self.assertEqual(cases[0]['text'],'Застрял возле больницы, нужна помощь')
        self.assertEqual(len(self.client.get('/cases/'+case_id).json()['events']),1)
    def test_ordinary_triage_does_not_fill_queue(self):
        resp=self.client.post('/triage',json={'kind':'report','messages':[{'role':'player','text':'Как играть?'}]})
        self.assertEqual(resp.status_code,200)
        self.assertNotIn('case_id',resp.json())
        self.assertEqual(self.client.get('/cases').json()['count'],0)
    def test_operator_edits_and_status_history(self):
        case_id=self.make().json()['case_id']
        response=self.client.patch('/cases/'+case_id,json={'status':'in_progress','draft_reply':'Здравствуйте! Пожалуйста, уточните сервер.'})
        self.assertEqual(response.status_code,200,response.text)
        case=response.json()['case']
        self.assertEqual(case['status'],'in_progress')
        self.assertIn('уточните сервер',case['draft_reply'])
        events=self.client.get('/cases/'+case_id).json()['events']
        self.assertEqual(len(events),3)
        self.assertEqual(self.client.get('/stats').json()['by_status']['in_progress'],1)
    def test_filters_and_rule_search(self):
        self.make();self.make('complaint','Игрок использует читы и летает через стены')
        cases=self.client.get('/cases?q=читы').json()['cases']
        self.assertEqual(len(cases),1)
        self.assertEqual(self.client.get('/cases?status=resolved').json()['count'],0)
        self.assertEqual(self.client.get('/cases?status=not_a_status').status_code,400)
        rules=self.client.get('/rules?q=оскорбление').json()
        self.assertGreater(rules['count'],0)
        for rule in rules['rules']:
            self.assertIn(rule['ref'],main.RULES)
    def test_critical_priority_cannot_be_downgraded(self):
        res=self.make('report','Оплатил донат, деньги списали, голда не пришла').json()
        self.assertEqual(res['priority'],'p0')
        self.assertTrue(res['needs_human'])
        case_id=res['case_id']
        self.assertEqual(self.client.patch('/cases/'+case_id,json={'priority':'p3'}).status_code,400)
        self.assertEqual(self.client.get('/cases/'+case_id).json()['case']['priority'],'p0')
        self.assertEqual(self.client.patch('/cases/'+case_id,json={'status':'escalated'}).status_code,200)
    def test_delete_removes_case_and_history(self):
        case_id=self.make().json()['case_id']
        self.assertEqual(self.client.delete('/cases/'+case_id).status_code,200)
        self.assertEqual(self.client.get('/cases/'+case_id).status_code,404)
        self.assertEqual(self.client.get('/cases').json()['count'],0)
        self.assertEqual(self.client.delete('/cases/'+case_id).status_code,404)
    def test_invalid_mutations_rejected(self):
        case_id=self.make().json()['case_id']
        self.assertEqual(self.client.patch('/cases/'+case_id,json={'status':'sending_to_game'}).status_code,400)
        self.assertEqual(self.client.patch('/cases/'+case_id,json={'draft_reply':'  '}).status_code,400)
        self.assertEqual(self.client.patch('/cases/'+case_id,json={'priority':'p7'}).status_code,400)
    def test_batch_import_creates_local_queue_cases(self):
        items=[{'kind':'report','messages':[{'role':'player','text':'Помогите с управлением'}]},
               {'kind':'complaint','messages':[{'role':'player','text':'Игрок оскорбляет меня, 2.1 ОП'}]}]
        res=self.client.post('/cases/batch',json={'items':items})
        self.assertEqual(res.status_code,200,res.text)
        self.assertEqual(res.json()['created'],2)
        self.assertEqual(self.client.get('/cases').json()['count'],2)
        self.assertEqual(self.client.post('/cases/batch',json={'items':[]}).status_code,422)
    def test_origin_whitelist(self):
        allowed=self.client.options('/triage',headers={'origin':'http://localhost:5173','access-control-request-method':'POST'})
        self.assertEqual(allowed.headers.get('access-control-allow-origin'),'http://localhost:5173')
        blocked=self.client.options('/triage',headers={'origin':'https://example.org','access-control-request-method':'POST'})
        self.assertNotEqual(blocked.headers.get('access-control-allow-origin'),'https://example.org')
    def test_stats_count_all_triages_and_only_saved_cases(self):
        self.make()
        self.client.post('/triage',json={'kind':'report','messages':[{'role':'player','text':'Другой вопрос'}]})
        st=self.client.get('/stats').json()
        self.assertEqual(st['processed'],2)
        self.assertEqual(st['cases_total'],1)

if __name__=='__main__':unittest.main()
