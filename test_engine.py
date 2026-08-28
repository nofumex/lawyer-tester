from __future__ import annotations

import tempfile
import unittest

from engine import SurveyEngine
from seed import Q, seed_default_test
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
    def test_resume_preserves_current_question(self):
        _,prompt=self.engine.begin('telegram','7','Ivan'); self.assertTrue(prompt.remove_keyboard)
        self.engine.receive('telegram','7','Иванов Иван')
        _,prompt=self.engine.begin('telegram','7','Ivan')
        self.assertEqual(prompt.text, 'Номер телефона*')
    def test_multi_choice_needs_confirmation(self):
        test=self.store.enabled_test()['id']; questions=self.store.test_questions(test)
        self.assertEqual(len(questions),22)
        q=questions[9]; self.assertEqual(q['kind'],'multi_choice')
        self.assertEqual(len(self.store.options(q['id'])),4)

if __name__=='__main__': unittest.main()
