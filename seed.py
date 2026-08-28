from __future__ import annotations

import json
import time
from storage import Storage

TEST_NAME = 'Тестирование на должность удаленного юриста Юридической компании "A7 Консалт"'
SPECIAL_QUESTION = 'Большинство кандидатов мы не сможем принять на работу. Готовы ли вы к другому сотрудничеству в случае отказа?'
SPECIAL_ANSWER = 'Да, готов(а) передавать контакты людей на банкротство за вознаграждение в 10000-15000 рублей как агент'


def seed_default_test(store: Storage) -> None:
    if store._one('SELECT id FROM tests LIMIT 1'): return
    with store.db:
        cursor=store.db.execute('INSERT INTO tests(name,enabled,created_at) VALUES(?,?,?)',(TEST_NAME,1,int(time.time())))
        test_id=cursor.lastrowid
        questions=[('Укажите ваши ФИО.', 'text', 'full_name'),('Укажите ваш номер телефона.', 'text', 'phone'),(SPECIAL_QUESTION,'single_choice',None)]
        for position,(text,kind,key) in enumerate(questions,1):
            qid=store.db.execute('INSERT INTO questions(test_id,position,text,kind,required,identity_key) VALUES(?,?,?,?,?,?)',(test_id,position,text,kind,1,key)).lastrowid
            if text==SPECIAL_QUESTION:
                options=[SPECIAL_ANSWER,'Нет, не готов(а)']
                for i,option in enumerate(options,1):
                    action=json.dumps({'type':'move_stage'}) if option==SPECIAL_ANSWER else None
                    store.db.execute('INSERT INTO options(question_id,position,text,action_json) VALUES(?,?,?,?)',(qid,i,option,action))
