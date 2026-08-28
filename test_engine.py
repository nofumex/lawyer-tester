from __future__ import annotations

import tempfile
import unittest

from engine import SurveyEngine
from seed import seed_default_test
from storage import Storage


class FakeCRM:
    def __init__(self): self.notes=[]; self.moves=[]
    def find_lead(self,name,phone): return 42 if name=='Иванов Иван' and phone=='79990000000' else None
    def add_note(self,lead,text): self.notes.append((lead,text))
    def target_stage(self,pipeline,status): return (1,2)
    def move_lead(self,lead,pipeline,status): self.moves.append((lead,pipeline,status))


class SurveyTests(unittest.TestCase):
    def setUp(self):
        self.file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); self.file.close()
        self.store=Storage(self.file.name); seed_default_test(self.store); self.crm=FakeCRM()
        self.engine=SurveyEngine(self.store,self.crm,'Судебный приказ','Готов к сотрудничеству')
    def test_resume_answer_and_action_are_idempotent(self):
        _,prompt=self.engine.begin('telegram','7','Ivan'); self.assertTrue(prompt.remove_keyboard)
        self.engine.receive('telegram','7','Иванов Иван')
        self.engine.receive('telegram','7','79990000000')
        self.assertEqual(len(self.crm.notes),1)
        _,prompt=self.engine.begin('telegram','7','Ivan'); self.assertEqual(prompt.text.split('\n')[0],'Большинство кандидатов мы не сможем принять на работу. Готовы ли вы к другому сотрудничеству в случае отказа?')
        self.engine.receive('telegram','7','1')
        self.assertEqual(self.crm.moves,[(42,1,2)])
        self.assertEqual(len(self.crm.notes),2)
    def test_multi_choice_needs_confirmation(self):
        test=self.store.enabled_test()['id']; q=self.store.db.execute('INSERT INTO questions(test_id,position,text,kind,required) VALUES(?,?,?,?,1)',(test,4,'Выберите','multi_choice')).lastrowid
        self.store.db.execute('INSERT INTO options(question_id,position,text) VALUES(?,?,?)',(q,1,'A')); self.store.db.commit()
        self.engine.begin('telegram','8','')
        self.engine.receive('telegram','8','name'); self.engine.receive('telegram','8','phone'); self.engine.receive('telegram','8','2')
        reply,_=self.engine.receive('telegram','8','Готово'); self.assertIn('Выберите',reply)

if __name__=='__main__': unittest.main()
