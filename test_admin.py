from __future__ import annotations
import tempfile, time, unittest
from admin import Admin
from storage import Storage

class AdminDraftTests(unittest.TestCase):
    def test_broadcast_text_draft_does_not_require_id(self):
        file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False);file.close()
        store=Storage(file.name);admin=Admin(store)
        store.set_draft('telegram','1','cast_text',{'target':'telegram','buttons':[]})
        text,buttons=admin.text('telegram','1','Текст рассылки')
        self.assertIn('Предпросмотр',text)
        self.assertEqual(buttons[0][0]['callback_data'],'a:castsend')

class AdminLeadListTests(unittest.TestCase):
    def setUp(self):
        self.file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False);self.file.close()
        self.store=Storage(self.file.name);self.admin=Admin(self.store,'https://amo.example')
        now=int(time.time())
        with self.store.db:
            test_id=self.store.db.execute('INSERT INTO tests(name,enabled,created_at) VALUES(?,?,?)',('T',1,now)).lastrowid
            qid=self.store.db.execute('INSERT INTO questions(test_id,position,text,kind) VALUES(?,?,?,?)',(test_id,1,'Q','single_choice')).lastrowid
            self.option_id=self.store.db.execute('INSERT INTO options(question_id,position,text) VALUES(?,?,?)',(qid,1,'Yes')).lastrowid
    def tearDown(self):
        self.store.close()
    def _attempt(self, lead_id:int, *, created:bool, moved:bool, status:str='completed'):
        now=int(time.time())
        with self.store.db:
            current_question_id=1 if status!='completed' else None
            attempt_id=self.store.db.execute("INSERT INTO attempts(user_platform,user_id,test_id,started_at,last_activity_at,current_question_id,status,amo_lead_id,full_name,phone,amo_created) VALUES(?,?,?,?,?,?,?,?,?,?,?)",('telegram',str(lead_id),1,now,now,current_question_id,status,lead_id,f'User {lead_id}','79990000000',int(created))).lastrowid
            if moved:self.store.db.execute('INSERT INTO action_executions VALUES(?,?,?,?)',(attempt_id,self.option_id,'move_stage',now))
        return attempt_id
    def test_stats_has_created_and_moved_found_buttons(self):
        self._attempt(100,created=True,moved=False)
        self._attempt(200,created=False,moved=True)
        self._attempt(300,created=False,moved=False,status='active')
        text,buttons=self.admin.callback('telegram','admin','a:stats')
        self.assertIn('Созданные сделки (1)',buttons[0][0]['text'])
        self.assertEqual(buttons[0][0]['callback_data'],'a:created:0')
        self.assertIn('Переведённые найденные (1)',buttons[1][0]['text'])
        self.assertEqual(buttons[1][0]['callback_data'],'a:moved:0')
        self.assertIn('Незавершённые (1)',buttons[2][0]['text'])
        self.assertEqual(buttons[2][0]['callback_data'],'a:unfinished:0')
        self.assertIn('Найдена и переведена сделка: 1',text)
        self.assertIn('Незавершённых со сделкой: 1',text)
    def test_created_leads_are_paginated(self):
        for lead_id in range(100,112):self._attempt(lead_id,created=True,moved=False)
        text,buttons=self.admin.callback('telegram','admin','a:created:0')
        lead_buttons=[row for row in buttons if row[0].get('url')]
        self.assertEqual(len(lead_buttons),10)
        self.assertEqual(buttons[-2][0]['callback_data'],'a:created:1')
        self.assertIn('Страница 1 из 2',text)
        self.assertTrue(lead_buttons[0][0]['url'].endswith('/leads/detail/111'))
    def test_moved_found_leads_are_paginated_and_exclude_created(self):
        for lead_id in range(200,211):self._attempt(lead_id,created=False,moved=True)
        self._attempt(300,created=True,moved=True)
        text,buttons=self.admin.callback('telegram','admin','a:moved:1')
        lead_buttons=[row for row in buttons if row[0].get('url')]
        self.assertEqual(len(lead_buttons),1)
        self.assertEqual(lead_buttons[0][0]['text'],'#200 User 200')
        self.assertEqual(self.store.detailed_stats()['moved_found_leads'],11)
        self.assertEqual(self.store.stats()['moved'],12)
        self.assertEqual(buttons[-2][0]['callback_data'],'a:moved:0')
        self.assertIn('Страница 2 из 2',text)
    def test_unfinished_leads_are_paginated(self):
        for lead_id in range(400,412):self._attempt(lead_id,created=False,moved=False,status='active')
        self._attempt(500,created=False,moved=False,status='completed')
        text,buttons=self.admin.callback('telegram','admin','a:unfinished:1')
        lead_buttons=[row for row in buttons if row[0].get('url')]
        self.assertEqual(len(lead_buttons),2)
        self.assertEqual(lead_buttons[0][0]['text'],'#401 User 401')
        self.assertEqual(buttons[-2][0]['callback_data'],'a:unfinished:0')
        self.assertIn('Страница 2 из 2',text)

class AdminBroadcastTests(unittest.TestCase):
    def test_deliver_both_uses_each_platform_transport(self):
        file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False);file.close()
        store=Storage(file.name);admin=Admin(store)
        now=int(time.time())
        with store.db:
            store.db.execute('INSERT INTO users VALUES(?,?,?,?,?)',('telegram','tg','Tg',now,now))
            store.db.execute('INSERT INTO users VALUES(?,?,?,?,?)',('max','mx','Max',now,now))
        store.set_draft('telegram','admin','cast_confirm',{'target':'both','text':'Hello','buttons':[]})
        class Transport:
            def __init__(self, platform): self.platform=platform; self.sent=[]
            def send_broadcast(self, user_id, payload, buttons): self.sent.append(user_id)
        tg=Transport('telegram'); mx=Transport('max')
        result=admin.deliver('telegram','admin',{'telegram':tg,'max':mx})
        self.assertEqual(tg.sent,['tg'])
        self.assertEqual(mx.sent,['mx'])
        self.assertIn('успешно 2',result)
        store.close()

if __name__=='__main__':unittest.main()
