import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.main import TriageRequest, triage, RULES

class TriageTests(unittest.TestCase):
    def run_ticket(self, kind, text, attachments=0):
        return triage(TriageRequest(kind=kind, messages=[{'role':'player','text':text,'attachments':attachments}], images_count=attachments))
    def test_insult_is_escalated_and_citation_exists(self):
        out=self.run_ticket('complaint','Игрок оскорблял меня и семью, 2.1 ОП',2)
        self.assertEqual(out['category'],'complaint.insult')
        self.assertTrue(out['needs_human'])
        self.assertTrue(all(ref in RULES for ref in out['draft_reply']['cited']))
        self.assertEqual(out['retrieved_rules'][0]['section'], 'Общие правила игры')
        self.assertIn('оскорбление', out['retrieved_rules'][0]['text'].lower())
    def test_payment_always_human(self):
        out=self.run_ticket('report','Оплатил донат, но голда не пришла')
        self.assertEqual(out['category'],'report.payment')
        self.assertEqual(out['priority'],'p0')
        self.assertTrue(out['needs_human'])
    def test_account_always_human(self):
        out=self.run_ticket('report','Меня взломали, не могу войти в аккаунт')
        self.assertTrue(out['needs_human'])
    def test_uzbek_reply_is_not_russian(self):
        out=triage(TriageRequest(kind='report', lang='uz', messages=[{'role':'player','text':'Iltimos yordam bering'}]))
        self.assertEqual(out['lang'], 'uz')
        self.assertIn('Assalomu alaykum', out['draft_reply']['text'])

    def test_priority_has_explanation(self):
        out=self.run_ticket('report','Оплатил донат, но голда не пришла')
        self.assertEqual(out['priority'], 'p0')
        self.assertTrue(out['priority_reason'])

    def test_no_invented_rule_refs(self):
        out=self.run_ticket('complaint','Нарушение 99.99, он меня оскорблял')
        self.assertNotIn('99.99',out['draft_reply']['cited'])
        self.assertTrue(all(ref in RULES for ref in out['draft_reply']['cited']))
        self.assertEqual(out['retrieved_rules'][0]['section'], 'Общие правила игры')
        self.assertIn('оскорбление', out['retrieved_rules'][0]['text'].lower())

    def test_payment_with_account_word_not_misclassified(self):
        out=self.run_ticket('report','Я оплатил донат на аккаунт, деньги списались, голда не пришла')
        self.assertEqual(out['category'],'report.payment')
        self.assertEqual(out['priority'],'p0')
        self.assertTrue(out['needs_human'])
        self.assertEqual(out['draft_reply']['cited'],[])

    def test_appeal_always_human(self):
        out=self.run_ticket('report','Хочу обжаловать бан администратора')
        self.assertTrue(out['needs_human'])
        self.assertNotIn('гарант',out['draft_reply']['text'].lower())

    def test_account_protection_even_complaint_kind(self):
        out=self.run_ticket('complaint','Аккаунт взломали и забрали вещи')
        self.assertTrue(out['needs_human'])
        self.assertEqual(out['priority'],'p0')

    def test_all_priorities_valid(self):
        for kind,text in [('report','как начать играть'),('report','помогите, ошибка'),('complaint','игрок оскорблял меня'),('complaint','игрок использует чит')]:
            out=self.run_ticket(kind,text)
            self.assertIn(out['priority'],{'p0','p1','p2','p3'})

    def test_uzbek_cyrillic_hint(self):
        out=triage(TriageRequest(kind='report',lang='uz',messages=[{'role':'player','text':'Илтимос ёрдам беринг, ака'}]))
        self.assertEqual(out['lang'],'uz')
        self.assertIn('Assalomu alaykum',out['draft_reply']['text'])

    def test_priority_model_metadata(self):
        from app.main import PRIORITY_MODEL
        self.assertIsNotNone(PRIORITY_MODEL)

if __name__=='__main__': unittest.main()
