"""Manager sessions and durable, per-message amoCRM notes."""
from __future__ import annotations

import logging
import json
import re
import time
from html import escape

LOG = logging.getLogger(__name__)
END_MENU = [[{'text': 'Завершить чат', 'callback_data': 'chat:end'}]]


def message_key(platform, update, user_id):
    incoming = update.get('message') or {}
    key = update.get('_event_id')
    if key is None and incoming.get('message_id') is not None:
        key = f"message:{incoming.get('chat', {}).get('id', user_id)}:{incoming['message_id']}"
    if key is None:
        key = update.get('update_id')
    return f'{platform}:{key}' if key is not None else None


def message_content(message):
    text = (message.get('text') or message.get('caption') or '').strip()
    attachments = list(message.get('attachments') or [])
    for kind in ('photo', 'document', 'video', 'voice', 'audio', 'sticker'):
        media = message.get(kind)
        if media:
            media = media[-1] if isinstance(media, list) else media
            attachments.append({'type': kind, 'payload': media})
    if attachments:
        labels = [str(a.get('payload', {}).get('file_name') or a.get('type') or 'вложение') for a in attachments]
        text = '[вложение: ' + ', '.join(labels) + ']' + (' ' + text if text else '')
    return text, attachments


class ChatService:
    def __init__(self, store, config, crm, transports):
        self.store, self.config, self.crm, self.transports = store, config, crm, transports

    def is_manager(self, platform, user_id):
        return user_id in self.config.admin_ids or (platform == 'telegram' and user_id in self.config.manager_ids)

    def active(self, platform, user_id):
        if self.is_manager(platform, user_id):
            return self.store._one("SELECT * FROM chat_sessions WHERE manager_platform=? AND manager_id=? AND status='active'", (platform, user_id))
        return self.store._one("SELECT * FROM chat_sessions WHERE platform=? AND user_id=? AND status IN ('open','active')", (platform, user_id))

    def customer(self, chat):
        attempt = self.store._one("SELECT full_name,amo_lead_id FROM attempts WHERE user_platform=? AND user_id=? ORDER BY id DESC LIMIT 1", (chat['platform'], chat['user_id']))
        user = self.store._one('SELECT display_name FROM users WHERE platform=? AND user_id=?', (chat['platform'], chat['user_id']))
        name = (attempt['full_name'] if attempt else None) or (user['display_name'] if user else None) or chat['user_id']
        channel = 'MAX' if chat['platform'] == 'max' else 'Telegram'
        return f"{name} / {channel} ID {chat['user_id']}"

    def send_to(self, transport, platform, user_id, text, inline=None):
        # The dispatcher records cross-platform sends in the same durable response
        # plan as local state changes. Never perform network I/O in that transaction.
        if hasattr(transport, 'send_to'):
            transport.send_to(platform, user_id, text, inline=inline, preserve=True)
        else:
            target = transport if transport.platform == platform else self.transports[platform]
            target.send(user_id, text, inline=inline, preserve=True)

    def send_text(self, transport, platform, user_id, heading, text):
        # Split before escaping so a long message never splits an HTML entity.
        # History and the CRM note still contain one complete original message.
        prefix = escape(heading) + ':\n'
        chunk, size = [], 0
        for char in text:
            encoded = escape(char)
            if chunk and size + len(encoded) > 3800 - len(prefix):
                self.send_to(transport, platform, user_id, prefix + ''.join(chunk), END_MENU)
                chunk, size = [], 0
            chunk.append(encoded)
            size += len(encoded)
        if chunk:
            self.send_to(transport, platform, user_id, prefix + ''.join(chunk), END_MENU)

    def send_attachments(self, transport, source_platform, platform, user_id, attachments, *, archive_only=False):
        if not attachments:
            return
        if hasattr(transport, 'send_chat_attachments'):
            transport.send_chat_attachments(source_platform, platform, user_id, attachments, archive_only=archive_only)
        else:
            from transports import send_chat_attachments
            send_chat_attachments(self.transports, source_platform, platform, user_id, attachments, archive_only=archive_only)

    def command(self, transport, platform, user_id, argument):
        if user_id not in self.config.admin_ids:
            transport.send(user_id, 'Недостаточно прав.', preserve=True)
            return
        argument = argument.strip()
        if not argument:
            transport.send(user_id, 'Использование: /chat ID_сделки_или_пользователя или /chat @username.\n'
                           'Для точного поиска: /chat lead:123, /chat telegram:123, /chat max:123.', preserve=True)
            return
        with self.store.db:
            explicit = re.fullmatch(r'(lead|amo|amocrm|telegram|tg|max)[:\s]+(\d+)', argument, re.IGNORECASE)
            targets = set()
            if explicit or argument.isdecimal():
                kind, value = (explicit.group(1).lower(), explicit.group(2)) if explicit else ('any', argument)
                value = str(int(value))
                if kind in {'any', 'lead', 'amo', 'amocrm'}:
                    targets.update((row['user_platform'], row['user_id']) for row in self.store.db.execute(
                        'SELECT DISTINCT user_platform,user_id FROM attempts WHERE amo_lead_id=?', (value,)).fetchall())
                if kind in {'any', 'telegram', 'tg', 'max'}:
                    target_platform = 'telegram' if kind in {'telegram', 'tg'} else kind
                    rows = self.store.db.execute('SELECT platform,user_id FROM users WHERE user_id=?' +
                                                 ('' if kind == 'any' else ' AND platform=?'),
                                                 (value,) if kind == 'any' else (value, target_platform)).fetchall()
                    targets.update((row['platform'], row['user_id']) for row in rows)
            elif re.fullmatch(r'@?[A-Za-z0-9_]+', argument):
                targets.update(('telegram', row['user_id']) for row in self.store.db.execute(
                    "SELECT user_id FROM user_handles WHERE platform='telegram' AND username=? COLLATE NOCASE",
                    (argument.lstrip('@'),)).fetchall())
            if not targets:
                transport.send(user_id, 'Пользователь не найден. Он должен сначала написать боту; ID сделки и username ищутся в сохранённых данных.', preserve=True)
                return
            if len(targets) != 1:
                choices = '\n'.join(f'/chat {p}:{uid}' for p, uid in sorted(targets))
                transport.send(user_id, 'Найдено несколько пользователей. Уточните платформу:\n' + escape(choices), preserve=True)
                return
            target_platform, target_user = targets.pop()
            if (target_platform, target_user) == (platform, user_id) or self.is_manager(target_platform, target_user):
                transport.send(user_id, 'Выберите чат пользователя, а не сотрудника.', preserve=True)
                return
            if target_platform not in self.transports:
                transport.send(user_id, 'Транспорт пользователя сейчас недоступен.', preserve=True)
                return
            busy = self.active(platform, user_id)
            if busy and (busy['platform'], busy['user_id']) != (target_platform, target_user):
                transport.send(user_id, f'У вас уже есть активный чат: {escape(self.customer(busy))}. Сначала завершите его командой /endchat.', inline=END_MENU, preserve=True)
                return
            chat = self.store._one("SELECT * FROM chat_sessions WHERE platform=? AND user_id=? AND status IN ('open','active')", (target_platform, target_user))
            if not chat:
                cid = self.store.db.execute('INSERT INTO chat_sessions(platform,user_id,created_at) VALUES(?,?,?)', (target_platform, target_user, int(time.time()))).lastrowid
            else:
                cid = chat['id']
            self.callback(transport, platform, user_id, f'chat:session:{cid}')

    def callback(self, transport, platform, user_id, data):
        if data in {'chat:start', 'chat:start_agent'}:
            if self.is_manager(platform, user_id):
                chat = self.active(platform, user_id)
                transport.send(user_id, f"Открыт диалог: {escape(self.customer(chat))}" if chat else 'Выберите чат кнопкой «Подключиться» в обращении пользователя.', inline=END_MENU, preserve=True)
                return True
            with self.store.db:
                chat = self.active(platform, user_id)
                if not chat:
                    cid = self.store.db.execute('INSERT INTO chat_sessions(platform,user_id,created_at) VALUES(?,?,?)', (platform, user_id, int(time.time()))).lastrowid
                    chat = self.store._one('SELECT * FROM chat_sessions WHERE id=?', (cid,))
                    # Replace the legacy one-message support state without losing history.
                    self.store.db.execute("DELETE FROM agent_sessions WHERE platform=? AND user_id=? AND state='manager_message'", (platform, user_id))
                    if 'telegram' in self.transports:
                        for manager in sorted(self.config.manager_ids):
                            self.send_to(transport, 'telegram', manager, f"Запрос менеджера: {escape(self.customer(chat))}", [[{'text': 'Подключиться', 'callback_data': f"chat:session:{cid}"}]])
            transport.send(user_id, 'Чат с менеджером открыт. Напишите вопрос следующим сообщением, менеджер увидит его здесь.', inline=END_MENU, preserve=True)
            return True
        if data.startswith('chat:session:'):
            if not self.is_manager(platform, user_id):
                transport.send(user_id, 'Недостаточно прав.', preserve=True)
                return True
            try:
                cid = int(data.rsplit(':', 1)[1])
            except ValueError:
                transport.send(user_id, 'Чат не найден.', preserve=True)
                return True
            with self.store.db:
                chat = self.store._one('SELECT * FROM chat_sessions WHERE id=?', (cid,))
                busy = self.active(platform, user_id)
                if not chat or chat['status'] == 'closed':
                    result = 'Чат завершён или не найден.'
                elif busy and busy['id'] != cid:
                    result = f"У вас уже есть активный чат: {self.customer(busy)}. Сначала завершите его."
                elif chat['manager_id'] and (chat['manager_platform'], chat['manager_id']) != (platform, user_id):
                    result = 'Уже подключился другой менеджер.'
                elif chat['platform'] not in self.transports:
                    result = 'Транспорт пользователя сейчас недоступен.'
                elif (chat['manager_platform'], chat['manager_id']) == (platform, user_id):
                    result = f"Открыт диалог: {self.customer(chat)}. Пишите обычными сообщениями."
                else:
                    self.store.db.execute("UPDATE chat_sessions SET manager_platform=?,manager_id=?,status='active' WHERE id=?", (platform, user_id, cid))
                    result = f"Вы подключились к чату с {self.customer(chat)}. Пишите обычными сообщениями."
                    self.send_to(transport, chat['platform'], chat['user_id'], 'Менеджер подключился к диалогу.', END_MENU)
                    for row in self.store.db.execute("SELECT text,attachments_json FROM chat_messages WHERE session_id=? AND sender_role='user' ORDER BY id", (cid,)).fetchall():
                        self.send_text(transport, platform, user_id, self.customer(chat), row['text'])
                        self.send_attachments(transport, chat['platform'], platform, user_id, json.loads(row['attachments_json']))
            transport.send(user_id, escape(result), inline=END_MENU, preserve=True)
            return True
        if data == 'chat:end':
            with self.store.db:
                chat = self.active(platform, user_id)
                if chat:
                    self.store.db.execute("UPDATE chat_sessions SET status='closed',closed_at=? WHERE id=?", (int(time.time()), chat['id']))
                    if self.is_manager(platform, user_id):
                        self.send_to(transport, chat['platform'], chat['user_id'], 'Чат завершён.')
                    elif chat['manager_id']:
                        self.send_to(transport, chat['manager_platform'], chat['manager_id'], f"Чат с {escape(self.customer(chat))} завершён.")
            transport.send(user_id, 'Чат завершён.' if chat else 'Активного чата сейчас нет.', preserve=True)
            return True
        return False

    def relay(self, transport, platform, user_id, text, external_key=None, attachments=None):
        if not text or text.startswith('/'):
            return False
        with self.store.db:
            chat = self.active(platform, user_id)
            if not chat:
                if self.is_manager(platform, user_id) and user_id not in self.config.admin_ids:
                    transport.send(user_id, 'Активного чата сейчас нет. Подключитесь кнопкой в обращении пользователя.', preserve=True)
                    return True
                return False
            role = 'manager' if self.is_manager(platform, user_id) else 'user'
            channel = 'MAX' if chat['platform'] == 'max' else 'Telegram'
            note = (f"Переписка с менеджером ({channel})\n"
                    f"Отправитель: {'Менеджер' if role == 'manager' else 'Пользователь'}\n"
                    f"Клиент: {self.customer(chat)}\nСообщение: {text}\nСессия чата: {chat['id']}")
            attempt = self.store._one('SELECT amo_lead_id FROM attempts WHERE user_platform=? AND user_id=? AND amo_lead_id IS NOT NULL ORDER BY id DESC LIMIT 1', (chat['platform'], chat['user_id']))
            inserted = self.store.db.execute('INSERT OR IGNORE INTO chat_messages(session_id,sender_role,text,external_key,created_at,amo_lead_id,note_text,attachments_json) VALUES(?,?,?,?,?,?,?,?)', (chat['id'], role, text, external_key, int(time.time()), attempt['amo_lead_id'] if attempt else None, note, json.dumps(attachments or [], ensure_ascii=False)))
            if inserted.rowcount != 1:
                return True
            self.send_attachments(transport, platform, platform, user_id, attachments, archive_only=True)
            if role == 'manager':
                self.send_text(transport, chat['platform'], chat['user_id'], 'Менеджер', text)
                self.send_attachments(transport, platform, chat['platform'], chat['user_id'], attachments)
            elif chat['manager_id']:
                self.send_text(transport, chat['manager_platform'], chat['manager_id'], self.customer(chat), text)
                self.send_attachments(transport, platform, chat['manager_platform'], chat['manager_id'], attachments)
            else:
                transport.send(user_id, 'Сообщение сохранено. Менеджер подключится, как только освободится.', inline=END_MENU, preserve=True)
        return True

    def retry_crm_notes(self):
        if not self.crm:
            return
        now = int(time.time())
        rows = self.store.db.execute(
            "SELECT m.id FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id "
            "WHERE (m.crm_status='pending' OR (m.crm_status='processing' AND m.lease_until<?)) "
            "AND (m.amo_lead_id IS NOT NULL OR EXISTS (SELECT 1 FROM attempts a "
            "WHERE a.user_platform=s.platform AND a.user_id=s.user_id AND a.amo_lead_id IS NOT NULL)) "
            "ORDER BY m.id LIMIT 100", (now,)).fetchall()
        for item in rows:
            with self.store.db:
                claimed = self.store.db.execute("UPDATE chat_messages SET crm_status='processing',lease_until=? WHERE id=? AND (crm_status='pending' OR (crm_status='processing' AND lease_until<?))", (now + 300, item['id'], now))
                if claimed.rowcount != 1:
                    continue
                row = self.store._one('SELECT m.*,s.platform,s.user_id FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id WHERE m.id=?', (item['id'],))
                lead = row['amo_lead_id']
                if not lead:
                    attempt = self.store._one('SELECT amo_lead_id FROM attempts WHERE user_platform=? AND user_id=? AND amo_lead_id IS NOT NULL ORDER BY id DESC LIMIT 1', (row['platform'], row['user_id']))
                    lead = attempt['amo_lead_id'] if attempt else None
                    self.store.db.execute('UPDATE chat_messages SET amo_lead_id=? WHERE id=?', (lead, row['id']))
            try:
                if not lead:
                    raise ValueError('Сделка пользователя ещё не привязана')
                marker = f"\u2063lawyer-tester:chat-message:{row['id']}"
                if not self.crm.has_note(int(lead), marker):
                    self.crm.add_note(int(lead), row['note_text'] + '\n' + marker)
            except Exception as exc:
                with self.store.db:
                    self.store.db.execute("UPDATE chat_messages SET crm_status='pending',lease_until=NULL,error_message=? WHERE id=?", (str(exc)[:1000], row['id']))
                if lead:
                    LOG.exception('Chat CRM note sync failed message_id=%s', row['id'])
            else:
                with self.store.db:
                    self.store.db.execute("UPDATE chat_messages SET crm_status='done',lease_until=NULL,error_message=NULL WHERE id=?", (row['id'],))
