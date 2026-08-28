from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from storage import Storage

LOG = logging.getLogger(__name__)


class CRM(Protocol):
    def add_note(self, lead_id: int, text: str) -> None: ...
    def find_lead(self, full_name: str, phone: str) -> int | None: ...
    def target_stage(self, pipeline_name: str, status_name: str) -> tuple[int, int]: ...
    def move_lead(self, lead_id: int, pipeline_id: int, status_id: int) -> None: ...


@dataclass(slots=True)
class Prompt:
    text: str
    keyboard: list[list[str]] | None
    remove_keyboard: bool = False


class SurveyEngine:
    def __init__(self, store: Storage, crm: CRM | None, target_pipeline: str, target_status: str) -> None:
        self.store, self.crm = store, crm
        self.target_pipeline, self.target_status = target_pipeline, target_status

    def begin(self, platform: str, user_id: str, name: str | None) -> tuple[str, Prompt | None]:
        self.store.touch_user(platform,user_id,name)
        attempt=self.store.active_attempt(platform,user_id)
        if attempt: return "Продолжаем незавершённое тестирование.", self.prompt(attempt)
        test=self.store.enabled_test()
        if not test: return "Сейчас нет доступного теста.", None
        attempt=self.store.start_attempt(platform,user_id,int(test['id']))
        return str(test['name']), self.prompt(attempt)

    def prompt(self, attempt: Any) -> Prompt | None:
        if not attempt['current_question_id']: return None
        q=self.store._one('SELECT * FROM questions WHERE id=?',(attempt['current_question_id'],))
        if not q: return None
        if q['kind']=='text': return Prompt(q['text'],None,True)
        opts=self.store.options(q['id']); numbered='\n'.join(f"{i}. {x['text']}" for i,x in enumerate(opts,1))
        keys=[[str(i)] for i in range(1,len(opts)+1)]
        if q['kind']=='multi_choice': keys.append(['Готово'])
        return Prompt(f"{q['text']}\n\n{numbered}",keys)

    def receive(self, platform: str, user_id: str, text: str) -> tuple[str, Prompt | None]:
        attempt=self.store.active_attempt(platform,user_id)
        if not attempt: return "Нажмите /start, чтобы начать тестирование.",None
        q=self.store._one('SELECT * FROM questions WHERE id=?',(attempt['current_question_id'],))
        if not q: return "Тест уже завершён.",None
        opts=self.store.options(q['id'])
        if q['kind']=='text':
            value=text.strip()
            if not value and q['required']: return "Введите ответ текстом.",self.prompt(attempt)
        elif q['kind']=='single_choice':
            if not text.isdigit() or not 1<=int(text)<=len(opts): return "Выберите номер варианта на клавиатуре.",self.prompt(attempt)
            value=opts[int(text)-1]['text']
        else:
            selected=self._selected(attempt['id'],q['id'])
            if text=='Готово':
                if not selected and q['required']: return "Выберите хотя бы один вариант.",self.prompt(attempt)
                value=selected
            elif text.isdigit() and 1<=int(text)<=len(opts):
                option=opts[int(text)-1]['text']; selected.remove(option) if option in selected else selected.append(option)
                self._set_selected(attempt['id'],q['id'],selected)
                return "Выбрано: " + (', '.join(selected) or 'ничего') + ". Нажмите «Готово».",self.prompt(attempt)
            else: return "Используйте номера вариантов и «Готово».",self.prompt(attempt)
        questions=self.store.test_questions(attempt['test_id']); index=next(i for i,x in enumerate(questions) if x['id']==q['id'])
        next_id=questions[index+1]['id'] if index+1<len(questions) else None
        if not self.store.answer(attempt['id'],q['id'],value,next_id,next_id is None):
            return "Этот ответ уже сохранён. Продолжаем.",self.prompt(self.store.active_attempt(platform,user_id))
        self._capture_identity(attempt,q,value)
        updated=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt['id'],)); assert updated
        if not updated['start_note_sent']:
            self._crm_note(updated, f"Кандидат начал тестирование на должность удаленного юриста через {platform.title()}", 'start_note_sent')
        self._run_actions(updated,opts,value)
        if next_id is None:
            self._crm_note(updated,self.result_text(updated,True),'final_note_sent')
            return "Спасибо! Тестирование завершено.",None
        return "Ответ сохранён.",self.prompt(updated)

    def _selected(self, attempt_id:int, question_id:int) -> list[str]:
        row=self.store._one("SELECT value_json FROM draft_answers WHERE attempt_id=? AND question_id=?",(attempt_id,question_id))
        return json.loads(row['value_json']) if row else []
    def _set_selected(self, attempt_id:int, question_id:int, values:list[str]) -> None:
        with self.store.db:
            self.store.db.execute("INSERT INTO draft_answers(attempt_id,question_id,value_json,updated_at) VALUES(?,?,?,?) ON CONFLICT(attempt_id,question_id) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",(attempt_id,question_id,json.dumps(values,ensure_ascii=False),int(time.time())))
    def _capture_identity(self, attempt:Any,q:Any,value:Any) -> None:
        if q['identity_key']=='full_name': self.store.set_identity(attempt['id'],full_name=str(value))
        if q['identity_key']=='phone': self.store.set_identity(attempt['id'],phone=str(value))
        fresh=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt['id'],))
        if fresh and fresh['full_name'] and fresh['phone'] and not fresh['amo_lead_id'] and self.crm:
            try:
                lead=self.crm.find_lead(fresh['full_name'],fresh['phone'])
                if lead: self.store.set_identity(attempt['id'],lead_id=lead)
                else: LOG.warning('No unambiguous amoCRM lead for attempt %s',attempt['id'])
            except Exception: LOG.exception('Cannot find amoCRM lead for attempt %s',attempt['id'])
    def _crm_note(self, attempt:Any,text:str, flag:str) -> None:
        if not self.crm or not attempt['amo_lead_id'] or attempt[flag]: return
        try: self.crm.add_note(int(attempt['amo_lead_id']),text); self.store.mark(attempt['id'],flag)
        except Exception: LOG.exception('Unable to add amoCRM note for attempt %s',attempt['id'])
    def _run_actions(self,attempt:Any,opts:list[Any],value:Any) -> None:
        if not self.crm or not attempt['amo_lead_id']: return
        selected=set(value if isinstance(value,list) else [value])
        for option in opts:
            if option['text'] not in selected or not option['action_json']: continue
            try:
                action=json.loads(option['action_json'])
                if action.get('type')=='move_stage':
                    if not self.store.claim_action(attempt['id'],option['id'],'move_stage'): continue
                    pipeline,status=self.crm.target_stage(action.get('pipeline',self.target_pipeline),action.get('status',self.target_status))
                    self.crm.move_lead(int(attempt['amo_lead_id']),pipeline,status)
            except Exception: LOG.exception('Action failed for attempt %s',attempt['id'])
    def result_text(self,attempt:Any,completed:bool) -> str:
        lines=['Тестирование кандидата — '+('итоговый результат' if completed else 'промежуточный результат'),f"ФИО: {attempt['full_name'] or 'не указано'}",f"Телефон: {attempt['phone'] or 'не указан'}",'']
        for answer in self.store.answers_with_questions(attempt['id']):
            value=json.loads(answer['value_json']); lines.extend([f"{answer['position']}. {answer['text']}","Ответ: "+(', '.join(value) if isinstance(value,list) else str(value))])
        lines += ['', 'Статус: '+('Тест завершён' if completed else 'не завершён')]
        if not completed: lines += [f"Остановился на вопросе: {attempt['current_question_id']}",f"Последняя активность: {time.strftime('%Y-%m-%d %H:%M',time.localtime(attempt['last_activity_at']))}"]
        return '\n'.join(lines)
    def send_snapshots(self, cutoff:int) -> int:
        sent=0
        for attempt in self.store.stale_attempts(cutoff):
            if attempt['amo_lead_id'] and self.crm:
                try: self.crm.add_note(int(attempt['amo_lead_id']),self.result_text(attempt,False)); self.store.mark(attempt['id'],'snapshot_version',1); sent+=1
                except Exception: LOG.exception('Unable to save inactivity snapshot for %s',attempt['id'])
        return sent
