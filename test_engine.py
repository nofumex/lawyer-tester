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
    def create_candidate_lead(self,name,phone): return 43


class SurveyTests(unittest.TestCase):
    def setUp(self):
        self.file=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False); self.file.close()
        self.store=Storage(self.file.name); seed_default_test(self.store); self.crm=FakeCRM()
        self.engine=SurveyEngine(self.store,self.crm,'Судебный приказ','Готов к сотрудничеству')
    def test_resume_preserves_current_question(self):
        _,prompt=self.engine.begin('telegram','7','Ivan'); self.assertTrue(prompt.remove_keyboard)
        self.engine.receive('telegram','7','Иванов Иван')
        _,prompt=self.engine.begin('telegram','7','Ivan')
        self.assertEqual(prompt.text, '<b>Номер телефона</b>')
    def test_multi_choice_needs_confirmation(self):
        test=self.store.enabled_test()['id']; questions=self.store.test_questions(test)
        self.assertEqual(len(questions),22)
        q=questions[9]; self.assertEqual(q['kind'],'multi_choice')
        self.assertEqual(len(self.store.options(q['id'])),4)
        self.assertEqual(self.store.test_questions(test)[17]['kind'],'multi_choice')
        self.assertEqual(self.store.test_questions(test)[18]['kind'],'multi_choice')
    def test_multi_choice_inline_toggle_and_done(self):
        test=self.store.enabled_test()['id']; q=self.store.test_questions(test)[9]
        attempt=self.store.start_attempt('telegram','88',test)
        self.store.db.execute('UPDATE attempts SET current_question_id=? WHERE id=?',(q['id'],attempt['id']));self.store.db.commit()
        attempt=self.store.active_attempt('telegram','88'); prompt=self.engine.prompt(attempt)
        self.assertIsNone(prompt.keyboard);self.assertTrue(prompt.inline[-1][0]['text']=='Готово')
        option=self.store.options(q['id'])[0]
        _,prompt,edit=self.engine.receive_callback('telegram','88',f'survey:pick:{attempt["id"]}:{q["id"]}:{option["id"]}')
        self.assertTrue(edit);self.assertEqual(prompt.inline[0][0]['text'],'✅ 1')
        _,next_prompt,_=self.engine.receive_callback('telegram','88',f'survey:done:{attempt["id"]}:{q["id"]}')
        self.assertIn('Выберите условие',next_prompt.text)

if __name__=='__main__': unittest.main()
