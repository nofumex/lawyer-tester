import json
import unittest
from unittest.mock import patch

from transports import MaxTransport


class _Response:
    def __init__(self, payload): self.payload = payload
    def read(self): return json.dumps(self.payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False


class MaxTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = MaxTransport('test-token', 'https://max.example')
        self.requests = []

    def request(self, request, timeout):
        self.requests.append(request)
        return _Response({'success': True})

    def test_send_uses_query_recipient_html_and_max_buttons(self):
        with patch('transports.urlopen', self.request):
            self.transport.send('42', '<b>Hello</b>', keyboard=[['One']], inline=[[{'text': 'Next', 'callback_data': 'survey:next'}]])
        request = self.requests[0]
        self.assertEqual(request.full_url, 'https://max.example/messages?user_id=42')
        self.assertEqual(request.get_header('Authorization'), 'test-token')
        body = json.loads(request.data)
        self.assertEqual(body['format'], 'html')
        self.assertEqual(body['attachments'][0]['payload']['buttons'][0][0], {'type': 'message', 'text': 'One'})
        self.assertEqual(body['attachments'][1]['payload']['buttons'][0][0], {'type': 'callback', 'text': 'Next', 'payload': 'survey:next'})

    def test_edit_delete_and_callback_use_documented_methods(self):
        with patch('transports.urlopen', self.request):
            self.transport.edit('42', 'mid-1', 'Changed', [[{'text': 'Back', 'callback_data': 'survey:back'}]])
            self.transport.delete('42', 'mid-1')
            self.transport.answer_callback('cb-1', 'Done')
        edit, delete, callback = self.requests
        self.assertEqual((edit.get_method(), edit.full_url), ('PUT', 'https://max.example/messages?message_id=mid-1'))
        self.assertEqual((delete.get_method(), delete.full_url), ('DELETE', 'https://max.example/messages?message_id=mid-1'))
        self.assertEqual((callback.get_method(), callback.full_url), ('POST', 'https://max.example/answers?callback_id=cb-1'))
        self.assertEqual(json.loads(callback.data), {'notification': 'Done'})

    def test_updates_uses_marker_and_normalizes_callback_user(self):
        response = {'marker': 99, 'updates': [{'update_type': 'message_callback', 'timestamp': 1, 'chat_id': 20, 'callback': {'callback_id': 'cb', 'payload': 'survey:back', 'user': {'user_id': 7, 'name': 'Ivan'}}, 'message': {'body': {'mid': 'mid'}}}]}
        def request(request, timeout):
            self.requests.append(request)
            return _Response(response)
        with patch('transports.urlopen', request):
            updates = self.transport.updates(0, 25)
        self.assertIn('types=message_created%2Cmessage_callback%2Cbot_started', self.requests[0].full_url if self.requests else '')
        self.assertEqual(self.transport.marker, 99)
        self.assertEqual(updates[0]['callback_query']['from']['id'], '7')
        self.assertEqual(updates[0]['callback_query']['message']['message_id'], 'mid')
