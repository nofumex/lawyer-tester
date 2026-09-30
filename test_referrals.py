import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from mailings import MailingService
from referrals import AgentProgram
from storage import Storage


class FakeTransport:
    def __init__(self): self.sent=[]
    def send(self,user_id,text,**kwargs): self.sent.append((str(user_id),text,kwargs))


class FakeCRM:
    def __init__(self): self.moves=[];self.created=[];self.notes=[]
    def target_stage(self,pipeline,status):
        self.moves.append(('target',pipeline,status));return 10,20
    def move_lead(self,lead,pipeline,status): self.moves.append((lead,pipeline,status))
    def find_referral_lead(self,phone,pipeline): return None
    def create_referral_lead(self,**kwargs): self.created.append(kwargs);return 501,601
    def has_note(self,*args): return False
    def add_note(self,*args): self.notes.append(args)


class ReferralTests(unittest.TestCase):
    def setUp(self):
        file=tempfile.NamedTemporaryFile(delete=False);file.close();self.path=file.name
        self.store=Storage(self.path);self.now=int(time.time())
        with self.store.db:self.store.db.execute("INSERT INTO users VALUES('telegram','1','Agent',?,?)",(self.now,self.now))
        self.transport=FakeTransport();self.transports={'telegram':self.transport}
        self.mailings=MailingService(self.store,self.transports,1);self.mailings.reconcile_all()
        self.config=SimpleNamespace(a7_offer_url='',default_bonus_per_client=10000,second_level_bonus=5000,
            telegram_bot_username='a7_bot',max_bot_link='https://max.ru/a7',manager_contact_url='',
            admin_ids=frozenset({'99'}),referral_pipeline='[A7] TG / Max - Боты',referral_status='')
        self.program=AgentProgram(self.store,self.config,None,self.mailings,self.transports)

    def tearDown(self):
        self.program.close();self.store.close();os.unlink(self.path)

    def test_interest_is_temporary_but_self_join_is_permanent(self):
        self.assertTrue(self.program.handle_callback(self.transport,'telegram','1','mail:interest'))
        state=self.store._one("SELECT * FROM mailing_states")
        self.assertIsNotNone(state['resume_at']);self.assertIsNone(state['stopped_reason'])
        self.program.handle_callback(self.transport,'telegram','1','agent:rules')
        self.assertIsNone(self.store._one("SELECT stopped_reason FROM mailing_states")['stopped_reason'])
        self.program.handle_callback(self.transport,'telegram','1','agent:menu')
        self.assertIsNone(self.store._one("SELECT stopped_reason FROM mailing_states")['stopped_reason'])
        self.program.handle_callback(self.transport,'telegram','1','agent:join')
        self.assertEqual(self.store._one("SELECT stopped_reason FROM mailing_states")['stopped_reason'],'joined_agent_program')
        self.assertEqual(self.store._one("SELECT count(*) n FROM agent_profiles")['n'],1)

    def test_full_new_client_form_is_idempotent_and_stops_ads(self):
        self.program.handle_callback(self.transport,'telegram','1','agent:new_client')
        self.assertTrue(self.program.handle_text(self.transport,'telegram','1','Иван'))
        self.assertTrue(self.program.handle_text(self.transport,'telegram','1','8 999 111-22-33'))
        early=self.store._one("SELECT * FROM referral_leads")
        self.assertEqual((early['client_name'],early['phone'],early['status']),('Иван','+79991112233','collecting'))
        for value in ('знакомый','да','8 999 222-33-44'):
            self.assertTrue(self.program.handle_text(self.transport,'telegram','1',value))
        self.program.handle_callback(self.transport,'telegram','1','agent:warn:yes')
        self.program.handle_callback(self.transport,'telegram','1','agent:call:no')
        lead=self.store._one("SELECT * FROM referral_leads")
        self.assertEqual((lead['phone'],lead['warning_answer'],lead['call_phone_answer']),('+79991112233','Да','Нет'))
        self.assertEqual(self.store._one("SELECT stopped_reason FROM mailing_states")['stopped_reason'],'client_transferred')
        self.program.handle_callback(self.transport,'telegram','1','agent:new_client')
        for value in ('Иван снова','8 999 111-22-33','знакомый','да','8 999 222-33-44'):
            self.program.handle_text(self.transport,'telegram','1',value)
        self.assertEqual(self.store._one("SELECT count(*) n FROM referral_leads")['n'],1)

    def test_referral_link_button_is_absent_from_all_menus(self):
        for menu in (self.program.menu(),self.program.profile_menu(),self.program.main_menu()):
            labels=[item['text'] for row in menu for item in row]
            self.assertNotIn('Реферальная ссылка',labels)

    def test_amocrm_deal_is_created_after_name_and_phone_then_supplemented(self):
        crm=FakeCRM();program=AgentProgram(self.store,self.config,crm,self.mailings,self.transports)
        try:
            program.handle_callback(self.transport,'telegram','1','agent:new_client')
            program.handle_text(self.transport,'telegram','1','Ранний клиент')
            program.handle_text(self.transport,'telegram','1','8 999 444-55-66')
            self.assertEqual(self.store._one("SELECT status FROM referral_leads WHERE phone='+79994445566'")['status'],'collecting')
            program.handle_text(self.transport,'telegram','1','коллега')
            program.handle_text(self.transport,'telegram','1','можно')
            program.handle_text(self.transport,'telegram','1','8 999 777-88-99')
            program.handle_callback(self.transport,'telegram','1','agent:warn:no')
            program.executor.shutdown(wait=True)
            lead=self.store._one("SELECT * FROM referral_leads WHERE phone='+79994445566'")
            self.assertEqual(lead['amo_lead_id'],501)
            self.assertEqual(len(crm.created),1)
            notes='\n'.join(text for _,text in crm.notes)
            self.assertIn('Связь клиента с агентом: коллега',notes)
            self.assertIn('Можно сообщить источник контакта: можно',notes)
        finally:
            # executor is already stopped above; shutdown remains idempotent.
            program.close()

    def test_referral_link_and_two_levels_are_deduplicated(self):
        self.program.join('telegram','1','self')
        with self.store.db:
            self.store.db.execute("INSERT INTO users VALUES('telegram','2','Second',?,?)",(self.now,self.now))
            self.store.db.execute("INSERT INTO users VALUES('telegram','3','Third',?,?)",(self.now,self.now))
        self.assertTrue(self.program.attach_referrer('telegram','2','telegram','1'))
        self.program.join('telegram','2','mailing')
        self.assertFalse(self.program.attach_referrer('telegram','2','telegram','1'))
        self.assertTrue(self.program.attach_referrer('telegram','3','telegram','2'))
        self.assertEqual(self.store._one("SELECT count(*) n FROM referrals WHERE referrer_user_id='1' AND level=2")['n'],1)
        self.program.handle_callback(self.transport,'telegram','1','agent:ref_url')
        self.assertIn('https://t.me/a7_bot?start=ref_telegram_1',self.transport.sent[-1][1])

    def test_bonus_accrual_display_and_payment_notification(self):
        self.program.join('telegram','1','self')
        result=self.program.admin_command(self.transport,'telegram','99','/bonus telegram 1 12000 - договор')
        self.assertIn('начислен',result)
        self.program.handle_callback(self.transport,'telegram','1','agent:bonuses')
        self.assertIn('12000',self.transport.sent[-1][1])
        bonus_id=self.store._one("SELECT id FROM bonuses")['id']
        self.program.admin_command(self.transport,'telegram','99',f'/bonus_paid {bonus_id}')
        self.assertEqual(self.store._one("SELECT status FROM bonuses")['status'],'paid')

    def test_amocrm_uses_only_a7_pipelines_and_keeps_associations(self):
        crm=FakeCRM();program=AgentProgram(self.store,self.config,crm,self.mailings,self.transports)
        try:
            with self.store.db:
                test_id=self.store.db.execute("INSERT INTO tests(name,enabled,created_at) VALUES('T',1,?)",(self.now,)).lastrowid
                self.store.db.execute("INSERT INTO attempts(user_platform,user_id,test_id,started_at,last_activity_at,status,amo_lead_id) VALUES('telegram','1',?,?,?,'completed',77)",(test_id,self.now,self.now))
                lead_id=self.store.db.execute("INSERT INTO referral_leads(submission_key,agent_platform,agent_user_id,source,platform,client_name,phone,status,created_at,updated_at) VALUES('crm','telegram','1','agent_form','telegram','Client','+79990000000','submitted',?,?)",(self.now,self.now)).lastrowid
            program._sync_interest('telegram','1')
            program._sync_lead(lead_id)
            row=self.store._one("SELECT * FROM referral_leads WHERE id=?",(lead_id,))
            self.assertIn(('target','HH-юристы','Подтвердил готовность в боте'),crm.moves)
            self.assertEqual(crm.created[0]['pipeline_name'],'[A7] TG / Max - Боты')
            self.assertEqual((row['agent_platform'],row['agent_user_id'],row['source'],row['platform'],row['amo_lead_id']),('telegram','1','agent_form','telegram',501))
            program._sync_lead(lead_id)
            self.assertEqual(len(crm.created),1)
        finally:
            program.close()


if __name__ == '__main__': unittest.main()
