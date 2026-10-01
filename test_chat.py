import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chat import ChatService, message_key, message_content
from dispatch import ResponsePlan, UpdateDispatcher
from main import handle
from seed import seed_default_test
from storage import Storage
from transports import MaxTransport, send_chat_attachments


class Transport:
    def __init__(self, platform):
        self.platform, self.sent, self.deleted = platform, [], []
        self.fail = False

    def send(self, user, text, **kwargs):
        if self.fail:
            raise RuntimeError('offline')
        self.sent.append((user, text, kwargs))

    def answer_callback(self, *args, **kwargs):
        pass

    def delete(self, *args):
        self.deleted.append(args)


class CRM:
    def __init__(self):
        self.notes = []
        self.fail_after_write = False

    def has_note(self, lead, marker):
        return any(l == lead and marker in note for l, note in self.notes)

    def add_note(self, lead, text):
        self.notes.append((lead, text))
        if self.fail_after_write:
            self.fail_after_write = False
            raise RuntimeError('response lost')


class ChatTests(unittest.TestCase):
    def setUp(self):
        f = tempfile.NamedTemporaryFile(delete=False)
        f.close()
        self.path = f.name
        self.store = Storage(self.path)
        seed_default_test(self.store)
        self.config = SimpleNamespace(manager_ids=frozenset({'88', '89'}), admin_ids=frozenset({'99'}))
        self.transports = {p: Transport(p) for p in ('telegram', 'max')}
        self.crm = CRM()
        self.chat = ChatService(self.store, self.config, self.crm, self.transports)
        self.engine = SimpleNamespace(store=self.store, agent_program=SimpleNamespace(chat=self.chat))
        for platform in self.transports:
            self.store.touch_user(platform, '1', 'Иван Клиент')
            test = self.store.enabled_test()
            attempt = self.store.start_attempt(platform, '1', test['id'])
            self.store.set_identity(attempt['id'], full_name='Иван Иванович Клиент', lead_id=101 if platform == 'max' else 202)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def start(self, platform='max', user='1'):
        self.chat.callback(self.transports[platform], platform, user, 'chat:start_agent')
        return self.chat.active(platform, user)['id']

    def connect(self, cid, manager='88', platform='telegram'):
        self.chat.callback(self.transports[platform], platform, manager, f'chat:session:{cid}')

    def event(self, platform, user, number, text):
        update = {'update_id': number, 'message': {'message_id': number, 'chat': {'id': user}, 'from': {'id': user}, 'text': text}}
        handle(self.transports[platform], update, self.engine, None, self.config, self.transports)

    def test_max_user_and_telegram_manager_relay_plain_messages_and_note_both_directions(self):
        cid = self.start()
        self.assertEqual({x[0] for x in self.transports['telegram'].sent}, {'88', '89'})
        self.connect(cid)
        self.event('max', '1', 1, 'Вопрос <текст>')
        self.event('telegram', '88', 1, 'Ответ & помощь')
        self.assertIn('Вопрос &lt;текст&gt;', self.transports['telegram'].sent[-1][1])
        self.assertIn('MAX ID 1', self.transports['telegram'].sent[-1][1])
        self.assertIn('Ответ &amp; помощь', self.transports['max'].sent[-1][1])
        self.assertTrue(self.transports['max'].sent[-1][2]['preserve'])
        self.chat.retry_crm_notes()
        self.assertEqual([lead for lead, _ in self.crm.notes], [101, 101])
        for (_, note), role in zip(self.crm.notes, ['Пользователь', 'Менеджер']):
            self.assertIn('Переписка с менеджером (MAX)', note)
            self.assertIn(f'Отправитель: {role}', note)
            self.assertIn('Клиент: Иван Иванович Клиент / MAX ID 1', note)
        self.chat.retry_crm_notes()
        self.assertEqual(len(self.crm.notes), 2)

    def test_telegram_customer_uses_telegram_transport_and_lead(self):
        cid = self.start('telegram')
        self.connect(cid)
        self.event('telegram', '1', 2, 'Вопрос')
        self.event('telegram', '88', 3, 'Ответ')
        self.chat.retry_crm_notes()
        self.assertEqual([lead for lead, _ in self.crm.notes], [202, 202])
        self.assertIn('Переписка с менеджером (Telegram)', self.crm.notes[0][1])

    def test_waiting_messages_saved_synced_and_shown_on_connect(self):
        cid = self.start()
        self.event('max', '1', 4, 'Первый вопрос')
        self.event('max', '1', 5, 'Второй вопрос')
        self.chat.retry_crm_notes()
        self.assertEqual(len(self.crm.notes), 2)
        self.connect(cid)
        messages = [text for user, text, _ in self.transports['telegram'].sent if user == '88']
        self.assertIn('Первый вопрос', messages[-3])
        self.assertIn('Второй вопрос', messages[-2])
        count = len(self.transports['telegram'].sent)
        self.connect(cid)
        self.assertEqual(len(self.transports['telegram'].sent), count + 1)

    def test_duplicate_incoming_message_is_saved_and_forwarded_once(self):
        cid = self.start()
        self.connect(cid)
        self.event('max', '1', 6, 'Повтор')
        count = len(self.transports['telegram'].sent)
        self.event('max', '1', 6, 'Повтор')
        self.assertEqual(len(self.transports['telegram'].sent), count)
        self.assertEqual(self.store._one('SELECT count(*) n FROM chat_messages')['n'], 1)

    def test_manager_busy_other_manager_denied_and_platform_ids_isolated(self):
        cid = self.start()
        second = self.start('telegram')
        self.connect(cid)
        self.connect(second)
        self.assertIn('уже есть активный чат', self.transports['telegram'].sent[-1][1])
        self.connect(cid, '89')
        self.assertIn('другой менеджер', self.transports['telegram'].sent[-1][1])
        self.connect(second, '88', 'max')
        self.assertIn('Недостаточно прав', self.transports['max'].sent[-1][1])
        self.assertIsNone(self.chat.active('telegram', '1')['manager_id'])

    def test_both_participants_can_close_and_stale_button_cannot_reopen(self):
        for platform, user in [('max', '1'), ('telegram', '88')]:
            cid = self.start()
            self.connect(cid)
            self.event(platform, user, cid, '/endchat')
            self.assertIsNone(self.chat.active('max', '1'))
            self.assertIsNone(self.chat.active('telegram', '88'))
            self.connect(cid)
            self.assertIn('завершён', self.transports['telegram'].sent[-1][1])

    def test_atomic_concurrent_connect_allows_one_manager(self):
        cid = self.start()
        threads = [threading.Thread(target=self.connect, args=(cid, manager)) for manager in ['88', '89']]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        active = [self.chat.active('telegram', manager) for manager in ['88', '89']]
        self.assertEqual(sum(chat is not None for chat in active), 1)

    def test_missing_lead_waits_without_creating_deal_and_syncs_when_linked(self):
        with self.store.db:
            self.store.db.execute("UPDATE attempts SET amo_lead_id=NULL WHERE user_platform='max'")
        self.start()
        self.event('max', '1', 7, 'Без сделки')
        self.chat.retry_crm_notes()
        self.assertEqual(self.crm.notes, [])
        self.assertEqual(self.store._one('SELECT crm_status FROM chat_messages')['crm_status'], 'pending')
        with self.store.db:
            self.store.db.execute("UPDATE attempts SET amo_lead_id=303 WHERE user_platform='max'")
        self.chat.retry_crm_notes()
        self.assertEqual(self.crm.notes[0][0], 303)

    def test_crm_lost_response_deduplicated_and_original_lead_pinned(self):
        self.start()
        self.event('max', '1', 8, 'Полный текст ' + 'д' * 4500)
        self.crm.fail_after_write = True
        with self.assertLogs('chat', level='ERROR'):
            self.chat.retry_crm_notes()
        with self.store.db:
            self.store.db.execute("UPDATE attempts SET amo_lead_id=999 WHERE user_platform='max'")
        self.chat.retry_crm_notes()
        self.assertEqual(len(self.crm.notes), 1)
        self.assertEqual(self.crm.notes[0][0], 101)
        self.assertIn('д' * 4500, self.crm.notes[0][1])
        self.assertEqual(self.store._one('SELECT crm_status FROM chat_messages')['crm_status'], 'done')

    def test_sessions_history_and_pending_notes_survive_restart(self):
        cid = self.start()
        self.connect(cid)
        self.event('telegram', '88', 9, 'Сохранённый ответ')
        self.store.close()
        self.store = Storage(self.path)
        self.chat = ChatService(self.store, self.config, self.crm, self.transports)
        self.assertEqual(self.chat.active('telegram', '88')['id'], cid)
        self.chat.retry_crm_notes()
        self.assertIn('Сохранённый ответ', self.crm.notes[0][1])

    def test_plan_has_no_network_calls_and_transaction_rolls_back_history(self):
        cid = self.start()
        self.connect(cid)
        plan = ResponsePlan('telegram')
        count = len(self.transports['max'].sent)
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.chat.relay(plan, 'telegram', '88', 'Откат', 'telegram:rollback')
                raise RuntimeError('rollback')
        self.assertEqual(self.store._one('SELECT count(*) n FROM chat_messages')['n'], 0)
        self.assertEqual(len(self.transports['max'].sent), count)
        self.assertEqual(plan.calls[0][0], 'send_to')

    def test_durable_cross_platform_response_retried_without_duplicate_history(self):
        cid = self.start()
        self.connect(cid)
        update = {'update_id': 10, 'message': {'message_id': 10, 'from': {'id': '88'}, 'chat': {'id': '88'}, 'text': 'Ответ из очереди'}}
        self.store.enqueue_updates('telegram', [('10', '88', update)], 11)
        # Drive a single dispatcher task explicitly to verify the failure boundary.
        dispatcher = object.__new__(UpdateDispatcher)
        dispatcher.transport = self.transports['telegram']
        dispatcher.engine, dispatcher.admin, dispatcher.config = self.engine, None, self.config
        dispatcher.transports, dispatcher.handle, dispatcher.store = self.transports, handle, self.store
        self.transports['max'].fail = True
        with self.assertRaises(RuntimeError):
            dispatcher._process_user('88')
        row = self.store._one('SELECT * FROM update_queue')
        self.assertEqual(json.loads(row['responses'])[0][0], 'send_to')
        self.assertTrue(self.store.update_processed('telegram', '10'))
        self.transports['max'].fail = False
        dispatcher._process_user('88')
        self.assertEqual(self.store._one('SELECT count(*) n FROM chat_messages')['n'], 1)
        self.assertIsNone(self.store._one('SELECT * FROM update_queue'))
        self.assertIn('Ответ из очереди', self.transports['max'].sent[-1][1])
        self.assertEqual(self.transports['telegram'].deleted, [])

    def test_message_keys_separate_platform_chat_and_message(self):
        update = {'message': {'message_id': 3, 'chat': {'id': 2}}}
        self.assertEqual(message_key('telegram', update, '1'), 'telegram:message:2:3')
        self.assertEqual(message_key('max', dict(update, _event_id='mid'), '1'), 'max:mid')

    def test_long_message_split_without_truncating_history_or_html_entities(self):
        cid = self.start()
        self.connect(cid)
        text = '<&>' * 2000
        count = len(self.transports['max'].sent)
        self.event('telegram', '88', 11, text)
        sent = self.transports['max'].sent[count:]
        self.assertGreater(len(sent), 1)
        self.assertTrue(all(len(part) < 4096 for _, part, _ in sent))
        self.assertEqual(self.store._one('SELECT text FROM chat_messages')['text'], text)

    def test_attachment_only_messages_archived_forwarded_and_noted(self):
        cid = self.start()
        self.connect(cid)
        message = {'message_id': 12, 'chat': {'id': '88'}, 'from': {'id': '88'}, 'document': {'file_id': 'file', 'file_name': 'договор.pdf'}}
        with patch('transports.send_chat_attachments') as forward:
            handle(self.transports['telegram'], {'message': message}, self.engine, None, self.config, self.transports)
            self.assertEqual(forward.call_count, 2)
            self.assertTrue(forward.call_args_list[0].kwargs['archive_only'])
            self.assertEqual(forward.call_args_list[1].args[1:4], ('telegram', 'max', '1'))
        self.chat.retry_crm_notes()
        self.assertIn('[вложение: договор.pdf]', self.crm.notes[0][1])
        row = self.store._one('SELECT attachments_json FROM chat_messages')
        self.assertEqual(json.loads(row['attachments_json'])[0]['payload']['file_id'], 'file')

    def test_max_normalization_preserves_media_and_caption(self):
        update = MaxTransport.normalize_update({'update_type': 'message_created', 'message': {'sender': {'user_id': 1}, 'body': {'mid': 'media', 'text': 'Подпись', 'attachments': [{'type': 'image', 'payload': {'url': 'https://max.example/image'}}]}}})
        text, attachments = message_content(update['message'])
        self.assertEqual(text, '[вложение: image] Подпись')
        self.assertEqual(attachments[0]['payload']['url'], 'https://max.example/image')

    def test_manager_without_active_dialog_gets_connection_hint(self):
        self.event('telegram', '88', 13, 'Ответ')
        self.assertIn('Подключитесь кнопкой', self.transports['telegram'].sent[-1][1])

    def test_admin_chat_by_lead_opens_dialog_without_user_request_and_relays(self):
        self.event('telegram', '99', 20, '/chat 101')
        chat = self.chat.active('telegram', '99')
        self.assertEqual((chat['platform'], chat['user_id']), ('max', '1'))
        self.assertIn('MAX ID 1', self.transports['telegram'].sent[-1][1])
        self.event('telegram', '99', 21, 'Ответ администратора')
        self.event('max', '1', 22, 'Ответ клиента')
        self.assertEqual(self.transports['telegram'].sent[-1][0], '99')
        self.chat.retry_crm_notes()
        self.assertEqual([lead for lead, _ in self.crm.notes], [101, 101])
        self.assertIn('Отправитель: Менеджер', self.crm.notes[0][1])

    def test_admin_chat_by_platform_and_username_case_insensitive(self):
        self.store.touch_user('telegram', '1', 'Клиент', 'Client_Name')
        for number, argument, target in [(23, '@client_NAME', 'telegram'), (24, 'Client_Name', 'telegram'),
                                        (25, 'telegram:1', 'telegram'), (26, 'max:1', 'max'), (27, 'lead:101', 'max')]:
            self.event('telegram', '99', number, '/chat ' + argument)
            self.assertEqual(self.chat.active('telegram', '99')['platform'], target)
            self.event('telegram', '99', number + 100, '/endchat')

    def test_admin_chat_bare_user_id_with_unique_match(self):
        self.store.touch_user('max', '555', 'Новый клиент')
        self.event('telegram', '99', 28, '/chat 555')
        self.assertEqual(self.chat.active('telegram', '99')['user_id'], '555')
        self.assertEqual(self.store._one("SELECT count(*) n FROM attempts WHERE user_id='555'")['n'], 0)

    def test_admin_chat_ambiguous_number_requires_explicit_target(self):
        self.event('telegram', '99', 29, '/chat 1')
        self.assertIn('/chat max:1', self.transports['telegram'].sent[-1][1])
        self.assertIn('/chat telegram:1', self.transports['telegram'].sent[-1][1])
        self.assertIsNone(self.chat.active('telegram', '99'))
        # A lead ID may also collide with another user's platform ID.
        self.store.touch_user('telegram', '101', 'Другой клиент')
        self.event('telegram', '99', 30, '/chat 101')
        self.assertIsNone(self.chat.active('telegram', '99'))

    def test_admin_chat_rejects_nonadmin_missing_user_and_invalid_input(self):
        for number, user, command, response in [(31, '88', '/chat 101', 'Недостаточно прав'),
                                              (32, '1', '/chat 101', 'Недостаточно прав'),
                                              (33, '99', '/chat', 'Использование'),
                                              (34, '99', '/chat @missing', 'не найден'),
                                              (35, '99', '/chat invalid input', 'не найден'),
                                              (36, '99', '/chat telegram:99', 'не сотрудника')]:
            self.event('telegram', user, number, command)
            self.assertIn(response, self.transports['telegram'].sent[-1][1])
        self.assertEqual(self.store._one('SELECT count(*) n FROM chat_sessions')['n'], 0)

    def test_admin_chat_keeps_busy_dialog_and_other_managers_assignment(self):
        cid = self.start()
        self.connect(cid)
        self.event('telegram', '99', 37, '/chat lead:101')
        self.assertIn('другой менеджер', self.transports['telegram'].sent[-1][1])
        self.assertEqual(self.chat.active('telegram', '88')['id'], cid)
        self.event('telegram', '99', 38, '/chat telegram:1')
        self.event('telegram', '99', 39, '/chat max:1')
        self.assertIn('/endchat', self.transports['telegram'].sent[-1][1])
        self.assertEqual(self.chat.active('telegram', '99')['platform'], 'telegram')

    def test_admin_chat_reopens_closed_chat_and_repeated_command_is_idempotent(self):
        cid = self.start()
        self.chat.callback(self.transports['max'], 'max', '1', 'chat:end')
        self.event('telegram', '99', 40, '/chat max:1')
        self.assertNotEqual(self.chat.active('telegram', '99')['id'], cid)
        count = len(self.transports['max'].sent)
        self.event('telegram', '99', 41, '/chat max:1')
        self.assertEqual(len(self.transports['max'].sent), count)
        self.assertEqual(self.store._one("SELECT count(*) n FROM chat_sessions WHERE status='active'")['n'], 1)

    def test_max_admin_dialog_routes_manager_messages_and_end_to_max(self):
        self.event('max', '99', 42, '/chat telegram:1')
        self.event('telegram', '1', 43, 'Сообщение админу MAX')
        self.assertIn('Сообщение админу MAX', self.transports['max'].sent[-1][1])
        self.assertEqual(self.transports['max'].sent[-1][0], '99')
        self.event('max', '99', 44, 'Ответ из MAX')
        self.assertEqual(self.transports['telegram'].sent[-1][0], '1')
        self.event('telegram', '1', 45, '/endchat')
        self.assertIn('завершён', self.transports['max'].sent[-1][1])
        self.assertIsNone(self.chat.active('max', '99'))

    def test_staff_platform_ids_have_separate_active_dialogs(self):
        self.event('telegram', '99', 46, '/chat max:1')
        self.event('max', '99', 47, '/chat telegram:1')
        self.assertEqual(self.chat.active('telegram', '99')['platform'], 'max')
        self.assertEqual(self.chat.active('max', '99')['platform'], 'telegram')

    def test_admin_text_editor_still_receives_input_without_active_chat(self):
        received = []
        def admin_text(platform, user, text):
            received.append(text)
            return 'Сохранено', []
        admin = SimpleNamespace(text=admin_text)
        update = {'update_id': 48, 'message': {'message_id': 48, 'from': {'id': '99'}, 'text': 'Текст вопроса'}}
        handle(self.transports['telegram'], update, self.engine, admin, self.config, self.transports)
        self.assertEqual(received, ['Текст вопроса'])
        self.assertEqual(self.transports['telegram'].sent[-1][1], 'Сохранено')


class AttachmentTransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cache_patch = patch('transports.CHAT_MEDIA_DIR', Path(self.directory.name))
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.directory.cleanup()

    def response(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'file bytes'
        return Response()

    def test_telegram_media_downloaded_once_archived_and_uploaded_to_max(self):
        calls = []
        source = SimpleNamespace(token='token', _call=lambda *args: {'file_path': 'docs/file.pdf'})
        def max_call(path, body=None, method=None):
            calls.append((path, body, method))
            return {'url': 'https://uploads.example/upload', 'token': 'upload-token'} if path.startswith('/uploads') else {'success': True}
        target = SimpleNamespace(_call=max_call)
        media = [{'type': 'document', 'payload': {'file_id': 'file-id', 'file_name': 'file.pdf'}}]
        transports = {'telegram': source, 'max': target}
        with patch('transports._open_with_retry', return_value=self.response()) as download, patch('transports._multipart', return_value={}) as upload:
            send_chat_attachments(transports, 'telegram', 'telegram', '88', media, archive_only=True)
            send_chat_attachments(transports, 'telegram', 'max', '1', media)
            self.assertEqual(download.call_count, 1)
            self.assertEqual(upload.call_args.args[2:5], ('file.pdf', b'file bytes', 'data'))
        self.assertEqual(calls[-1][0], '/messages?user_id=1')
        self.assertEqual(calls[-1][1]['attachments'][0]['payload']['token'], 'upload-token')

    def test_max_media_downloaded_and_sent_to_telegram_as_document(self):
        media = [{'type': 'file', 'payload': {'url': 'https://max.example/file', 'file_name': 'file.pdf'}}]
        transports = {'max': SimpleNamespace(), 'telegram': SimpleNamespace(token='token')}
        with patch('transports._open_with_retry', return_value=self.response()), patch('transports._multipart', return_value={'ok': True}) as upload:
            send_chat_attachments(transports, 'max', 'telegram', '88', media)
        self.assertEqual(upload.call_args.args[0], 'https://api.telegram.org/bottoken/sendDocument')
        self.assertEqual(upload.call_args.args[1], {'chat_id': '88'})
        self.assertEqual(upload.call_args.args[-1], 'document')


if __name__ == '__main__':
    unittest.main()
