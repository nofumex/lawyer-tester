from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


class Storage:
    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(Path(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS users(platform TEXT NOT NULL,user_id TEXT NOT NULL,display_name TEXT,created_at INTEGER NOT NULL,last_seen_at INTEGER NOT NULL,PRIMARY KEY(platform,user_id));
        CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY,test_id INTEGER NOT NULL REFERENCES tests(id) ON DELETE CASCADE,position INTEGER NOT NULL,text TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('text','single_choice','multi_choice')),required INTEGER NOT NULL DEFAULT 1,identity_key TEXT,UNIQUE(test_id,position));
        CREATE TABLE IF NOT EXISTS options(id INTEGER PRIMARY KEY,question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,position INTEGER NOT NULL,text TEXT NOT NULL,action_json TEXT,UNIQUE(question_id,position));
        CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,user_platform TEXT NOT NULL,user_id TEXT NOT NULL,test_id INTEGER NOT NULL REFERENCES tests(id),started_at INTEGER NOT NULL,last_activity_at INTEGER NOT NULL,current_question_id INTEGER,status TEXT NOT NULL CHECK(status IN ('active','completed','abandoned')),amo_lead_id INTEGER,full_name TEXT,phone TEXT,start_note_sent INTEGER NOT NULL DEFAULT 0,snapshot_version INTEGER NOT NULL DEFAULT 0,final_note_sent INTEGER NOT NULL DEFAULT 0,UNIQUE(user_platform,user_id,id));
        CREATE INDEX IF NOT EXISTS idx_attempt_active ON attempts(status,last_activity_at);
        CREATE TABLE IF NOT EXISTS answers(id INTEGER PRIMARY KEY,attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,question_id INTEGER NOT NULL REFERENCES questions(id),value_json TEXT NOT NULL,answered_at INTEGER NOT NULL,UNIQUE(attempt_id,question_id));
        CREATE TABLE IF NOT EXISTS draft_answers(attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,question_id INTEGER NOT NULL REFERENCES questions(id),value_json TEXT NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(attempt_id,question_id));
        CREATE TABLE IF NOT EXISTS action_executions(attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,option_id INTEGER NOT NULL REFERENCES options(id) ON DELETE CASCADE,action_type TEXT NOT NULL,executed_at INTEGER NOT NULL,PRIMARY KEY(attempt_id,option_id,action_type));
        CREATE TABLE IF NOT EXISTS broadcasts(id INTEGER PRIMARY KEY,platform TEXT,source_platform TEXT NOT NULL,source_user_id TEXT NOT NULL,payload_json TEXT NOT NULL,buttons_json TEXT NOT NULL,created_at INTEGER NOT NULL,sent_count INTEGER NOT NULL DEFAULT 0,failed_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS admin_drafts(platform TEXT NOT NULL,user_id TEXT NOT NULL,kind TEXT NOT NULL,payload_json TEXT NOT NULL,updated_at INTEGER NOT NULL,PRIMARY KEY(platform,user_id));
        """)
        if 'amo_created' not in {r[1] for r in self.db.execute('PRAGMA table_info(attempts)')}:
            self.db.execute('ALTER TABLE attempts ADD COLUMN amo_created INTEGER NOT NULL DEFAULT 0')
        if 'amo_link_in_progress' not in {r[1] for r in self.db.execute('PRAGMA table_info(attempts)')}:
            self.db.execute('ALTER TABLE attempts ADD COLUMN amo_link_in_progress INTEGER NOT NULL DEFAULT 0')
        if 'edit_question_id' not in {r[1] for r in self.db.execute('PRAGMA table_info(attempts)')}:
            self.db.execute('ALTER TABLE attempts ADD COLUMN edit_question_id INTEGER')
        if 'review_confirmed' not in {r[1] for r in self.db.execute('PRAGMA table_info(attempts)')}:
            self.db.execute('ALTER TABLE attempts ADD COLUMN review_confirmed INTEGER NOT NULL DEFAULT 0')
        self.db.execute('UPDATE attempts SET amo_link_in_progress=0 WHERE amo_link_in_progress=1 AND amo_lead_id IS NULL')
        self.db.commit()
        self.db.commit()

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None: return self.db.execute(sql,args).fetchone()
    def touch_user(self, platform: str, user_id: str, name: str | None) -> None:
        now=int(time.time()); self.db.execute("INSERT INTO users VALUES(?,?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET display_name=excluded.display_name,last_seen_at=excluded.last_seen_at",(platform,user_id,name,now,now)); self.db.commit()
    def enabled_test(self) -> sqlite3.Row | None: return self._one("SELECT * FROM tests WHERE enabled=1 ORDER BY id LIMIT 1")
    def test_questions(self, test_id: int) -> list[sqlite3.Row]: return self.db.execute("SELECT * FROM questions WHERE test_id=? ORDER BY position,id",(test_id,)).fetchall()
    def options(self, question_id: int) -> list[sqlite3.Row]: return self.db.execute("SELECT * FROM options WHERE question_id=? ORDER BY position,id",(question_id,)).fetchall()
    def active_attempt(self, platform: str, user_id: str) -> sqlite3.Row | None: return self._one("SELECT * FROM attempts WHERE user_platform=? AND user_id=? AND status='active' ORDER BY id DESC LIMIT 1",(platform,user_id))
    def start_attempt(self, platform: str, user_id: str, test_id: int) -> sqlite3.Row:
        now=int(time.time()); qs=self.test_questions(test_id); current=qs[0]['id'] if qs else None
        self.db.execute("INSERT INTO attempts(user_platform,user_id,test_id,started_at,last_activity_at,current_question_id,status) VALUES(?,?,?,?,?,?, 'active')",(platform,user_id,test_id,now,now,current)); self.db.commit(); return self.active_attempt(platform,user_id)  # type: ignore[return-value]
    def answer(self, attempt_id: int, question_id: int, value: Any, next_question_id: int | None, completed: bool) -> bool:
        now=int(time.time())
        try:
            with self.db:
                self.db.execute("INSERT INTO answers(attempt_id,question_id,value_json,answered_at) VALUES(?,?,?,?)",(attempt_id,question_id,json.dumps(value,ensure_ascii=False),now))
                self.db.execute("UPDATE attempts SET current_question_id=?,last_activity_at=?,status=?,snapshot_version=0 WHERE id=?",(next_question_id,now,'completed' if completed else 'active',attempt_id))
            return True
        except sqlite3.IntegrityError: return False
    def replace_answer(self,attempt_id:int,question_id:int,value:Any)->None:
        now=int(time.time())
        with self.db:
            self.db.execute('INSERT INTO answers(attempt_id,question_id,value_json,answered_at) VALUES(?,?,?,?) ON CONFLICT(attempt_id,question_id) DO UPDATE SET value_json=excluded.value_json,answered_at=excluded.answered_at',(attempt_id,question_id,json.dumps(value,ensure_ascii=False),now))
            self.db.execute("UPDATE attempts SET status='completed',current_question_id=NULL,edit_question_id=NULL,last_activity_at=? WHERE id=?",(now,attempt_id))
            self.db.execute('DELETE FROM draft_answers WHERE attempt_id=? AND question_id=?',(attempt_id,question_id))
    def set_identity(self, attempt_id:int, full_name: str | None=None, phone: str | None=None, lead_id: int | None=None, amo_created:bool|None=None) -> None:
        row=self._one("SELECT full_name,phone,amo_lead_id FROM attempts WHERE id=?",(attempt_id,)); assert row
        self.db.execute("UPDATE attempts SET full_name=?,phone=?,amo_lead_id=?,amo_created=COALESCE(?,amo_created) WHERE id=?",(full_name or row['full_name'],phone or row['phone'],lead_id if lead_id is not None else row['amo_lead_id'],None if amo_created is None else int(amo_created),attempt_id)); self.db.commit()
    def mark(self, attempt_id:int, column:str, value: int=1) -> None: self.db.execute(f"UPDATE attempts SET {column}=? WHERE id=?",(value,attempt_id)); self.db.commit()
    def claim_amo_link(self,attempt_id:int)->bool:
        with self.db:
            cursor=self.db.execute('UPDATE attempts SET amo_link_in_progress=1 WHERE id=? AND amo_lead_id IS NULL AND amo_link_in_progress=0',(attempt_id,))
        return cursor.rowcount==1
    def release_amo_link(self,attempt_id:int)->None:
        with self.db:self.db.execute('UPDATE attempts SET amo_link_in_progress=0 WHERE id=?',(attempt_id,))
    def claim_action(self, attempt_id:int, option_id:int, action_type:str) -> bool:
        try:
            with self.db: self.db.execute('INSERT INTO action_executions VALUES(?,?,?,?)',(attempt_id,option_id,action_type,int(time.time())))
            return True
        except sqlite3.IntegrityError: return False
    def answers_with_questions(self, attempt_id:int) -> list[sqlite3.Row]: return self.db.execute("SELECT q.position,q.text,a.value_json FROM answers a JOIN questions q ON q.id=a.question_id WHERE a.attempt_id=? ORDER BY q.position",(attempt_id,)).fetchall()
    def stale_attempts(self, cutoff: int) -> list[sqlite3.Row]: return self.db.execute("SELECT * FROM attempts WHERE status='active' AND last_activity_at<=? AND snapshot_version=0",(cutoff,)).fetchall()
    def users(self, platform: str | None=None) -> Iterable[sqlite3.Row]: return self.db.execute("SELECT * FROM users" + (" WHERE platform=?" if platform else ""), (() if not platform else (platform,)))
    def set_draft(self, platform:str,user_id:str,kind:str,payload:dict[str,Any]) -> None:
        with self.db:self.db.execute("INSERT INTO admin_drafts VALUES(?,?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET kind=excluded.kind,payload_json=excluded.payload_json,updated_at=excluded.updated_at",(platform,user_id,kind,json.dumps(payload,ensure_ascii=False),int(time.time())))
    def draft(self, platform:str,user_id:str) -> tuple[str,dict[str,Any]]|None:
        row=self._one('SELECT kind,payload_json FROM admin_drafts WHERE platform=? AND user_id=?',(platform,user_id)); return (row['kind'],json.loads(row['payload_json'])) if row else None
    def clear_draft(self, platform:str,user_id:str) -> None:
        with self.db:self.db.execute('DELETE FROM admin_drafts WHERE platform=? AND user_id=?',(platform,user_id))
    def create_broadcast(self, platform:str, user_id:str, target:str, payload:dict[str,Any], buttons:list[list[dict[str,str]]]) -> int:
        with self.db:return self.db.execute('INSERT INTO broadcasts(platform,source_platform,source_user_id,payload_json,buttons_json,created_at) VALUES(?,?,?,?,?,?)',(target,platform,user_id,json.dumps(payload,ensure_ascii=False),json.dumps(buttons,ensure_ascii=False),int(time.time()))).lastrowid
    def complete_broadcast(self, bid:int, ok:int, failed:int) -> None:
        with self.db:self.db.execute('UPDATE broadcasts SET sent_count=?,failed_count=? WHERE id=?',(ok,failed,bid))
    def broadcast_history(self) -> list[sqlite3.Row]: return self.db.execute('SELECT * FROM broadcasts ORDER BY id DESC LIMIT 20').fetchall()
    def detailed_stats(self, inactivity_seconds:int=1800) -> dict[str,Any]:
        base=self.stats(inactivity_seconds)
        base['platforms']={r['platform']:r['n'] for r in self.db.execute('SELECT platform,count(*) n FROM users GROUP BY platform')}
        base['completion_pct']=round(100*base['completed']/base['started'],1) if base['started'] else 0
        row=self._one("SELECT avg(last_activity_at-started_at) FROM attempts WHERE status='completed'"); base['avg_seconds']=round(row[0] or 0)
        last=self._one('SELECT q.position,q.text,count(*) n FROM attempts a JOIN questions q ON q.id=a.current_question_id GROUP BY q.id ORDER BY n DESC LIMIT 1');base['last_question']=dict(last) if last else None
        base['questions']=[dict(r) for r in self.db.execute('SELECT q.position,q.text,count(a.id) answers FROM questions q LEFT JOIN answers a ON a.question_id=q.id GROUP BY q.id ORDER BY q.position')]
        base['options']=[dict(r) for r in self.db.execute("SELECT o.question_id,o.position,o.text,count(a.id) answers FROM options o LEFT JOIN answers a ON a.question_id=o.question_id AND (a.value_json='\"'||o.text||'\"' OR a.value_json LIKE '%"+'"'+"'||o.text||'"+'"'+"%') GROUP BY o.id ORDER BY o.question_id,o.position")]
        base['broadcasts']=[dict(r) for r in self.db.execute('SELECT platform,sum(sent_count) sent,sum(failed_count) failed,count(*) campaigns FROM broadcasts GROUP BY platform')]
        base['created_leads']=self._one('SELECT count(*) FROM attempts WHERE amo_created=1 AND amo_lead_id IS NOT NULL')[0]
        return base
    def created_leads(self)->list[sqlite3.Row]:return self.db.execute('SELECT id,full_name,phone,amo_lead_id,started_at FROM attempts WHERE amo_created=1 AND amo_lead_id IS NOT NULL ORDER BY id DESC').fetchall()
    def stats(self, inactivity_seconds: int = 1800) -> dict[str, Any]:
        now=int(time.time()); day=now-86400; week=now-604800; month=now-2592000
        count=lambda q,a=(): self._one(q,a)[0]
        abandoned=count("SELECT count(*) FROM attempts WHERE status='active' AND last_activity_at<=?",(now-inactivity_seconds,))
        return {'users':count('SELECT count(*) FROM users'),'started':count('SELECT count(*) FROM attempts'),'completed':count("SELECT count(*) FROM attempts WHERE status='completed'"),'active':count("SELECT count(*) FROM attempts WHERE status='active'"),'abandoned':abandoned,'day':count('SELECT count(*) FROM attempts WHERE started_at>=?',(day,)),'week':count('SELECT count(*) FROM attempts WHERE started_at>=?',(week,)),'month':count('SELECT count(*) FROM attempts WHERE started_at>=?',(month,)),'moved':count("SELECT count(DISTINCT attempt_id) FROM action_executions WHERE action_type='move_stage'")}
