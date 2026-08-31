from __future__ import annotations
import json
from typing import Any
from storage import Storage

def b(text:str,data:str)->dict[str,str]: return {'text':text,'callback_data':data}
HOME=[[b('Тесты','a:tests'),b('Статистика','a:stats')],[b('Рассылка','a:cast')]]
PAGE_SIZE=10

class Admin:
 def __init__(self,s:Storage,amo_base_url:str=''): self.s=s;self.amo_base_url=amo_base_url.rstrip('/')
 def menu(self): return 'Админка',HOME
 def _lead_page(self,kind:str,page:int,total:int,rows:list)->tuple[str,list]:
  titles={'created':'Созданные сделки','moved':'Переведённые найденные сделки','unfinished':'Незавершённые со сделкой'}
  title=titles.get(kind,'Сделки')
  keys=[]
  for r in rows:
   button={'text':f"#{r['amo_lead_id']} {r['full_name'] or 'Без ФИО'}"}
   if self.amo_base_url:button['url']=f"{self.amo_base_url}/leads/detail/{r['amo_lead_id']}"
   else:button['callback_data']=f'a:{kind}:{page}'
   keys.append([button])
  nav=[]
  if page>0:nav.append(b('← Назад',f'a:{kind}:{page-1}'))
  if (page+1)*PAGE_SIZE<total:nav.append(b('Вперёд →',f'a:{kind}:{page+1}'))
  if nav:keys.append(nav)
  keys.append([b('‹ К статистике','a:stats')])
  pages=max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
  return f'{title}: {total}\nСтраница {page+1} из {pages}',keys
 def callback(self,platform:str,user:str,data:str)->tuple[str,list]|None:
  if data=='a:home':return self.menu()
  if data=='a:tests':
   rows=self.s.db.execute('SELECT * FROM tests ORDER BY id').fetchall(); keys=[[b(('✅ ' if x['enabled'] else '⛔ ')+x['name'],f'a:test:{x["id"]}')] for x in rows]+[[b('＋ Создать тест','a:newtest'),b('‹ Назад','a:home')]]
   return 'Тесты',keys
  if data.startswith('a:test:'):
   t=self.s._one('SELECT * FROM tests WHERE id=?',(data.split(':')[2],));
   return t['name'],[[b('Вкл/выкл',f'a:toggle:{t["id"]}'),b('Название',f'a:rename:{t["id"]}')],[b('Вопросы',f'a:qs:{t["id"]}'),b('Удалить',f'a:deltest:{t["id"]}')],[b('‹ К тестам','a:tests')]]
  if data.startswith('a:toggle:'):
   i=data.split(':')[2];self.s.db.execute('UPDATE tests SET enabled=1-enabled WHERE id=?',(i,));self.s.db.commit();return self.callback(platform,user,f'a:test:{i}')
  if data.startswith('a:rename:') or data=='a:newtest':
   self.s.set_draft(platform,user,'rename_test' if data!='a:newtest' else 'new_test',{'id':data.split(':')[2] if ':' in data else None});return 'Введите название теста.',[[b('Отмена','a:home')]]
  if data.startswith('a:deltest:'):
   self.s.db.execute('DELETE FROM tests WHERE id=?',(data.split(':')[2],));self.s.db.commit();return self.callback(platform,user,'a:tests')
  if data.startswith('a:qs:'):
   tid=data.split(':')[2]; qs=self.s.test_questions(int(tid));return 'Вопросы',[[b(f"{q['position']}. {q['text'][:32]}",f'a:q:{q["id"]}')] for q in qs]+[[b('＋ Вопрос',f'a:newq:{tid}'),b('‹ Назад',f'a:test:{tid}')]]
  if data.startswith('a:q:'):
   q=self.s._one('SELECT * FROM questions WHERE id=?',(data.split(':')[2],));return q['text'],[[b('Текст',f'a:qtext:{q["id"]}'),b('Тип',f'a:qtype:{q["id"]}')],[b('Обязательность',f'a:qreq:{q["id"]}'),b('Порядок',f'a:qpos:{q["id"]}')],[b('Варианты',f'a:opts:{q["id"]}'),b('Удалить',f'a:delq:{q["id"]}')],[b('‹ Назад',f'a:qs:{q["test_id"]}')]]
  if data.startswith('a:qtype:'):
   i=data.split(':')[2];return 'Выберите тип',[[b(x,f'a:settype:{i}:{x}')] for x in ('text','single_choice','multi_choice')]
  if data.startswith('a:settype:'):
   _,_,i,kind=data.split(':');self.s.db.execute('UPDATE questions SET kind=? WHERE id=?',(kind,i));self.s.db.commit();return self.callback(platform,user,f'a:q:{i}')
  if data.startswith('a:qreq:'):
   i=data.split(':')[2];self.s.db.execute('UPDATE questions SET required=1-required WHERE id=?',(i,));self.s.db.commit();return self.callback(platform,user,f'a:q:{i}')
  if data.startswith('a:qtext:') or data.startswith('a:qpos:') or data.startswith('a:newq:'):
   kind='q_text' if ':qtext:' in data else 'q_pos' if ':qpos:' in data else 'new_q';self.s.set_draft(platform,user,kind,{'id':data.split(':')[2]});return 'Введите текст вопроса.' if kind!='q_pos' else 'Введите новый номер вопроса.',[[b('Отмена','a:home')]]
  if data.startswith('a:opts:'):
   qid=data.split(':')[2];opts=self.s.options(int(qid));return 'Варианты',[[b(f"{o['position']}. {o['text'][:35]}",f'a:o:{o["id"]}')] for o in opts]+[[b('＋ Вариант',f'a:newo:{qid}'),b('‹ Назад',f'a:q:{qid}')]]
  if data.startswith('a:o:'):
   o=self.s._one('SELECT * FROM options WHERE id=?',(data.split(':')[2],));return o['text'],[[b('Текст',f'a:otext:{o["id"]}'),b('Порядок',f'a:opos:{o["id"]}')],[b('Action',f'a:action:{o["id"]}'),b('Удалить',f'a:delo:{o["id"]}')],[b('‹ Назад',f'a:opts:{o["question_id"]}')]]
  if data.startswith('a:otext:') or data.startswith('a:opos:') or data.startswith('a:newo:'):
   kind='o_text' if ':otext:' in data else 'o_pos' if ':opos:' in data else 'new_o';self.s.set_draft(platform,user,kind,{'id':data.split(':')[2]});return 'Введите текст варианта.' if kind!='o_pos' else 'Введите новый номер варианта.',[[b('Отмена','a:home')]]
  if data.startswith('a:action:'):
   i=data.split(':')[2];return 'Действие ответа',[[b('Нет',f'a:setact:{i}:none')],[b('Перевести в «Готов к сотрудничеству»',f'a:setact:{i}:move_stage')]]
  if data.startswith('a:setact:'):
   _,_,i,act=data.split(':');self.s.db.execute('UPDATE options SET action_json=? WHERE id=?',(None if act=='none' else json.dumps({'type':act}),i));self.s.db.commit();return self.callback(platform,user,f'a:o:{i}')
  if data.startswith('a:delq:') or data.startswith('a:delo:'):
   table='questions' if ':delq:' in data else 'options';self.s.db.execute(f'DELETE FROM {table} WHERE id=?',(data.split(':')[2],));self.s.db.commit();return self.menu()
  if data=='a:stats':
   x=self.s.detailed_stats(); plats=' | '.join(f"{k}: {v}" for k,v in x['platforms'].items()); return f"Пользователи: {x['users']} ({plats})\nНачали: {x['started']} | Завершили: {x['completed']} | Активны: {x['active']} | Неактивны: {x['abandoned']}\nСегодня/7д/30д: {x['day']}/{x['week']}/{x['month']}\nЗавершение: {x['completion_pct']}% | Среднее: {x['avg_seconds']} сек.\nПереведено: {x['moved']}\nФИО не найдено, создана сделка: {x['created_leads']}\nНайдена и переведена сделка: {x['moved_found_leads']}\nНезавершённых со сделкой: {x['unfinished_leads']}\nРассылок: {sum(r['campaigns'] for r in x['broadcasts'])}",[[b(f"Созданные сделки ({x['created_leads']})",'a:created:0')],[b(f"Переведённые найденные ({x['moved_found_leads']})",'a:moved:0')],[b(f"Незавершённые ({x['unfinished_leads']})",'a:unfinished:0')],[b('‹ Назад','a:home')]]
  if data.startswith('a:created'):
   page=int(data.split(':')[2]) if len(data.split(':'))>2 and data.split(':')[2].isdigit() else 0
   total=self.s.detailed_stats()['created_leads']; rows=self.s.created_leads(PAGE_SIZE,page*PAGE_SIZE)
   return self._lead_page('created',page,total,rows)
  if data.startswith('a:moved'):
   page=int(data.split(':')[2]) if len(data.split(':'))>2 and data.split(':')[2].isdigit() else 0
   total=self.s.detailed_stats()['moved_found_leads']; rows=self.s.moved_found_leads(PAGE_SIZE,page*PAGE_SIZE)
   return self._lead_page('moved',page,total,rows)
  if data.startswith('a:unfinished'):
   page=int(data.split(':')[2]) if len(data.split(':'))>2 and data.split(':')[2].isdigit() else 0
   total=self.s.detailed_stats()['unfinished_leads']; rows=self.s.unfinished_leads(PAGE_SIZE,page*PAGE_SIZE)
   return self._lead_page('unfinished',page,total,rows)
  if data=='a:cast': return 'Кому отправить?',[[b('Telegram','a:castto:telegram'),b('MAX','a:castto:max')],[b('Обе платформы','a:castto:both')],[b('История','a:casthistory'),b('‹ Назад','a:home')]]
  if data.startswith('a:castto:'):
   self.s.set_draft(platform,user,'cast_text',{'target':data.split(':')[2],'buttons':[]});return 'Отправьте текст рассылки. Форматирование Telegram сохраняется при отправке HTML/entities.',[[b('Отмена','a:home')]]
  if data=='a:casthistory':
   rows=self.s.broadcast_history();return '\n'.join(f"#{r['id']} {r['platform']}: ✓{r['sent_count']} ✗{r['failed_count']}" for r in rows) or 'История пуста.',HOME
  return self.menu()
 def text(self,platform:str,user:str,text:str):
  d=self.s.draft(platform,user)
  if not d:return None
  kind,p=d; self.s.clear_draft(platform,user); i=p.get('id')
  with self.s.db:
   if kind=='rename_test':self.s.db.execute('UPDATE tests SET name=? WHERE id=?',(text,i));return self.callback(platform,user,f'a:test:{i}')
   if kind=='new_test':self.s.db.execute('INSERT INTO tests(name,enabled,created_at) VALUES(?,?,strftime("%s","now"))',(text,1));return self.callback(platform,user,'a:tests')
   if kind=='q_text':self.s.db.execute('UPDATE questions SET text=? WHERE id=?',(text,i));return self.callback(platform,user,f'a:q:{i}')
   if kind=='q_pos':self.s.db.execute('UPDATE questions SET position=? WHERE id=?',(int(text),i));return self.menu()
   if kind=='new_q':self.s.db.execute('INSERT INTO questions(test_id,position,text,kind,required) VALUES(?,COALESCE((SELECT MAX(position)+1 FROM questions WHERE test_id=?),1),?,"text",1)',(i,i,text));return self.callback(platform,user,f'a:qs:{i}')
   if kind=='o_text':self.s.db.execute('UPDATE options SET text=? WHERE id=?',(text,i));return self.callback(platform,user,f'a:o:{i}')
   if kind=='o_pos':self.s.db.execute('UPDATE options SET position=? WHERE id=?',(int(text),i));return self.menu()
   if kind=='new_o':self.s.db.execute('INSERT INTO options(question_id,position,text) VALUES(?,COALESCE((SELECT MAX(position)+1 FROM options WHERE question_id=?),1),?)',(i,i,text));return self.callback(platform,user,f'a:opts:{i}')
   if kind=='cast_text':
    p['text']=text;self.s.set_draft(platform,user,'cast_confirm',p);return 'Предпросмотр:\n\n'+text,[[b('Отправить','a:castsend'),b('Отмена','a:home')]]
 def deliver(self,platform:str,user:str,transport:Any)->str:
  d=self.s.draft(platform,user)
  if not d or d[0]!='cast_confirm': return 'Черновик рассылки не найден.'
  _,p=d;bid=self.s.create_broadcast(platform,user,p['target'],{'kind':'text','text':p['text']},p['buttons']);ok=bad=0
  for row in self.s.users(None if p['target']=='both' else p['target']):
   if row['platform']!=transport.platform: continue
   try: transport.send_broadcast(row['user_id'],{'kind':'text','text':p['text']},p['buttons']);ok+=1
   except Exception: bad+=1
  self.s.complete_broadcast(bid,ok,bad);self.s.clear_draft(platform,user);return f'Рассылка завершена: успешно {ok}, ошибок {bad}.'
