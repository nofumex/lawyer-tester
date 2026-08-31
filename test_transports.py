import io
import json
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from transports import MaxTransport, TelegramTransport


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
        self.assertEqual(json.loads(callback.data), {'message': {'text': 'Done', 'format': 'html'}})

    def test_empty_callback_ack_sends_nullable_message_body(self):
        with patch('transports.urlopen', self.request):
            self.transport.answer_callback('cb-empty')
        request = self.requests[0]
        self.assertEqual((request.get_method(), request.full_url), ('POST', 'https://max.example/answers?callback_id=cb-empty'))
        self.assertEqual(json.loads(request.data), {'message': None})
        self.assertEqual(request.get_header('Content-type'), 'application/json')

    def test_callback_ack_400_logs_response_and_is_best_effort(self):
        error = HTTPError('https://max.example/answers?callback_id=bad', 400, 'bad request', {}, io.BytesIO(b'{"success":false,"message":"bad callback"}'))
        with patch('transports.urlopen', side_effect=error), self.assertLogs('transports', level='WARNING') as logs:
            self.transport.answer_callback('bad')
        self.assertIn('status=400', logs.output[0])
        self.assertIn('bad callback', logs.output[0])

    def test_edit_400_logs_response_and_is_best_effort(self):
        error = HTTPError('https://max.example/messages?message_id=bad', 400, 'bad request', {}, io.BytesIO(b'{"success":false,"message":"bad edit"}'))
        with patch('transports.urlopen', side_effect=error), self.assertLogs('transports', level='WARNING') as logs:
            self.transport.edit('42', 'bad', 'Changed', [[{'text': 'Back', 'callback_data': 'survey:back'}]])
        self.assertIn('status=400', logs.output[0])
        self.assertIn('bad edit', logs.output[0])

    def test_edit_empty_inline_removes_attachments_without_empty_buttons(self):
        with patch('transports.urlopen', self.request):
            self.transport.edit('42', 'mid-1', 'Done', [])
        body = json.loads(self.requests[0].data)
        self.assertEqual(body['attachments'], [])

    def test_callback_answer_can_update_message_with_inline_buttons(self):
        with patch('transports.urlopen', self.request):
            self.transport.answer_callback('cb-edit', 'Changed', inline=[[{'text':'Next','callback_data':'survey:next'}]])
        body = json.loads(self.requests[0].data)
        self.assertEqual(body['message']['text'], 'Changed')
        self.assertEqual(body['message']['format'], 'html')
        self.assertEqual(body['message']['attachments'][0]['payload']['buttons'][0][0], {'type':'callback','text':'Next','payload':'survey:next'})
        self.assertNotIn('notification', body)

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

    def test_restored_marker_is_used_when_no_explicit_cursor_is_passed(self):
        response={'marker':101,'updates':[]}
        requests=[]
        def request(request, timeout):
            requests.append(request)
            return _Response(response)
        with patch('transports.urlopen',request):
            MaxTransport('test-token','https://max.example',marker='saved-marker').updates(None,25)
        self.assertIn('marker=saved-marker',requests[0].full_url)


class TransportRetryTests(unittest.TestCase):
    def test_telegram_retries_rate_limit_and_network_failures(self):
        failures=[HTTPError('https://telegram.example',429,'rate limited',{'Retry-After':'0'},None),URLError('temporary')]
        response=_Response({'ok':True,'result':{'message_id':1}})
        with patch('transports.urlopen',side_effect=failures+[response]) as opened, patch('transports.time.sleep') as sleep:
            TelegramTransport('token').send('1','hello')
        self.assertEqual(opened.call_count,3)
        self.assertEqual(sleep.call_count,2)

    def test_max_retries_server_error(self):
        error=HTTPError('https://max.example',503,'unavailable',{},None)
        with patch('transports.urlopen',side_effect=[error,_Response({'success':True})]) as opened, patch('transports.time.sleep'):
            MaxTransport('token','https://max.example').send('1','hello')
        self.assertEqual(opened.call_count,2)
