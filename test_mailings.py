import os
import tempfile
import time
import unittest

from mailings import DAY, MailingService, due_for_step, mailing_buttons, message_for
from seed import SPECIAL_ANSWER, seed_default_test
from storage import Storage


class FakeTransport:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    def send(self, user_id, text, **kwargs):
        self.sent.append((user_id, text, kwargs))
        if self.fail:
            raise TimeoutError("ambiguous")


class NoteCRM:
    def __init__(self, fail_once=False):
        self.fail_once=fail_once
        self.notes=[]
    def has_note(self,lead_id,marker):
        return any(item[0]==lead_id and marker in item[1] for item in self.notes)
    def add_note(self,lead_id,text):
        if self.fail_once:
            self.fail_once=False
            raise RuntimeError('temporary CRM error')
        self.notes.append((lead_id,text))


class MailingTests(unittest.TestCase):
    def setUp(self):
        file = tempfile.NamedTemporaryFile(delete=False)
        file.close()
        self.path = file.name
        self.store = Storage(self.path)
        seed_default_test(self.store)
        self.transport = FakeTransport()
        self.service = MailingService(self.store, {"telegram": self.transport}, 1)
        self.now = int(time.time())
        self.service.set_setting("mailings_implemented_at", str(self.now))

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def user(self, user_id="1", created_at=None):
        created_at = self.now if created_at is None else created_at
        with self.store.db:
            self.store.db.execute("INSERT INTO users VALUES(?,?,?,?,?)", ("telegram", user_id, "User", created_at, created_at))

    def make_group1(self, user_id="1", completed_at=None):
        completed_at = completed_at or self.now
        test = self.store.enabled_test()
        question = self.store.db.execute("SELECT q.* FROM questions q JOIN options o ON o.question_id=q.id WHERE o.text=?", (SPECIAL_ANSWER,)).fetchone()
        attempt_id = self.store.db.execute(
            "INSERT INTO attempts(user_platform,user_id,test_id,started_at,last_activity_at,status) VALUES(?,?,?,?,?,'completed')",
            ("telegram", user_id, test["id"], completed_at - DAY, completed_at),
        ).lastrowid
        self.store.db.execute("INSERT INTO answers(attempt_id,question_id,value_json,answered_at) VALUES(?,?,?,?)", (attempt_id, question["id"], f'"{SPECIAL_ANSWER}"', completed_at))

    def test_legacy_overdue_is_quarantined_and_default_is_off(self):
        self.user(created_at=self.now - 100 * DAY)
        self.service.reconcile_all()
        job = self.store._one("SELECT * FROM mailing_jobs")
        self.assertEqual(job["status"], "overdue")
        self.assertFalse(self.service.enabled())
        self.assertEqual(self.service.due_ids(), [])
        self.assertEqual(self.transport.sent, [])

    def test_future_natural_schedule_survives_and_uses_actual_send_time(self):
        self.user()
        self.service.reconcile_all()
        first = self.store._one("SELECT * FROM mailing_jobs")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["due_at"], due_for_step(2, 1, self.now))
        with self.store.db:
            self.store.db.execute("UPDATE mailing_jobs SET due_at=? WHERE id=?", (self.now, first["id"]))
            self.store.db.execute("INSERT INTO mailing_test_users VALUES('telegram','1',?)", (self.now,))
        self.assertTrue(self.service.deliver_job(first["id"]))
        second = self.store._one("SELECT * FROM mailing_jobs WHERE step=2")
        sent = self.store._one("SELECT * FROM mailing_jobs WHERE step=1")
        self.assertEqual(second["due_at"], due_for_step(2, 2, sent["sent_at"]))
        self.assertEqual(len(self.transport.sent), 1)

    def test_sent_message_creates_readable_idempotent_crm_note_with_retry(self):
        self.user()
        test=self.store.enabled_test()
        with self.store.db:
            self.store.db.execute("INSERT INTO attempts(user_platform,user_id,test_id,started_at,last_activity_at,status,amo_lead_id) VALUES('telegram','1',?,?,?,'active',77)",(test['id'],self.now,self.now))
        crm=NoteCRM(fail_once=True)
        service=MailingService(self.store,{"telegram":self.transport},1,crm)
        service.reconcile_all()
        job=self.store._one("SELECT * FROM mailing_jobs WHERE step=1")
        with self.store.db:
            self.store.db.execute("UPDATE mailing_jobs SET due_at=? WHERE id=?",(self.now,job['id']))
            self.store.db.execute("INSERT INTO mailing_test_users VALUES('telegram','1',?)",(self.now,))
        self.assertTrue(service.deliver_job(job['id']))
        outbox=self.store._one("SELECT * FROM mailing_crm_notes")
        self.assertIn("Канал: Telegram",outbox['note_text'])
        self.assertIn("Группа: 2",outbox['note_text'])
        self.assertIn("Номер сообщения: 1",outbox['note_text'])
        self.assertIn(message_for(2,1),outbox['note_text'])
        self.assertFalse(service.sync_crm_note(outbox['id']))
        self.assertEqual(self.store._one("SELECT status FROM mailing_crm_notes")['status'],'pending')
        self.assertTrue(service.sync_crm_note(outbox['id']))
        self.assertEqual(self.store._one("SELECT status FROM mailing_crm_notes")['status'],'done')
        self.assertFalse(service.sync_crm_note(outbox['id']))
        self.assertEqual(len(crm.notes),1)

    def test_group1_uses_completion_and_exact_first_message(self):
        self.user(created_at=self.now - 300 * DAY)
        self.make_group1(completed_at=self.now)
        self.service.reconcile_all()
        state = self.store._one("SELECT * FROM mailing_states")
        job = self.store._one("SELECT * FROM mailing_jobs")
        self.assertEqual((state["group_no"], state["anchor_at"]), (1, self.now))
        self.assertEqual(job["due_at"], self.now + 7 * DAY)
        self.assertTrue(message_for(1, 1).startswith("Здравствуйте!"))

    def test_group_change_recalculates_one_existing_job_without_duplicate(self):
        self.user()
        self.service.reconcile_all()
        self.make_group1(completed_at=self.now)
        self.service.reconcile_all()
        job=self.store._one("SELECT * FROM mailing_jobs")
        self.assertEqual((job['group_no'],job['due_at']),(1,self.now+7*DAY))
        self.assertEqual(self.store._one("SELECT count(*) n FROM mailing_jobs")['n'],1)

    def test_deadline_passed_while_disabled_never_bursts_on_enable(self):
        self.user()
        self.service.reconcile_all()
        with self.store.db:
            self.store.db.execute("UPDATE mailing_jobs SET due_at=?", (self.now - 1,))
        self.assertEqual(self.service.set_enabled(True), 1)
        self.assertEqual(self.store._one("SELECT status FROM mailing_jobs")["status"], "overdue")
        self.assertEqual(self.service.due_ids(), [])

    def test_interest_pauses_then_resumes_next_step_without_repeat(self):
        self.user()
        self.service.reconcile_all()
        with self.store.db:
            self.store.db.execute("UPDATE mailing_states SET next_step=2,last_sent_step=1,last_sent_at=?", (self.now,))
            self.store.db.execute("UPDATE mailing_jobs SET status='sent',sent_at=? WHERE step=1", (self.now,))
            self.service._schedule("telegram", "1", 2, 2, self.now + DAY)
        self.assertTrue(self.service.interest("telegram", "1"))
        with self.store.db:
            self.store.db.execute("UPDATE mailing_states SET resume_at=?", (self.now - 1,))
        self.assertEqual(self.service.resume_paused(), 1)
        state = self.store._one("SELECT * FROM mailing_states")
        job = self.store._one("SELECT * FROM mailing_jobs WHERE step=2")
        self.assertIsNone(state["paused_at"])
        self.assertEqual(job["status"], "pending")
        self.assertEqual(self.store._one("SELECT count(*) n FROM mailing_jobs WHERE step=1")["n"], 1)

    def test_transferred_client_stops_resume(self):
        self.user()
        self.service.reconcile_all()
        self.service.interest("telegram", "1")
        with self.store.db:
            self.store.db.execute("UPDATE mailing_states SET resume_at=?", (self.now - 1,))
            self.store.db.execute("INSERT INTO referral_leads(submission_key,agent_platform,agent_user_id,source,platform,client_name,status,created_at,updated_at) VALUES('x','telegram','1','agent_form','telegram','Client','submitted',?,?)", (self.now, self.now))
        self.assertEqual(self.service.resume_paused(), 0)
        self.assertEqual(self.store._one("SELECT stopped_reason FROM mailing_states")["stopped_reason"], "client_transferred")

    def test_disable_button_starts_on_third_message(self):
        self.assertEqual(len(mailing_buttons(2)), 1)
        self.assertEqual(mailing_buttons(3)[1][0]["callback_data"], "mail:disable")

    def test_both_chains_and_long_term_alternation(self):
        group1 = [due_for_step(1, step, self.now) for step in range(1, 7)]
        group2 = [due_for_step(2, step, self.now) for step in range(1, 6)]
        self.assertEqual(group1[0], self.now + 7 * DAY)
        self.assertGreater(group1[5], group1[4])
        self.assertGreater(group2[4], group2[3])
        self.assertNotEqual(message_for(1, 5), message_for(1, 6))
        self.assertEqual(message_for(1, 5), message_for(1, 7))
        self.assertEqual(message_for(2, 5), message_for(2, 8))

    def test_ambiguous_delivery_is_quarantined_not_retried(self):
        self.user()
        service = MailingService(self.store, {"telegram": FakeTransport(fail=True)}, 1)
        service.reconcile_all()
        job = self.store._one("SELECT * FROM mailing_jobs")
        with self.store.db:
            self.store.db.execute("UPDATE mailing_jobs SET due_at=? WHERE id=?", (self.now, job["id"]))
        self.assertFalse(service.deliver_job(job["id"]))
        self.assertEqual(self.store._one("SELECT status FROM mailing_jobs")["status"], "uncertain")
        self.assertEqual(service.due_ids(), [])
        self.assertIn("явная повторная",service.resolve_uncertain(job["id"],False))
        self.assertEqual(self.store._one("SELECT status FROM mailing_jobs")["status"],"backfill_pending")

    def test_restart_expired_sending_claim_becomes_uncertain(self):
        self.user();self.service.reconcile_all()
        with self.store.db:
            self.store.db.execute("UPDATE mailing_jobs SET status='sending',lease_until=?",(self.now-1,))
        self.assertEqual(self.service.recover_expired_claims(),1)
        self.assertEqual(self.store._one("SELECT status FROM mailing_jobs")['status'],'uncertain')
        self.assertEqual(self.transport.sent,[])

    def test_backfill_requires_preview_token_and_honours_limit(self):
        for user_id in ("1", "2", "3"):
            self.user(user_id, self.now - 100 * DAY)
        self.service.reconcile_all()
        preview = self.service.preview_backfill("telegram", "admin", 2, 2)
        self.assertIn("Ничего не отправлено", preview)
        token = preview.rsplit(" ", 1)[-1]
        result = self.service.confirm_backfill(token)
        self.assertIn("2", result)
        self.assertEqual(self.store._one("SELECT count(*) n FROM mailing_jobs WHERE status='backfill_pending'")["n"], 2)


if __name__ == "__main__":
    unittest.main()
