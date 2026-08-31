from __future__ import annotations

import tempfile
import unittest

from engine import SurveyEngine
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
