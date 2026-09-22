import json
import threading
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from background import cleanup, latency
from engine import SurveyEngine
from main import run_transport
from seed import seed_default_test
from storage import Storage
from transports import TelegramTransport


def event(number, user='1', text='/start'):
    return {'update_id':number,'_event_id':str(number), 'message':{
        'message_id':number,'from':{'id':user},'chat':{'id':user},'text':text}}


class FakeTransport:
    def __init__(self, platform, batches, stop):
        self.platform, self.batches, self.stop = platform, list(batches), stop
        self.marker = '0'
        self.sent = []
        self.polled = threading.Event()
        self.send_hook = lambda user,text: None
        self.delete_hook = lambda: None

    def updates(self, offset, timeout):
        if self.batches:
            self.marker = str(int(self.marker)+1)
            return self.batches.pop(0)
        self.polled.set()
        self.stop.wait(.01)
        return []

    def send(self, user, text, **kwargs):
        self.send_hook(user,text)
        self.sent.append((user,text))

    def delete(self, *args):
        self.delete_hook()


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.store = Storage(':memory:')
        seed_default_test(self.store)
        self.engine = SurveyEngine(self.store,None,'pipeline','stage')
        self.stop = threading.Event()
        self.release = threading.Event()
        self.threads = []
        self.config = SimpleNamespace(poll_timeout=1,inactivity_seconds=1,
                                      admin_ids=frozenset({'99'}),amo_base_url='https://crm.example')

    def tearDown(self):
        self.release.set()
        self.stop.set()
        for thread in self.threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        cleanup.queue.join()
        self.engine.shutdown()
        self.store.close()

    def start(self, transport, snapshots=False):
        thread = threading.Thread(target=run_transport,args=(transport,self.engine,None,self.config,
                                  threading.RLock(),snapshots,self.stop))
        self.threads.append(thread)
        thread.start()

    def wait_for(self, condition):
        deadline=time.monotonic()+2
        while not condition() and time.monotonic()<deadline:
            time.sleep(.005)
        self.assertTrue(condition())

    def test_slow_user_does_not_block_other_user_or_polling_and_preserves_order(self):
        for platform in ('telegram','max'):
            with self.subTest(platform=platform):
                entered=threading.Event()
                transport=FakeTransport(platform,[[event(1),event(2,text='First answer'),event(3,'2')]],self.stop)
                def send(user,text):
                    if user=='1' and not entered.is_set():
                        entered.set()
                        self.release.wait(3)
                transport.send_hook=send
                self.start(transport)
                self.assertTrue(entered.wait(2))
                self.wait_for(lambda:any(user=='2' for user,_ in transport.sent))
                self.assertTrue(transport.polled.wait(2))
                attempt=self.store.active_attempt(platform,'1')
                self.assertEqual(len(self.store.answers_with_questions(attempt['id'])),0)
                self.release.set()
                self.wait_for(lambda:len(transport.sent)==3)
                self.assertEqual(len(self.store.answers_with_questions(attempt['id'])),1)
                self.release.clear()

    def test_slow_incoming_cleanup_does_not_block_same_user(self):
        entered=threading.Event()
        transport=FakeTransport('telegram',[[event(1),event(2,text='Answer')]],self.stop)
        def delete():
            entered.set()
            self.release.wait(3)
        transport.delete_hook=delete
        self.start(transport)
        self.assertTrue(entered.wait(2))
        self.wait_for(lambda:len(transport.sent)==2)

    def test_telegram_previous_message_cleanup_is_background(self):
        entered=threading.Event()
        transport=TelegramTransport('token')
        calls=[]
        def call(method,body):
            if method=='deleteMessage':
                entered.set()
                self.release.wait(3)
            calls.append(method)
            return {'message_id':len(calls)}
        transport._call=call
        transport.send('1','first')
        transport.send('1','second')
        self.assertTrue(entered.wait(2))
        transport.send('1','third')
        self.assertEqual(calls.count('sendMessage'),3)

    def test_snapshot_network_does_not_block_ui_or_mark_new_answer(self):
        entered=threading.Event()
        class CRM:
            def target_stage(self,*args): return (1,2)
            def move_lead(self,*args): pass
            def add_note(_,lead,text):
                entered.set()
                self.release.wait(3)
        self.engine.crm=CRM()
        self.engine.begin('telegram','1',None)
        attempt=self.store.active_attempt('telegram','1')
        self.store.set_identity(attempt['id'],lead_id=42)
        with self.store.db:
            self.store.db.execute('UPDATE attempts SET last_activity_at=0 WHERE id=?',(attempt['id'],))
        transport=FakeTransport('telegram',[],self.stop)
        self.start(transport,True)
        self.assertTrue(entered.wait(2))
        transport.batches.append([event(1,text='Answer')])
        self.wait_for(lambda:len(transport.sent)==1)
        self.release.set()
        self.stop.set()
        self.threads[-1].join(3)
        self.assertEqual(self.store.active_attempt('telegram','1')['snapshot_version'],0)

    def test_search_does_not_block_polling_or_candidate(self):
        entered=threading.Event()
        class CRM:
            def find_lead(_,name,phone):
                entered.set()
                self.release.wait(3)
                return None
        self.engine.crm=CRM()
        transport=FakeTransport('max',[[event(1,'99','/test_search Name'),event(2)]],self.stop)
        self.start(transport)
        self.assertTrue(entered.wait(2))
        self.wait_for(lambda:any(user=='1' for user,_ in transport.sent))
        self.assertTrue(transport.polled.wait(2))

    def test_failed_delivery_replays_response_without_reapplying_answer(self):
        self.engine.begin('telegram','1',None)
        transport=FakeTransport('telegram',[[event(1,text='Answer'),event(1,text='Answer')]],self.stop)
        def fail(*args):
            raise OSError('offline')
        transport.send_hook=fail
        self.stop.set()
        self.store.enqueue_updates('telegram',[('1','1',event(1,text='Answer'))],2)
        with self.assertLogs(level='ERROR'):
            run_transport(transport,self.engine,None,self.config,threading.RLock(),False,self.stop)
        attempt=self.store.active_attempt('telegram','1')
        self.assertEqual(len(self.store.answers_with_questions(attempt['id'])),1)
        self.assertTrue(self.store.update_processed('telegram','1'))
        self.assertIsNotNone(self.store._one('SELECT responses FROM update_queue')['responses'])
        transport.send_hook=lambda *args:None
        run_transport(transport,self.engine,None,self.config,threading.RLock(),False,self.stop)
        self.assertEqual(len(transport.sent),1)
        self.assertEqual(len(self.store.answers_with_questions(attempt['id'])),1)
        self.assertIsNone(self.store._one('SELECT * FROM update_queue'))

    def test_nested_transaction_rolls_back_state_and_dedup_together(self):
        with self.assertRaises(RuntimeError):
            with self.store.db:
                self.engine.begin('telegram','1',None)
                self.engine.receive('telegram','1','Answer')
                self.store.complete_update('telegram','1')
                raise RuntimeError('crash before commit')
        self.assertIsNone(self.store.active_attempt('telegram','1'))
        self.assertFalse(self.store.update_processed('telegram','1'))

    def test_receipt_and_cursor_survive_restart_before_processing(self):
        for platform in ('telegram','max'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                path=directory+'/state.sqlite3'
                store=Storage(path)
                seed_default_test(store)
                store.enqueue_updates(platform,[('1','1',event(1)),('2','1',event(2,text='Answer'))],3)
                store.close()
                store=Storage(path)
                engine=SurveyEngine(store,None,'pipeline','stage')
                stop=threading.Event(); stop.set()
                transport=FakeTransport(platform,[],stop)
                run_transport(transport,engine,None,self.config,threading.RLock(),False,stop)
                self.assertEqual(store.poll_cursor(platform),'3')
                self.assertEqual(len(transport.sent),2)
                self.assertEqual(len(store.answers_with_questions(store.active_attempt(platform,'1')['id'])),1)
                engine.shutdown(); store.close()

    def test_failed_preparation_keeps_later_answer_pending(self):
        self.store.enqueue_updates('max',[('1','1',event(1)),('2','1',event(2,text='Answer'))],3)
        transport=FakeTransport('max',[],self.stop)
        self.stop.set()
        from main import handle
        def fail(*args):
            handle(*args)
            raise RuntimeError('local failure')
        with patch('main.handle',fail), self.assertLogs(level='ERROR'):
            run_transport(transport,self.engine,None,self.config,threading.RLock(),False,self.stop)
        self.assertFalse(self.store.update_processed('max','1'))
        self.assertFalse(self.store.update_processed('max','2'))
        self.assertIsNone(self.store.active_attempt('max','1'))
        self.assertEqual(self.store._one('SELECT count(*) FROM update_queue')[0],2)

    def test_latency_logs_section_and_duration(self):
        with patch('background.time.monotonic',side_effect=[1,1.201]),self.assertLogs(level='WARNING') as logs:
            with latency('update.local',platform='max'):
                pass
        self.assertIn('section=update.local duration_ms=201.0',logs.output[0])
