from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape
from dataclasses import dataclass
from typing import Any, Protocol

from storage import Storage

LOG = logging.getLogger(__name__)


class CRM(Protocol):
    def add_note(self, lead_id: int, text: str) -> None: ...
    def find_lead(self, full_name: str, phone: str) -> int | None: ...
    def create_candidate_lead(self, full_name: str, phone: str) -> int: ...
    def target_stage(self, pipeline_name: str, status_name: str) -> tuple[int, int]: ...
    def move_lead(self, lead_id: int, pipeline_id: int, status_id: int) -> None: ...


@dataclass(slots=True)
class Prompt:
    text: str
    keyboard: list[list[str]] | None
    remove_keyboard: bool = False
    inline: list[list[dict[str, str]]] | None = None


class SurveyEngine:
    def __init__(self, store: Storage, crm: CRM | None, target_pipeline: str, target_status: str) -> None:
        self.store, self.crm = store, crm
        self.target_pipeline, self.target_status = target_pipeline, target_status
        self._crm_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='amocrm')

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
        question=escape(q['text'].replace('*','').rstrip())
        position=int(q['position'])
        if q['kind']=='text':
            inline=[[{'text':'← Назад','callback_data':f'survey:back:{attempt["id"]}:{q["id"]}'}]] if position>1 else None
            return Prompt(f"<b>{question}</b>",None,True,inline)
        opts=self.store.options(q['id'])
        selected=set(self._selected(attempt['id'],q['id'])) if q['kind']=='multi_choice' else set()
        numbered='\n'.join(f"{'✅' if x['text'] in selected else ''}{i}. {escape(x['text'])}" for i,x in enumerate(opts,1))
        keys=[[f"{'✅' if x['text'] in selected else ''}{i}" for i,x in enumerate(opts,1)]]
        if q['kind']=='multi_choice': keys.append(['Готово'])
        if position>1: keys.append(['← Назад'])
        return Prompt(f"<b>{question}</b>\n\n{numbered}",keys,False,None)

    def review_prompt(self,attempt:Any)->Prompt:
        questions=self.store.test_questions(attempt['test_id'])
        buttons=[[{'text':str(q['position']),'callback_data':f'review:view:{attempt["id"]}:{q["id"]}'} for q in questions[i:i+6]] for i in range(0,len(questions),6)]
        buttons.append([{'text':'✅ Все верно','callback_data':f'review:confirm:{attempt["id"]}:0'}])
        return Prompt('<b>Тест завершён</b>\n\nПроверьте ответы. Нажмите номер вопроса, чтобы посмотреть или изменить ответ.',None,True,buttons)

    def receive_callback(self, platform:str,user_id:str,data:str) -> tuple[str,Prompt|None,bool]:
        parts=data.split(':')
        if parts[0]=='review': return self._review_callback(platform,user_id,parts)
        if len(parts)<4 or parts[0]!='survey': return 'Кнопка устарела.',None,False
        action,attempt_id,question_id=parts[1],int(parts[2]),int(parts[3])
        attempt=self.store.active_attempt(platform,user_id)
        if not attempt or attempt['id']!=attempt_id or attempt['current_question_id']!=question_id:
            return 'Этот вопрос уже обработан.',self.prompt(attempt) if attempt else None,False
        q=self.store._one('SELECT * FROM questions WHERE id=?',(question_id,)); opts=self.store.options(question_id)
        if action=='back':
            prompt=self._go_back(attempt);return '',prompt,False
        if action=='pick':
            if len(parts)!=5: return 'Некорректная кнопка.',self.prompt(attempt),False
            option=next((x for x in opts if x['id']==int(parts[4])),None)
            if not option:return 'Вариант больше недоступен.',self.prompt(attempt),False
            if q['kind']=='multi_choice':
                selected=self._selected(attempt_id,question_id)
                selected.remove(option['text']) if option['text'] in selected else selected.append(option['text'])
                self._set_selected(attempt_id,question_id,selected)
                return 'Выбор обновлён.',self.prompt(attempt),True
            reply,prompt=self.receive(platform,user_id,str(opts.index(option)+1));return reply,prompt,False
        if action=='done' and q['kind']=='multi_choice':
            reply,prompt=self.receive(platform,user_id,'Готово');return reply,prompt,False
        return 'Некорректная кнопка.',self.prompt(attempt),False

    def _review_callback(self,platform:str,user_id:str,parts:list[str])->tuple[str,Prompt|None,bool]:
        if len(parts)<4:return 'Некорректная кнопка.',None,False
        action,attempt_id,question_id=parts[1],int(parts[2]),int(parts[3])
        attempt=self.store._one('SELECT * FROM attempts WHERE id=? AND user_platform=? AND user_id=?',(attempt_id,platform,user_id))
        if not attempt:return 'Результат не найден.',None,False
        if action=='confirm':
            with self.store.db:self.store.db.execute('UPDATE attempts SET review_confirmed=1 WHERE id=?',(attempt_id,))
            return '',Prompt('<b>Спасибо! Ответы подтверждены.</b>',None,True,[]),True
        if action=='view':
            q=self.store._one('SELECT * FROM questions WHERE id=?',(question_id,));a=self.store._one('SELECT value_json FROM answers WHERE attempt_id=? AND question_id=?',(attempt_id,question_id))
            value=json.loads(a['value_json']) if a else 'Нет ответа';shown=', '.join(value) if isinstance(value,list) else str(value)
            text=f"<b>{escape(q['text'].replace('*','').rstrip())}</b>\n\nВаш ответ: {escape(shown)}"
            inline=[[{'text':'Изменить ответ','callback_data':f'review:edit:{attempt_id}:{question_id}'}],[{'text':'← К списку','callback_data':f'review:list:{attempt_id}:{question_id}'}]]
            return '',Prompt(text,None,True,inline),True
        if action=='list':return '',self.review_prompt(attempt),True
        if action=='edit':
            with self.store.db:
                self.store.db.execute("UPDATE attempts SET status='active',current_question_id=?,edit_question_id=? WHERE id=?",(question_id,question_id,attempt_id))
                answer=self.store._one('SELECT value_json FROM answers WHERE attempt_id=? AND question_id=?',(attempt_id,question_id))
                q=self.store._one('SELECT kind FROM questions WHERE id=?',(question_id,))
                if answer and q['kind']=='multi_choice':self.store.db.execute('INSERT OR REPLACE INTO draft_answers VALUES(?,?,?,?)',(attempt_id,question_id,answer['value_json'],int(time.time())))
            return '',self.prompt(self.store._one('SELECT * FROM attempts WHERE id=?',(attempt_id,))),False
        return 'Некорректная кнопка.',None,False

    def _go_back(self,attempt:Any)->Prompt:
        questions=self.store.test_questions(attempt['test_id']);index=next(i for i,x in enumerate(questions) if x['id']==attempt['current_question_id'])
        if index<=0:return self.prompt(attempt)
        previous=questions[index-1]
        with self.store.db:
            self.store.db.execute('DELETE FROM answers WHERE attempt_id=? AND question_id=?',(attempt['id'],previous['id']))
            self.store.db.execute('DELETE FROM draft_answers WHERE attempt_id=? AND question_id=?',(attempt['id'],previous['id']))
            self.store.db.execute('UPDATE attempts SET current_question_id=?,last_activity_at=? WHERE id=?',(previous['id'],int(time.time()),attempt['id']))
        return self.prompt(self.store._one('SELECT * FROM attempts WHERE id=?',(attempt['id'],)))

    def receive(self, platform: str, user_id: str, text: str) -> tuple[str, Prompt | None]:
        attempt=self.store.active_attempt(platform,user_id)
        if not attempt: return "Нажмите /start, чтобы начать тестирование.",None
        q=self.store._one('SELECT * FROM questions WHERE id=?',(attempt['current_question_id'],))
        if not q: return "Тест уже завершён.",None
        opts=self.store.options(q['id'])
        if text.strip()=='← Назад': return '',self._go_back(attempt)
        if q['kind']=='text':
            value=text.strip()
            if not value and q['required']: return "Введите ответ текстом.",self.prompt(attempt)
        elif q['kind']=='single_choice':
            if not text.isdigit() or not 1<=int(text)<=len(opts): return "Выберите номер варианта на клавиатуре.",self.prompt(attempt)
            value=opts[int(text)-1]['text']
        else:
            selected=self._selected(attempt['id'],q['id'])
            clean=text.removeprefix('✅').strip()
            if clean.casefold()=='готово':
                if not selected and q['required']: return "Выберите хотя бы один вариант.",self.prompt(attempt)
                value=selected
            elif clean.isdigit() and 1<=int(clean)<=len(opts):
                option=opts[int(clean)-1]['text']; selected.remove(option) if option in selected else selected.append(option)
                self._set_selected(attempt['id'],q['id'],selected)
                return "",self.prompt(attempt)
            else: return "Используйте номера вариантов и «Готово».",self.prompt(attempt)
        if attempt['edit_question_id']==q['id']:
            self.store.replace_answer(attempt['id'],q['id'],value)
            completed_attempt=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt['id'],))
            return '',self.review_prompt(completed_attempt)
        questions=self.store.test_questions(attempt['test_id']); index=next(i for i,x in enumerate(questions) if x['id']==q['id'])
        next_id=questions[index+1]['id'] if index+1<len(questions) else None
        if not self.store.answer(attempt['id'],q['id'],value,next_id,next_id is None):
            return "Этот ответ уже сохранён. Продолжаем.",self.prompt(self.store.active_attempt(platform,user_id))
        self._capture_identity(attempt,q,value)
        updated=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt['id'],)); assert updated
        if self.crm:self._crm_executor.submit(self._sync_crm_after_answer,attempt['id'],platform,list(opts),value,next_id is None)
        if next_id is None:
            return "",self.review_prompt(updated)
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
    def _sync_crm_after_answer(self,attempt_id:int,platform:str,opts:list[Any],value:Any,completed:bool)->None:
        fresh=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt_id,))
        if not fresh or not self.crm:return
        if fresh['full_name'] and fresh['phone'] and not fresh['amo_lead_id'] and self.store.claim_amo_link(attempt_id):
            try:
                lead=self.crm.find_lead(fresh['full_name'],fresh['phone'])
                if lead:self.store.set_identity(attempt_id,lead_id=lead)
                else:self.store.set_identity(attempt_id,lead_id=self.crm.create_candidate_lead(fresh['full_name'],fresh['phone']),amo_created=True)
            except Exception:LOG.exception('Cannot find/create amoCRM lead for attempt %s',attempt_id)
            finally:self.store.release_amo_link(attempt_id)
        fresh=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt_id,))
        if not fresh or not fresh['amo_lead_id']:return
        if not fresh['start_note_sent']:self._crm_note(fresh,f"Кандидат начал тестирование на должность удаленного юриста через {platform.title()}",'start_note_sent')
        self._run_actions(fresh,opts,value)
        if completed:
            fresh=self.store._one('SELECT * FROM attempts WHERE id=?',(attempt_id,))
            self._crm_note(fresh,self.result_text(fresh,True),'final_note_sent')
            if fresh['amo_created']:
                try:
                    pipeline,status=self.crm.target_stage('HH-юристы','Прошел тест (собес)');self.crm.move_lead(int(fresh['amo_lead_id']),pipeline,status)
                except Exception:LOG.exception('Unable to move created lead after test')
    def resume_crm(self)->None:
        if not self.crm:return
        for row in self.store.db.execute("SELECT id,user_platform,status FROM attempts WHERE amo_lead_id IS NULL AND full_name IS NOT NULL AND phone IS NOT NULL"):
            self._crm_executor.submit(self._sync_crm_after_answer,row['id'],row['user_platform'],[],None,row['status']=='completed')
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
