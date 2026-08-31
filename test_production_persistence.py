from __future__ import annotations

import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError

from engine import SurveyEngine
from main import run_transport
from seed import seed_default_test
from storage import Storage
from transports import MaxTransport


class DurablePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); self.file.close()
        self.store=Storage(self.file.name)

    def test_cursor_and_processed_update_survive_reopen(self) -> None:
        self.assertTrue(self.store.complete_update('telegram','123',124))
        self.store.close()
        reopened=Storage(self.file.name)
        self.assertEqual(reopened.poll_cursor('telegram'),'124')
        self.assertTrue(reopened.update_processed('telegram','123'))
        self.assertFalse(reopened.complete_update('telegram','123',125))
        self.assertEqual(reopened.poll_cursor('telegram'),'124')

    def test_crm_operation_has_durable_single_claim(self) -> None:
        self.assertTrue(self.store.claim_crm_operation('note:start:7'))
        self.store.finish_crm_operation('note:start:7')
        self.store.close()
        reopened=Storage(self.file.name)
        self.assertFalse(reopened.claim_crm_operation('note:start:7'))

    def test_failed_crm_operation_can_be_retried(self) -> None:
        self.assertTrue(self.store.claim_crm_operation('note:final:7'))
        self.store.fail_crm_operation('note:final:7')
        self.assertTrue(self.store.claim_crm_operation('note:final:7'))

    def test_running_operation_is_recovered_after_restart(self) -> None:
        self.assertTrue(self.store.claim_crm_operation('action:move_stage:7:2'))
        self.store.close()
        reopened=Storage(self.file.name)
        self.assertTrue(reopened.claim_crm_operation('action:move_stage:7:2'))


class MaxLinkButtonsTests(unittest.TestCase):
    def test_links_and_callbacks_keep_their_respective_max_types(self) -> None:
        buttons=MaxTransport._inline_buttons([[{'text':'Site','url':'https://example.test'}, {'text':'Next','callback_data':'survey:next'}]])
        self.assertEqual(buttons[0][0],{'type':'link','text':'Site','url':'https://example.test'})
        self.assertEqual(buttons[0][1],{'type':'callback','text':'Next','payload':'survey:next'})


class PollingLifecycleTests(unittest.TestCase):
    class RecordingMaxTransport(MaxTransport):
        def __init__(self, update: dict, stop: threading.Event) -> None:
            super().__init__('token','https://max.example',marker='next-marker')
            self.update=update
            self.stop=stop
            self.calls:list[tuple[str,dict|None,str|None]]=[]
        def updates(self, offset, timeout):
            self.stop.set()
            return [self.update]
        def _call(self,path,body=None,method=None):
            self.calls.append((path,body,method))
            return {'success':True}

    def test_max_polling_starts_from_sqlite_marker_and_honours_stop_event(self) -> None:
        file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); file.close()
        store=Storage(file.name); store.set_poll_cursor('max','saved-marker')
        stop=threading.Event(); received=[]
        class Transport:
            platform='max'; marker='next-marker'
            def updates(self, offset, timeout):
                received.append(offset); stop.set(); return []
        engine=SimpleNamespace(store=store)
        run_transport(Transport(),engine,None,SimpleNamespace(poll_timeout=1,inactivity_seconds=1),threading.RLock(),False,stop)
        self.assertEqual(received,['saved-marker'])
        store.close()

    def test_max_callback_ack_400_does_not_block_processed_update(self) -> None:
        file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); file.close()
        store=Storage(file.name); seed_default_test(store)
        engine=SurveyEngine(store,None,'pipeline','status')
        engine.begin('max','42','Max User')
        active=store.active_attempt('max','42')
        update={
            '_event_id':'max-callback-once',
            'update_id':'m1',
            'callback_query':{
                'id':'bad-callback',
                'from':{'id':'42','first_name':'Max'},
                'data':f'survey:back:{active["id"]}:{active["current_question_id"]}',
                'message':{'message_id':'mid-1','chat':{'id':'42'}},
            },
        }
        stop=threading.Event()
        class Transport:
            platform='max'; marker='next-marker'
            def __init__(self): self.polls=0; self.acks=0; self.sends=0
            def updates(self, offset, timeout):
                self.polls+=1
                if self.polls == 2:
                    stop.set()
                return [update]
            def answer_callback(self, callback_id, text='', *, inline=None):
                self.acks+=1
                raise HTTPError('https://max.example/answers?callback_id=bad-callback',400,'bad request',{},None)
            def edit(self, user_id, message_id, text, inline): pass
            def send(self, *args, **kwargs): self.sends+=1
            def delete(self, *args, **kwargs): pass
        transport=Transport()
        with self.assertLogs(level='WARNING'):
            run_transport(transport,engine,None,SimpleNamespace(poll_timeout=1,inactivity_seconds=1,admin_ids=frozenset()),threading.RLock(),False,stop)
        self.assertTrue(store.update_processed('max','max-callback-once'))
        self.assertEqual(transport.acks,1)
        self.assertEqual(transport.sends,1)
        engine.shutdown(); store.close()

    def test_max_inline_callback_is_answered_with_new_message_body(self) -> None:
        file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); file.close()
        store=Storage(file.name); seed_default_test(store)
        engine=SurveyEngine(store,None,'pipeline','status')
        test=store.enabled_test()['id']; question=store.test_questions(test)[2]
        attempt=store.start_attempt('max','42',test)
        with store.db:
            store.db.execute("UPDATE attempts SET status='completed',current_question_id=NULL WHERE id=?",(attempt['id'],))
            store.db.execute('INSERT INTO answers(attempt_id,question_id,value_json,answered_at) VALUES(?,?,?,?)',(attempt['id'],question['id'],json.dumps('answer'),1))
        update={
            '_event_id':'max-review-view',
            'callback_query':{
                'id':'cb-review',
                'from':{'id':'42','first_name':'Max'},
                'data':f'review:view:{attempt["id"]}:{question["id"]}',
                'message':{'message_id':'mid-review','chat':{'id':'42'}},
            },
        }
        stop=threading.Event(); transport=self.RecordingMaxTransport(update,stop)
        run_transport(transport,engine,None,SimpleNamespace(poll_timeout=1,inactivity_seconds=1,admin_ids=frozenset()),threading.RLock(),False,stop)
        answer=next(call for call in transport.calls if call[0].startswith('/answers?'))
        self.assertIn('message',answer[1])
        self.assertIn('attachments',answer[1]['message'])
        self.assertEqual(answer[1]['message']['attachments'][0]['type'],'inline_keyboard')
        self.assertFalse(any(path.startswith('/messages?message_id=') and method == 'PUT' for path,_,method in transport.calls))
        self.assertTrue(store.update_processed('max','max-review-view'))
        engine.shutdown(); store.close()

    def test_max_multi_choice_callback_sends_reply_keyboard_in_new_message(self) -> None:
        file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); file.close()
        store=Storage(file.name); seed_default_test(store)
        engine=SurveyEngine(store,None,'pipeline','status')
        test=store.enabled_test()['id']; question=store.test_questions(test)[9]; option=store.options(question['id'])[0]
        attempt=store.start_attempt('max','42',test)
        with store.db:
            store.db.execute('UPDATE attempts SET current_question_id=? WHERE id=?',(question['id'],attempt['id']))
        update={
            '_event_id':'max-multi-choice',
            'callback_query':{
                'id':'cb-multi',
                'from':{'id':'42','first_name':'Max'},
                'data':f'survey:pick:{attempt["id"]}:{question["id"]}:{option["id"]}',
                'message':{'message_id':'mid-multi','chat':{'id':'42'}},
            },
        }
        stop=threading.Event(); transport=self.RecordingMaxTransport(update,stop)
        run_transport(transport,engine,None,SimpleNamespace(poll_timeout=1,inactivity_seconds=1,admin_ids=frozenset()),threading.RLock(),False,stop)
        answer=next(call for call in transport.calls if call[0].startswith('/answers?'))
        message=next(call for call in transport.calls if call[0].startswith('/messages?user_id='))
        self.assertEqual(answer[1],{'message':None})
        self.assertEqual(message[1]['attachments'][0]['payload']['buttons'][0][0]['type'],'message')
        self.assertFalse(any(path.startswith('/messages?message_id=') and method == 'PUT' for path,_,method in transport.calls))
        self.assertTrue(store.update_processed('max','max-multi-choice'))
        engine.shutdown(); store.close()


class _FlakyCRM:
    def __init__(self) -> None: self.calls=0; self.fail=True
    def target_stage(self, pipeline, status): return (1,2)
    def move_lead(self, lead, pipeline, status):
        self.calls+=1
        if self.fail:
            self.fail=False
            raise RuntimeError('temporary amoCRM failure')


class CRMActionRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); self.file.close()
        self.store=Storage(self.file.name); seed_default_test(self.store)
        self.crm=_FlakyCRM(); self.engine=SurveyEngine(self.store,self.crm,'pipeline','status')
        option=self.store.db.execute("SELECT * FROM options WHERE action_json IS NOT NULL LIMIT 1").fetchone()
        assert option is not None
        self.option=option
        attempt=self.store.start_attempt('telegram','crm-test',self.store.enabled_test()['id'])
        self.store.set_identity(attempt['id'],lead_id=42)
        self.attempt=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt['id'],))

    def tearDown(self) -> None: self.engine.shutdown(); self.store.close()

    def test_crm_error_then_retry_executes_action_once(self) -> None:
        self.engine._run_actions(self.attempt,[self.option],self.option['text'])
        self.engine._run_actions(self.attempt,[self.option],self.option['text'])
        self.assertEqual(self.crm.calls,2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM action_executions').fetchone()[0],1)

    def test_successful_action_and_duplicate_update_do_not_repeat_it(self) -> None:
        self.crm.fail=False
        update_key='telegram-update-99'
        for _ in range(2):
            if not self.store.update_processed('telegram',update_key):
                self.engine._run_actions(self.attempt,[self.option],self.option['text'])
                self.store.complete_update('telegram',update_key)
        self.assertEqual(self.crm.calls,1)


if __name__=='__main__': unittest.main()
