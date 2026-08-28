from __future__ import annotations

from storage import Storage


MENU = [[{'text':'Тесты','callback_data':'admin:tests'},{'text':'Статистика','callback_data':'admin:stats'}],[{'text':'Рассылка','callback_data':'admin:broadcast'}]]


class Admin:
    def __init__(self, store: Storage) -> None: self.store=store
    def menu(self) -> tuple[str,list[list[dict[str,str]]]]: return 'Админка: выберите раздел.',MENU
    def callback(self, data:str) -> tuple[str,list[list[dict[str,str]]]|None]:
        if data=='admin:stats':
            s=self.store.stats(); return (f"Пользователи: {s['users']}\nНачали: {s['started']}\nЗавершили: {s['completed']}\nВ процессе: {s['active']}\nБросили: {s['abandoned']}\nНачато сегодня/неделя/месяц: {s['day']}/{s['week']}/{s['month']}",MENU)
        if data=='admin:tests':
            tests=self.store.db.execute('SELECT id,name,enabled FROM tests ORDER BY id').fetchall()
            text='Тесты:\n'+ '\n'.join(f"{x['id']}. {'✅' if x['enabled'] else '⛔'} {x['name']}" for x in tests)
            return text+'\n\nКоманды: test on ID | test off ID | test delete ID',MENU
        if data=='admin:broadcast': return 'Рассылка: отправьте команду broadcast ТЕКСТ. Для форматирования Telegram используйте HTML. Перед отправкой бот покажет предпросмотр и попросит подтвердить.',MENU
        return 'Неизвестное действие.',MENU
    def command(self,text:str) -> str | None:
        # Compact command API permits full test/question/option lifecycle without code changes.
        parts=text.split(' ',3)
        if len(parts)>=3 and parts[0]=='test' and parts[1] in ('on','off','delete'):
            if parts[1]=='delete': self.store.db.execute('DELETE FROM tests WHERE id=?',(parts[2],)); result='Тест удалён.'
            else: self.store.db.execute('UPDATE tests SET enabled=? WHERE id=?',(parts[1]=='on',parts[2])); result='Статус теста обновлён.'
            self.store.db.commit(); return result
        if len(parts)>=4 and parts[0]=='question' and parts[1]=='add':
            # question add TEST_ID TYPE Текст
            test_id,kind=int(parts[2]),parts[3].split(' ',1)[0]; question=parts[3][len(kind):].strip()
            pos=self.store._one('SELECT COALESCE(MAX(position),0)+1 FROM questions WHERE test_id=?',(test_id,))[0]
            self.store.db.execute('INSERT INTO questions(test_id,position,text,kind,required) VALUES(?,?,?,?,1)',(test_id,pos,question,kind)); self.store.db.commit(); return 'Вопрос добавлен.'
        if len(parts)>=3 and parts[0]=='question' and parts[1] in ('delete','move','type','required','edit'):
            qid=int(parts[2]); tail=parts[3] if len(parts)>3 else ''
            if parts[1]=='delete': self.store.db.execute('DELETE FROM questions WHERE id=?',(qid,))
            elif parts[1]=='move': self.store.db.execute('UPDATE questions SET position=? WHERE id=?',(int(tail),qid))
            elif parts[1]=='type' and tail in ('text','single_choice','multi_choice'): self.store.db.execute('UPDATE questions SET kind=? WHERE id=?',(tail,qid))
            elif parts[1]=='required' and tail in ('0','1'): self.store.db.execute('UPDATE questions SET required=? WHERE id=?',(tail,qid))
            elif parts[1]=='edit' and tail: self.store.db.execute('UPDATE questions SET text=? WHERE id=?',(tail,qid))
            else: return 'Неверные параметры вопроса.'
            self.store.db.commit(); return 'Вопрос обновлён.'
        if len(parts)>=4 and parts[0]=='option' and parts[1]=='add':
            qid=int(parts[2]); pos=self.store._one('SELECT COALESCE(MAX(position),0)+1 FROM options WHERE question_id=?',(qid,))[0]
            self.store.db.execute('INSERT INTO options(question_id,position,text) VALUES(?,?,?)',(qid,pos,parts[3])); self.store.db.commit(); return 'Вариант добавлен.'
        if len(parts)>=3 and parts[0]=='option' and parts[1] in ('delete','move','edit','action'):
            oid=int(parts[2]); tail=parts[3] if len(parts)>3 else ''
            if parts[1]=='delete': self.store.db.execute('DELETE FROM options WHERE id=?',(oid,))
            elif parts[1]=='move': self.store.db.execute('UPDATE options SET position=? WHERE id=?',(int(tail),oid))
            elif parts[1]=='edit' and tail: self.store.db.execute('UPDATE options SET text=? WHERE id=?',(tail,oid))
            elif parts[1]=='action' and tail: self.store.db.execute('UPDATE options SET action_json=? WHERE id=?',(tail,oid))
            else: return 'Неверные параметры варианта.'
            self.store.db.commit(); return 'Вариант обновлён.'
        return None
