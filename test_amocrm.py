from __future__ import annotations

import unittest

from amocrm import AmoClient


class Client(AmoClient):
    def __init__(self): super().__init__('https://example.test','token'); self.queries=[]
    def request(self, method, path, *, params=None, body=None):
        if path == '/api/v4/leads':
            self.queries.append(params['query'])
            query=params['query']
            if query == '9990000000': return {'_embedded': {'leads': [{'id': 2, '_embedded': {'contacts':[{'id':20}]}}]}}
            if query in {'иванов','иван'}: return {'_embedded': {'leads': [{'id': 3, '_embedded': {'contacts':[{'id':30}]}}]}}
            return {'_embedded': {'leads': []}}
        if path == '/api/v4/contacts/20': return {'name':'Другой','custom_fields_values':[{'field_code':'PHONE','values':[{'value':'+7 999 000-00-00'}]}]}
        if path == '/api/v4/contacts/30': return {'name':'Иван Иванов','custom_fields_values':[]}
        raise AssertionError(path)


class AmoFindLeadTests(unittest.TestCase):
    def test_phone_has_priority_over_name(self):
        client=Client(); self.assertEqual(client.find_lead('Иванов Иван','+7 (999) 000-00-00'),2)
        self.assertEqual(client.queries,['9990000000'])
    def test_name_is_unordered_fallback(self):
        client=Client(); self.assertEqual(client.find_lead('Иванов Иван',''),3)
    def test_empty_amocrm_result_is_not_an_error(self):
        client=Client(); client.request=lambda *args, **kwargs: None
        self.assertIsNone(client.find_lead('Иванов Иван','79990000000'))

if __name__ == '__main__': unittest.main()
