from __future__ import annotations

import unittest

from amocrm import AmoClient


class Client(AmoClient):
    def __init__(self): super().__init__('https://example.test','token'); self.queries=[]
    def request(self, method, path, *, params=None, body=None):
        if path == '/api/v4/contacts':
            query=params.get('query','');self.queries.append(query);items=[]
            if query=='9990000000':items.append({'id':20,'name':'Другой','custom_fields_values':[{'field_code':'PHONE','values':[{'value':'+7 999 000-00-00'}]}],'_embedded':{'leads':[{'id':2}]}})
            if query in {'иванов','иван'}:items.append({'id':30,'name':'Иван Иванов','custom_fields_values':[],'_embedded':{'leads':[{'id':3}]}})
            return {'_embedded':{'contacts':items}}
        if path == '/api/v4/leads':
            ids=set(params.values());items=[]
            if 2 in ids:items.append({'id':2,'pipeline_id':111})
            if 3 in ids:items.append({'id':3,'pipeline_id':111})
            return {'_embedded':{'leads':items}}
        raise AssertionError(path)
    def target_stage(self,pipeline,status):return (111,222)


class AmoFindLeadTests(unittest.TestCase):
    def test_phone_has_priority_over_name(self):
        client=Client(); self.assertEqual(client.find_lead('Иванов Иван','+7 (999) 000-00-00'),2)
        self.assertEqual(client.queries,['9990000000'])
    def test_name_is_unordered_fallback(self):
        client=Client(); self.assertEqual(client.find_lead('Иванов Иван',''),3)
    def test_contact_without_patronymic_matches_full_fio(self):
        client=Client(); self.assertEqual(client.find_lead('Иванов Иван Иванович',''),3)
    def test_empty_amocrm_result_is_not_an_error(self):
        client=Client(); client.request=lambda *args, **kwargs: None
        self.assertIsNone(client.find_lead('Иванов Иван','79990000000'))

    def test_note_deduplication_searches_beyond_first_page(self):
        client = Client()
        pages = []
        def request(method, path, *, params):
            pages.append(params['page'])
            return {'_embedded': {'notes': [{'params': {'text': 'other'}}] * 250}} if params['page'] == 1 else {'_embedded': {'notes': [{'params': {'text': 'message-marker'}}]}}
        client.request = request
        self.assertTrue(client.has_note(123, 'message-marker'))
        self.assertEqual(pages, [1, 2])

    def test_note_deduplication_stops_at_empty_result(self):
        client = Client()
        client.request = lambda *args, **kwargs: None
        self.assertFalse(client.has_note(123, 'message-marker'))

if __name__ == '__main__': unittest.main()
