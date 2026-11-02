"""Identity and role access tests, run with isolated real SQLite files."""
import sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi.testclient import TestClient
from app import main

class AccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(main,'DB_PATH',str(Path(self.tmp.name)/'accounts.sqlite3'))
        self.db_patch.start()
        self.auth_patch=patch.object(main,'AUTH_MODE','auto')
        self.auth_patch.start()
        self.api_patch=patch.object(main,'API_TOKEN','')
        self.api_patch.start()
        main.LOGIN_ATTEMPTS.clear()
        self.client=TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.api_patch.stop();self.auth_patch.stop();self.db_patch.stop();self.tmp.cleanup()

    def post(self,path,body,headers=None):
        return self.client.post(path,json=body,headers=headers or {})

    def admin(self):
        r=self.post('/auth/setup',{'username':'root','full_name':'Первый администратор','password':'VerySafePassword123!'})
        self.assertEqual(r.status_code,200,r.text)
        return {'Authorization':'Bearer '+r.json()['access_token']}

    def test_email_is_valid_admin_login_and_password_is_not_filtered(self):
        email = 'operator.team+demo@example.com'
        pw = 'LongPasswordWithSymbols!2026'
        created = self.post('/auth/setup', {'username':email, 'full_name':'Didar', 'password':pw})
        self.assertEqual(created.status_code,200,created.text)
        self.assertEqual(created.json()['user']['username'],email)
        logged = self.post('/auth/login',{'username':email.upper(),'password':pw})
        self.assertEqual(logged.status_code,200,logged.text)
        who = self.client.get('/auth/me',headers={'Authorization':'Bearer '+logged.json()['access_token']})
        self.assertEqual(who.status_code,200,who.text)

    def test_rejects_malformed_username_without_internal_regex_message(self):
        for bad in ['some name','bad@','@@@']:
            out=self.post('/auth/setup', {'username':bad,'full_name':'Didar','password':'ThisIsLongEnough2026!'})
            self.assertEqual(out.status_code,422,out.text)

    def test_fresh_install_is_locked_until_admin_setup(self):
        with patch.object(main,'AUTH_MODE','on'):
            status=self.client.get('/auth/status')
            self.assertTrue(status.json()['enabled'])
            self.assertTrue(status.json()['setup_required'])
            self.assertEqual(self.client.get('/cases').status_code,401)
            self.assertEqual(self.post('/triage',{'messages':[]}).status_code,401)
            self.assertEqual(self.client.get('/health').status_code,200)

    def test_first_admin_creation_turns_on_protected_mode(self):
        self.assertTrue(self.client.get('/auth/status').json()['setup_required'])
        admin=self.admin()
        self.assertFalse(self.client.get('/auth/status').json()['setup_required'])
        self.assertEqual(self.client.get('/cases').status_code,401)
        self.assertEqual(self.client.get('/cases',headers=admin).status_code,200)
        self.assertEqual(self.client.get('/health').status_code,200)
        self.assertEqual(self.post('/auth/setup',{'username':'other','full_name':'Extra','password':'abcdefgh1234567'}).status_code,409)
        self.assertEqual(self.client.get('/auth/me',headers=admin).json()['user']['role'],'admin')

    def test_login_wrong_password_and_logout_revoke_token(self):
        admin=self.admin()
        bad=self.post('/auth/login',{'username':'root','password':'wrong'})
        self.assertEqual(bad.status_code,401)
        logged=self.post('/auth/login',{'username':'ROOT','password':'VerySafePassword123!'})
        self.assertEqual(logged.status_code,200,logged.text)
        valid={'Authorization':'Bearer '+logged.json()['access_token']}
        self.assertEqual(self.client.get('/auth/me',headers=valid).status_code,200)
        self.assertEqual(self.client.post('/auth/logout',headers=valid).status_code,200)
        self.assertEqual(self.client.get('/auth/me',headers=valid).status_code,401)
        self.assertEqual(self.client.get('/cases',headers=admin).status_code,200)

    def test_profile_preferences_persist_and_password_revokes_all_sessions(self):
        admin=self.admin()
        p=self.client.patch('/auth/profile',json={'full_name':'Новое имя','email':'operator@example.com'},headers=admin)
        self.assertEqual(p.status_code,200,p.text)
        self.assertEqual(p.json()['user']['full_name'],'Новое имя')
        self.assertEqual(self.client.patch('/auth/profile',json={'full_name':'Новое имя','email':'not_an_email'},headers=admin).status_code,422)
        prefs={'theme':'light','compact_queue':True,'auto_refresh':False,'notifications':False}
        r=self.client.patch('/auth/preferences',json=prefs,headers=admin)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/auth/me',headers=admin).json()['user']['preferences'],prefs)
        self.assertEqual(self.post('/auth/change-password',{'current_password':'wrong','new_password':'EvenBetterPassword456!'},admin).status_code,401)
        r=self.post('/auth/change-password',{'current_password':'VerySafePassword123!','new_password':'EvenBetterPassword456!'},admin)
        self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(self.client.get('/auth/me',headers=admin).status_code,401)
        self.assertEqual(self.post('/auth/login',{'username':'root','password':'VerySafePassword123!'}).status_code,401)
        self.assertEqual(self.post('/auth/login',{'username':'root','password':'EvenBetterPassword456!'}).status_code,200)

    def test_role_authorization_and_deactivation(self):
        admin=self.admin()
        self.assertEqual(self.post('/auth/users',{'username':'ops','full_name':'Оператор','password':'StrongOperator123','role':'operator'},admin).status_code,200)
        viewer=self.post('/auth/users',{'username':'viewer','full_name':'Наблюдатель','password':'StrongViewer1234','role':'viewer'},admin).json()['user']
        ops_token=self.post('/auth/login',{'username':'ops','password':'StrongOperator123'}).json()['access_token']
        op={'Authorization':'Bearer '+ops_token}
        viewer_token=self.post('/auth/login',{'username':'viewer','password':'StrongViewer1234'}).json()['access_token']
        v={'Authorization':'Bearer '+viewer_token}
        self.assertEqual(self.client.get('/cases',headers=v).status_code,200)
        self.assertEqual(self.post('/triage',{'messages':[{'role':'player','text':'hello'}]},v).status_code,403)
        self.assertEqual(self.client.get('/auth/users',headers=op).status_code,403)
        made=self.post('/triage',{'messages':[{'role':'player','text':'застрял в текстурах'}],'save_to_queue':True},op)
        self.assertEqual(made.status_code,200,made.text)
        cid=made.json()['case_id']
        self.assertEqual(self.client.delete('/cases/'+cid,headers=op).status_code,403)
        self.assertEqual(self.client.delete('/cases/'+cid,headers=admin).status_code,200)
        disabled=self.client.patch('/auth/users/'+viewer['id'],json={'active':False},headers=admin)
        self.assertEqual(disabled.status_code,200,disabled.text)
        self.assertEqual(self.client.get('/auth/me',headers=v).status_code,401)
        self.assertEqual(self.post('/auth/login',{'username':'viewer','password':'StrongViewer1234'}).status_code,401)
        self.assertEqual(self.client.patch('/auth/users/'+self.client.get('/auth/me',headers=admin).json()['user']['id'],json={'role':'viewer'},headers=admin).status_code,400)

    def test_case_audit_marks_real_operator(self):
        admin=self.admin()
        item=self.post('/triage',{'kind':'report','messages':[{'role':'player','text':'застрял в текстурах'}],'save_to_queue':True},admin)
        self.assertEqual(item.status_code,200,item.text)
        cid=item.json()['case_id']
        note=self.post('/cases/'+cid+'/notes',{'text':'Проверено администратором'},admin)
        self.assertEqual(note.status_code,200,note.text)
        events=self.client.get('/cases/'+cid,headers=admin).json()['events']
        self.assertTrue(all(e.get('actor')=='root' for e in events),events)

    def test_logout_all_and_activity(self):
        admin=self.admin()
        other=self.post('/auth/login',{'username':'root','password':'VerySafePassword123!'})
        second={'Authorization':'Bearer '+other.json()['access_token']}
        item=self.post('/triage',{'messages':[{'role':'player','text':'Как вернуться в игру?'}],'save_to_queue':True},admin)
        self.assertEqual(item.status_code,200,item.text)
        activity=self.client.get('/auth/activity',headers=admin)
        self.assertEqual(activity.status_code,200,activity.text)
        self.assertEqual(activity.json()['events'][0]['case_id'],item.json()['case_id'])
        gone=self.client.post('/auth/logout-all',headers=admin)
        self.assertEqual(gone.status_code,200,gone.text)
        self.assertGreaterEqual(gone.json()['sessions_revoked'],2)
        self.assertEqual(self.client.get('/auth/me',headers=admin).status_code,401)
        self.assertEqual(self.client.get('/auth/me',headers=second).status_code,401)

    def test_brute_force_delay(self):
        self.admin()
        for i in range(5):
            self.assertEqual(self.post('/auth/login',{'username':'root','password':'incorrect'}).status_code,401)
        self.assertEqual(self.post('/auth/login',{'username':'root','password':'incorrect'}).status_code,429)

if __name__=='__main__':unittest.main()
