from __future__ import annotations

import tempfile, time
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
    def test_multi_choice_reply_toggle_and_done(self):
        test=self.store.enabled_test()['id']; q=self.store.test_questions(test)[9]
        attempt=self.store.start_attempt('telegram','88',test)
        self.store.db.execute('UPDATE attempts SET current_question_id=? WHERE id=?',(q['id'],attempt['id']));self.store.db.commit()
        attempt=self.store.active_attempt('telegram','88'); prompt=self.engine.prompt(attempt)
        self.assertEqual(prompt.keyboard[-2],['Готово']);self.assertEqual(prompt.keyboard[-1],['← Назад'])
        _,prompt=self.engine.receive('telegram','88','1')
        self.assertEqual(prompt.keyboard[0][0],'✅1');self.assertIn('✅1.',prompt.text)
        _,prompt=self.engine.receive('telegram','88','✅1')
        self.assertEqual(prompt.keyboard[0][0],'1');self.assertNotIn('✅1.',prompt.text)
        self.engine.receive('telegram','88','1')
        _,next_prompt=self.engine.receive('telegram','88','Готово')
        self.assertIn('Выберите условие',next_prompt.text)
    def test_slow_crm_does_not_block_next_question(self):
        class SlowCRM(FakeCRM):
            def add_note(self,*args):time.sleep(.3)
        self.engine=SurveyEngine(self.store,SlowCRM(),'Судебный приказ','Готов к сотрудничеству')
        test=self.store.enabled_test()['id'];questions=self.store.test_questions(test);attempt=self.store.start_attempt('telegram','slow',test)
        self.store.set_identity(attempt['id'],full_name='Иванов Иван',phone='79990000000',lead_id=42)
        self.store.db.execute('UPDATE attempts SET current_question_id=? WHERE id=?',(questions[2]['id'],attempt['id']));self.store.db.commit()
        started=time.monotonic();_,prompt=self.engine.receive('telegram','slow','1')
        self.assertLess(time.monotonic()-started,.15);self.assertIn('удаленной работы',prompt.text)
    def test_back_returns_to_previous_question(self):
        test=self.store.enabled_test()['id'];questions=self.store.test_questions(test);attempt=self.store.start_attempt('telegram','back',test)
        self.store.db.execute('UPDATE attempts SET current_question_id=? WHERE id=?',(questions[3]['id'],attempt['id']));self.store.db.commit()
        _,prompt=self.engine.receive('telegram','back','← Назад')
        self.assertIn('Какой тип занятости',prompt.text)
    def test_completed_review_can_edit_answer(self):
        test=self.store.enabled_test()['id'];q=self.store.test_questions(test)[2];attempt=self.store.start_attempt('telegram','review',test)
        self.store.db.execute("UPDATE attempts SET status='completed',current_question_id=NULL WHERE id=?",(attempt['id'],));self.store.db.execute('INSERT INTO answers(attempt_id,question_id,value_json,answered_at) VALUES(?,?,?,?)',(attempt['id'],q['id'],'"Частичная занятость"',int(time.time())));self.store.db.commit()
        _,view,edit=self.engine.receive_callback('telegram','review',f'review:view:{attempt["id"]}:{q["id"]}')
        self.assertTrue(edit);self.assertIn('Ваш ответ',view.text)
        _,question,_=self.engine.receive_callback('telegram','review',f'review:edit:{attempt["id"]}:{q["id"]}')
        self.assertEqual(question.keyboard[0],['1','2'])
        _,review=self.engine.receive('telegram','review','2')
        self.assertIn('Тест завершён',review.text)
        self.assertEqual(review.inline[-1][0]['text'],'✅ Все верно')
        _,confirmed,edit=self.engine.receive_callback('telegram','review',f'review:confirm:{attempt["id"]}:0')
        self.assertTrue(edit);self.assertIn('Ответы подтверждены',confirmed.text)

if __name__=='__main__': unittest.main()
