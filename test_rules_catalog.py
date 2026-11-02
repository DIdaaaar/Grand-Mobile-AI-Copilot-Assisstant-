"""Knowledge-base regression: no lost chapters or confusing reused paragraph numbers."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi.testclient import TestClient
from app import main

class RuleCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.auth_patch=patch.object(main,'AUTH_MODE','off');self.auth_patch.start()
        self.client = TestClient(main.app)
    def tearDown(self):
        self.client.close();self.auth_patch.stop()

    def test_all_source_points_present_without_number_rewrites(self):
        source_points = [(chapter['id'], p['ref'], p['text']) for chapter in main.RULES_RAW['rules'] for p in chapter.get('points', []) if p.get('ref')]
        actual_points = [(r['section_id'], r['ref'], r['text']) for r in main.RULE_CATALOG if r['kind'] == 'rule']
        self.assertCountEqual(source_points, actual_points)
        self.assertEqual(len(actual_points), 266)

    def test_unique_ids_and_cross_chapter_repeated_refs(self):
        ids = [rule['id'] for rule in main.RULE_CATALOG]
        self.assertEqual(len(ids),len(set(ids)))
        repeated = [r for r in main.RULE_CATALOG if r['ref'] == '1.1' and r['kind'] == 'rule']
        self.assertEqual(len(repeated),10)
        self.assertEqual(len({r['section_id'] for r in repeated}),10)

    def test_api_returns_every_record_and_exact_sections(self):
        response = self.client.get('/rules', params={'limit':500})
        self.assertEqual(response.status_code,200)
        data = response.json()
        self.assertEqual(data['count'],len(main.RULE_CATALOG))
        self.assertEqual(len(data['rules']),data['count'])
        self.assertEqual(len(data['sections']),11)
        for rule in data['rules']:
            self.assertTrue(rule['text'].strip())
            self.assertTrue(rule['display_ref'].startswith(rule['section']))

    def test_rp_terms_not_assigned_fictitious_rule_number(self):
        response=self.client.get('/rules', params={'section':'RP термины и их описание','limit':500}).json()
        self.assertGreater(response['count'],0)
        self.assertTrue(all(r['kind']=='term' for r in response['rules']))
        self.assertTrue(all('glossary' in r['id'] for r in response['rules']))

    def test_filter_exact_chapter_and_text_search(self):
        entries=self.client.get('/rules',params={'section':'Правила нахождения в Зелёной Зоне','q':'1.1'}).json()['rules']
        self.assertTrue(entries)
        self.assertTrue(all(r['section']=='Правила нахождения в Зелёной Зоне' for r in entries))

if __name__=='__main__':unittest.main()
