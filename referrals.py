from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from html import escape
from typing import Any
from urllib.parse import quote

from chat import ChatService

LOG = logging.getLogger(__name__)


def button(text: str, callback: str | None = None, url: str | None = None) -> dict[str, str]:
    return {"text": text, **({"url": url} if url else {"callback_data": callback or "agent:menu"})}


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return ""


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


class AgentProgram:
    def __init__(self, store: Any, config: Any, crm: Any, mailings: Any, transports: dict[str, Any]) -> None:
        self.store, self.config, self.crm, self.mailings, self.transports = store, config, crm, mailings, transports
        self.chat = ChatService(store, config, crm, transports)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="referral")

    def close(self) -> None:
        self.executor.shutdown(wait=True)

    def run(self, stop_event: threading.Event) -> None:
        """Retry durable amoCRM work; operation keys keep restarts idempotent."""
        while not stop_event.is_set():
            try:
                self.chat.retry_crm_notes()
                for row in self.store.db.execute(
                    "SELECT id FROM referral_leads WHERE status<>'draft' AND (amo_sync_status IS NULL OR amo_sync_status='failed') ORDER BY id LIMIT 100"
                ).fetchall():
                    self._sync_lead(int(row["id"]))
                for row in self.store.db.execute(
                    "SELECT id FROM referral_leads WHERE amo_lead_id IS NOT NULL AND (relation_to_agent IS NOT NULL OR source_permission IS NOT NULL OR agent_payout_phone IS NOT NULL OR warning_answer IS NOT NULL OR call_phone_answer IS NOT NULL) ORDER BY id LIMIT 100"
                ).fetchall():
                    self._sync_followup(int(row["id"]), "relation_to_agent", "Связь клиента с агентом")
                    self._sync_followup(int(row["id"]), "source_permission", "Можно сообщить источник контакта")
                    self._sync_followup(int(row["id"]), "agent_payout_phone", "Телефон агента для выплаты")
                    self._sync_followup(int(row["id"]), "warning_answer", "Получится предупредить знакомого")
                    self._sync_followup(int(row["id"]), "call_phone_answer", "Получится передать номер звонящего менеджера")
                for row in self.store.db.execute(
                    "SELECT platform,user_id FROM agent_profiles WHERE is_agent=1 AND joined_source='mailing' ORDER BY joined_at LIMIT 100"
                ).fetchall():
                    self._sync_interest(str(row["platform"]), str(row["user_id"]))
            except Exception:
                LOG.exception("Partner-program background retry failed")
            stop_event.wait(60)

    @staticmethod
    def _safe_send(transport: Any, user_id: str, text: str, inline: Any = None) -> None:
        try:
            transport.send(user_id, text, inline=inline)
        except Exception:
            LOG.exception("Deferred partner-program notification failed user_id=%s", user_id)

    def _notify_new_lead(self, platform: str, lead_id: int, agent_user_id: str,
                         client_name: str, phone: str) -> None:
        # MANAGER_IDS always contains Telegram account IDs, including when the
        # client was submitted through MAX.
        transport = self.transports.get("telegram")
        if not transport:
            LOG.warning("Cannot notify managers about lead %s: Telegram transport is unavailable", lead_id)
            return
        agent = self.store._one(
            "SELECT u.display_name,h.username FROM users u LEFT JOIN user_handles h "
            "ON h.platform=u.platform AND h.user_id=u.user_id WHERE u.platform=? AND u.user_id=?",
            (platform, agent_user_id),
        )
        lead = self.store._one("SELECT * FROM referral_leads WHERE id=?", (lead_id,))
        agent_name = str(agent["display_name"] or agent_user_id) if agent else agent_user_id
        platform_name = "MAX" if platform == "max" else "Telegram"
        created = time.strftime("%d.%m.%Y %H:%M", time.localtime(int(lead["created_at"]))) if lead else "неизвестно"
        relation = str(lead["relation_to_agent"] or "не указана") if lead else "не указана"
        payout_phone = str(lead["agent_payout_phone"] or "не указан") if lead else "не указан"
        text = (
            "<b>Новый клиент от юриста</b>\n\n"
            f"<b>Клиент от агента #{lead_id}</b>\n"
            "<b>Статус:</b> Новая\n"
            f"<b>Дата:</b> {created}\n\n"
            f"<b>Контакт:</b> {escape(client_name)}\n"
            f"<b>Телефон:</b> <code>{escape(phone)}</code>\n"
            f"<b>Связь с агентом:</b> {escape(relation)}\n"
            f"<b>Телефон агента для выплаты:</b> <code>{escape(payout_phone)}</code>\n\n"
            f"<b>Агент:</b> {escape(agent_name)}\n"
            f"<b>{platform_name} ID:</b> <code>{escape(agent_user_id)}</code>"
        )
        for manager_id in self.config.manager_ids:
            manager_id = str(manager_id)
            now = int(time.time())
            with self.store.db:
                claim = self.store.db.execute(
                    "INSERT OR IGNORE INTO manager_lead_notifications(lead_id,manager_id,claimed_at) VALUES(?,?,?)",
                    (lead_id, manager_id, now),
                )
            if claim.rowcount != 1:
                continue
            try:
                transport.send(manager_id, text, preserve=True)
            except Exception:
                with self.store.db:
                    self.store.db.execute(
                        "DELETE FROM manager_lead_notifications WHERE lead_id=? AND manager_id=? AND sent_at IS NULL",
                        (lead_id, manager_id),
                    )
                LOG.exception("Deferred partner-program notification failed user_id=%s", manager_id)
            else:
                with self.store.db:
                    self.store.db.execute(
                        "UPDATE manager_lead_notifications SET sent_at=? WHERE lead_id=? AND manager_id=?",
                        (int(time.time()), lead_id, manager_id),
                    )

    def _session(self, platform: str, user_id: str) -> tuple[str, dict[str, Any]] | None:
        row = self.store._one("SELECT state,data_json FROM agent_sessions WHERE platform=? AND user_id=?", (platform, user_id))
        return (str(row["state"]), json.loads(row["data_json"])) if row else None

    def _set_session(self, platform: str, user_id: str, state: str, data: dict[str, Any]) -> None:
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO agent_sessions VALUES(?,?,?,?,?) ON CONFLICT(platform,user_id) DO UPDATE SET state=excluded.state,data_json=excluded.data_json,updated_at=excluded.updated_at",
                (platform, user_id, state, json.dumps(data, ensure_ascii=False), int(time.time())),
            )

    def _clear_session(self, platform: str, user_id: str) -> None:
        with self.store.db:
            self.store.db.execute("DELETE FROM agent_sessions WHERE platform=? AND user_id=?", (platform, user_id))

    def _profile(self, platform: str, user_id: str) -> Any:
        return self.store._one("SELECT * FROM agent_profiles WHERE platform=? AND user_id=?", (platform, user_id))

    def join(self, platform: str, user_id: str, source: str) -> bool:
        now = int(time.time())
        with self.store.db:
            current = self._profile(platform, user_id)
            self.store.db.execute(
                "INSERT INTO agent_profiles(platform,user_id,is_agent,joined_at,joined_source,created_at,updated_at) VALUES(?,?,1,?,?,?,?) "
                "ON CONFLICT(platform,user_id) DO UPDATE SET is_agent=1,joined_at=COALESCE(agent_profiles.joined_at,excluded.joined_at),"
                "joined_source=COALESCE(agent_profiles.joined_source,excluded.joined_source),updated_at=excluded.updated_at",
                (platform, user_id, now, source, now, now),
            )
        if source == "self":
            self.mailings.disable(platform, user_id, "joined_agent_program")
        return not bool(current and current["is_agent"])

    def attach_referrer(self, platform: str, user_id: str, ref_platform: str, ref_user_id: str) -> bool:
        if (platform, user_id) == (ref_platform, ref_user_id):
            return False
        referrer = self._profile(ref_platform, ref_user_id)
        if not referrer or not referrer["is_agent"]:
            return False
        now = int(time.time())
        with self.store.db:
            profile = self._profile(platform, user_id)
            if profile and profile["referrer_user_id"]:
                return False
            self.store.db.execute(
                "INSERT INTO agent_profiles(platform,user_id,is_agent,referrer_platform,referrer_user_id,created_at,updated_at) VALUES(?,?,0,?,?,?,?) "
                "ON CONFLICT(platform,user_id) DO UPDATE SET referrer_platform=CASE WHEN agent_profiles.referrer_user_id IS NULL THEN excluded.referrer_platform ELSE agent_profiles.referrer_platform END,"
                "referrer_user_id=CASE WHEN agent_profiles.referrer_user_id IS NULL THEN excluded.referrer_user_id ELSE agent_profiles.referrer_user_id END,updated_at=excluded.updated_at",
                (platform, user_id, ref_platform, ref_user_id, now, now),
            )
            self.store.db.execute(
                "INSERT OR IGNORE INTO referrals(referrer_platform,referrer_user_id,referred_platform,referred_user_id,level,created_at) VALUES(?,?,?,?,1,?)",
                (ref_platform, ref_user_id, platform, user_id, now),
            )
            parent = self._profile(ref_platform, ref_user_id)
            if parent and parent["referrer_user_id"]:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO referrals(referrer_platform,referrer_user_id,referred_platform,referred_user_id,level,created_at) VALUES(?,?,?,?,2,?)",
                    (parent["referrer_platform"], parent["referrer_user_id"], platform, user_id, now),
                )
        return True

    def menu(self) -> list[list[dict[str, str]]]:
        offer = button("Ознакомиться с офертой", url=self.config.a7_offer_url) if self.config.a7_offer_url else button("Ознакомиться с офертой", "agent:offer")
        return [
            [button("+ Новый клиент", "agent:new_client")],
            [button("Правила программы", "agent:rules"), offer],
            [button("Заработанные бонусы", "agent:bonuses"), button("Профиль", "profile:show")],
            [button("Связь с менеджером", "chat:start_agent")],
            [button("Главное меню", "user:main")],
        ]

    @staticmethod
    def main_menu() -> list[list[dict[str, str]]]:
        return [[button("Пройти или продолжить тестирование", "user:test")], [button("Партнёрская программа", "agent:menu")]]

    def profile_menu(self) -> list[list[dict[str, str]]]:
        return [[button("Мои рефералы", "agent:referrals")],
                [button("+ Новый клиент", "agent:new_client")],
                [button("Связь с менеджером", "chat:start_agent")],
                [button("Главное меню", "user:main")]]

    def welcome_text(self) -> str:
        return (
            "<b>Партнёрская программа «А7 Консалт»</b>\n\n"
            "Вы можете рекомендовать нашу компанию людям с долговой нагрузкой, которым может подойти процедура банкротства физических лиц, и получать вознаграждение за успешные рекомендации.\n\n"
            "<b>Как это работает:</b>\n"
            "• вы передаёте контакт человека или отправляете ему реферальную ссылку;\n"
            "• команда «А7 Консалт» связывается с ним, проводит консультацию и оценивает ситуацию;\n"
            "• после заключения договора вам начисляется бонус.\n\n"
            f"<b>Вознаграждение:</b> от {money(self.config.default_bonus_per_client)} ₽ за клиента.\n"
            f"<b>Второй уровень:</b> дополнительный бонус {money(self.config.second_level_bonus)} ₽, если клиента привёл ваш агент.\n\n"
            "Вам не нужно консультировать клиента или вести дело — юридическую работу берёт на себя А7 Консалт."
        )

    def rules_text(self) -> str:
        return (
            "<b>Правила партнёрской программы А7 Консалт</b>\n\n"
            "1. Передайте контакт клиента через форму или отправьте реферальную ссылку.\n"
            "2. Мы проведём консультацию и проверим, подходит ли человеку банкротство.\n"
            "3. После заключения договора администратор начислит бонус.\n\n"
            f"За прямого клиента — от {money(self.config.default_bonus_per_client)} ₽. "
            f"За клиента агента второго уровня — {money(self.config.second_level_bonus)} ₽.\n\n"
            "Подходят обращения по кредитам, микрозаймам, просрочкам и исполнительным производствам. "
            "Чтобы начать, нажмите <b>+ Новый клиент</b> или получите реферальную ссылку."
        )

    def _ref_link(self, platform: str, user_id: str) -> str:
        payload = quote(f"ref_{platform}_{user_id}")
        if platform == "telegram" and self.config.telegram_bot_username:
            return f"https://t.me/{self.config.telegram_bot_username}?start={payload}"
        if platform == "max" and self.config.max_bot_link:
            separator = "&" if "?" in self.config.max_bot_link else "?"
            return f"{self.config.max_bot_link}{separator}start={payload}"
        return ""

    def _profile_text(self, platform: str, user_id: str) -> str:
        user = self.store._one("SELECT * FROM users WHERE platform=? AND user_id=?", (platform, user_id))
        direct = self.store._one("SELECT count(*) n FROM referrals WHERE referrer_platform=? AND referrer_user_id=? AND level=1", (platform, user_id))["n"]
        second = self.store._one("SELECT count(*) n FROM referrals WHERE referrer_platform=? AND referrer_user_id=? AND level=2", (platform, user_id))["n"]
        leads = self.store._one("SELECT count(*) n FROM referral_leads WHERE agent_platform=? AND agent_user_id=? AND status<>'draft'", (platform, user_id))["n"]
        totals = self._bonus_totals(platform, user_id)
        registered = time.strftime("%d.%m.%Y", time.localtime(int(user["created_at"]))) if user else "неизвестно"
        return (
            f"<b>Профиль</b>\n\n<b>Имя:</b> {escape(str(user['display_name'] or 'Не указано')) if user else 'Не указано'}\n"
            f"<b>Статус:</b> {'Агент' if self._profile(platform,user_id) and self._profile(platform,user_id)['is_agent'] else 'Пользователь'}\n"
            f"<b>Регистрация:</b> {registered}\n\n<b>Партнёрские показатели</b>\n"
            f"• Прямые рефералы: {direct}\n• Второй уровень: {second}\n• Переданные клиенты: {leads}\n\n"
            f"<b>Бонусы</b>\n• Начислено: {totals['total']} ₽\n• Выплачено: {totals['paid']} ₽\n• Ожидает выплаты: {totals['pending']} ₽"
        )

    def _bonus_totals(self, platform: str, user_id: str) -> dict[str, int]:
        totals = {"total": 0, "paid": 0, "pending": 0, "canceled": 0}
        for row in self.store.db.execute("SELECT status,COALESCE(sum(amount),0) amount FROM bonuses WHERE agent_platform=? AND agent_user_id=? GROUP BY status", (platform, user_id)):
            totals[str(row["status"])] = int(row["amount"])
            if row["status"] != "canceled":
                totals["total"] += int(row["amount"])
        return totals

    def _sync_interest(self, platform: str, user_id: str) -> None:
        if not self.crm:
            return
        attempt = self.store._one("SELECT * FROM attempts WHERE user_platform=? AND user_id=? AND amo_lead_id IS NOT NULL ORDER BY id DESC LIMIT 1", (platform, user_id))
        if not attempt:
            return
        key = f"referral-interest:{platform}:{user_id}"
        if not self.store.claim_crm_operation(key):
            return
        try:
            pipeline, status = self.crm.target_stage("HH-юристы", "Подтвердил готовность в боте")
            self.crm.move_lead(int(attempt["amo_lead_id"]), pipeline, status)
        except Exception:
            self.store.fail_crm_operation(key)
            LOG.exception("Cannot move interested lawyer to amoCRM stage")
        else:
            self.store.finish_crm_operation(key)

    def _sync_lead(self, lead_id: int) -> None:
        if not self.crm:
            return
        lead = self.store._one("SELECT * FROM referral_leads WHERE id=?", (lead_id,))
        if not lead or not lead["phone"]:
            return
        key = f"referral-lead:{lead_id}"
        if not self.store.claim_crm_operation(key):
            return
        marker = f"\u2063lawyer-tester:{key}"
        agent = self.store._one(
            "SELECT u.display_name,h.username FROM users u LEFT JOIN user_handles h "
            "ON h.platform=u.platform AND h.user_id=u.user_id WHERE u.platform=? AND u.user_id=?",
            (str(lead["agent_platform"]), str(lead["agent_user_id"])),
        )
        agent_name = str(agent["display_name"] or "Не указано") if agent else "Не указано"
        agent_username = f"@{agent['username']}" if agent and agent["username"] else "не указан"
        platform_name = "MAX" if lead["agent_platform"] == "max" else "Telegram"
        note = (
            "Заявка партнёрской программы А7 Консалт\n"
            f"Юрист: {agent_name}\n"
            f"Username юриста: {agent_username}\n"
            f"Платформа юриста: {platform_name}\n"
            "Источник заявки: форма «+ Новый клиент»\n"
            f"Связь с агентом: {lead['relation_to_agent'] or 'не указана'}\n"
            f"Можно назвать источник: {lead['source_permission'] or 'не указано'}\n"
            f"Телефон агента для выплаты: {lead['agent_payout_phone'] or 'не указан'}{marker}"
        )
        try:
            existing = self.crm.find_referral_lead(str(lead["phone"]), self.config.referral_pipeline)
            if existing:
                amo_lead_id, contact_id = existing, None
                if not self.crm.has_note(existing, marker):
                    self.crm.add_note(existing, note)
            else:
                amo_lead_id, contact_id = self.crm.create_referral_lead(
                    client_name=str(lead["client_name"]), phone=str(lead["phone"]),
                    pipeline_name=self.config.referral_pipeline, status_name=self.config.referral_status, note=note,
                )
            with self.store.db:
                self.store.db.execute("UPDATE referral_leads SET amo_lead_id=?,amo_contact_id=?,amo_sync_status='done',amo_sync_error=NULL,updated_at=? WHERE id=?", (amo_lead_id, contact_id, int(time.time()), lead_id))
        except Exception as exc:
            self.store.fail_crm_operation(key)
            with self.store.db:
                self.store.db.execute("UPDATE referral_leads SET amo_sync_status='failed',amo_sync_error=?,updated_at=? WHERE id=?", (str(exc)[:1000], int(time.time()), lead_id))
            LOG.exception("Cannot sync referral lead %s", lead_id)
        else:
            self.store.finish_crm_operation(key)
            self._sync_followup(lead_id, "relation_to_agent", "Связь клиента с агентом")
            self._sync_followup(lead_id, "source_permission", "Можно сообщить источник контакта")
            self._sync_followup(lead_id, "agent_payout_phone", "Телефон агента для выплаты")
            self._sync_followup(lead_id, "warning_answer", "Получится предупредить знакомого")
            self._sync_followup(lead_id, "call_phone_answer", "Получится передать номер звонящего менеджера")

    def _sync_followup(self, lead_id: int, column: str, label: str) -> None:
        allowed = {"relation_to_agent", "source_permission", "agent_payout_phone", "warning_answer", "call_phone_answer"}
        if not self.crm or column not in allowed:
            return
        lead = self.store._one("SELECT * FROM referral_leads WHERE id=?", (lead_id,))
        if not lead or not lead["amo_lead_id"] or not lead[column]:
            return
        key = f"referral-followup:{lead_id}:{column}"
        if not self.store.claim_crm_operation(key):
            return
        marker = f"\u2063lawyer-tester:{key}"
        note = f"{label}: {lead[column]}{marker}"
        try:
            if not self.crm.has_note(int(lead["amo_lead_id"]), marker):
                self.crm.add_note(int(lead["amo_lead_id"]), note)
        except Exception:
            self.store.fail_crm_operation(key)
            LOG.exception("Cannot sync referral follow-up lead_id=%s field=%s", lead_id, column)
        else:
            self.store.finish_crm_operation(key)

    def _ensure_collecting_lead(self, platform: str, user_id: str, data: dict[str, Any]) -> int | None:
        now = int(time.time())
        phone_normalized = re.sub(r"\D", "", data["phone"])
        duplicate = self.store._one(
            "SELECT id FROM referral_leads WHERE phone_normalized=? AND submission_key<>? AND status<>'draft'",
            (phone_normalized, data["submission_key"]),
        )
        if duplicate:
            return None
        with self.store.db:
            self.store.db.execute(
                "INSERT OR IGNORE INTO referral_leads(submission_key,agent_platform,agent_user_id,source,platform,client_name,phone,phone_normalized,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?, 'collecting',?,?)",
                (data["submission_key"], platform, user_id, "agent_form", platform, data["client_name"], data["phone"], phone_normalized, now, now),
            )
            lead = self.store._one("SELECT id FROM referral_leads WHERE submission_key=?", (data["submission_key"],))
            if lead:
                self.store.db.execute(
                    "UPDATE referral_leads SET client_name=?,phone=?,phone_normalized=?,status=CASE WHEN status='draft' THEN 'collecting' ELSE status END,updated_at=? WHERE id=?",
                    (data["client_name"], data["phone"], phone_normalized, now, lead["id"]),
                )
        return int(lead["id"]) if lead else None

    def handle_callback(self, transport: Any, platform: str, user_id: str, data: str) -> bool:
        if data == "mail:disable":
            self.mailings.disable(platform, user_id)
            transport.send(user_id, "Напоминания отключены")
            return True
        if data == "mail:interest":
            self.mailings.interest(platform, user_id)
            self.join(platform, user_id, "mailing")
            self.executor.submit(self._sync_interest, platform, user_id)
            transport.send(user_id, "Спасибо за интерес! Рассылка приостановлена. Ниже — всё необходимое для начала сотрудничества.")
            transport.send(user_id, self.welcome_text(), inline=self.menu())
            return True
        if data == "agent:join":
            self.join(platform, user_id, "self")
            transport.send(user_id, self.welcome_text(), inline=self.menu())
            return True
        if data == "agent:menu":
            profile = self._profile(platform, user_id)
            if not profile or not profile["is_agent"]:
                self.join(platform, user_id, "self")
            transport.send(user_id, self.welcome_text(), inline=self.menu())
            return True
        if data == "user:main":
            transport.send(user_id, "<b>Главное меню А7 Консалт</b>", inline=self.main_menu())
            return True
        if data == "agent:rules":
            transport.send(user_id, self.rules_text(), inline=self.menu())
            return True
        if data == "agent:offer":
            transport.send(user_id, "Ссылка на оферту А7 Консалт пока не настроена. Сообщите администратору значение A7_OFFER_URL.", inline=self.menu())
            return True
        if data == "agent:ref_url":
            if not self._profile(platform, user_id) or not self._profile(platform, user_id)["is_agent"]:
                self.join(platform, user_id, "self")
            link = self._ref_link(platform, user_id)
            text = f"<b>Ваша реферальная ссылка</b>\n\n<code>{escape(link)}</code>" if link else "Реферальная ссылка для этой платформы пока не настроена администратором."
            transport.send(user_id, text, inline=self.menu())
            return True
        if data == "profile:show":
            transport.send(user_id, self._profile_text(platform, user_id), inline=self.profile_menu())
            return True
        if data == "agent:referrals":
            rows = self.store.db.execute(
                "SELECT r.*,u.display_name FROM referrals r LEFT JOIN users u ON u.platform=r.referred_platform AND u.user_id=r.referred_user_id "
                "WHERE r.referrer_platform=? AND r.referrer_user_id=? ORDER BY r.id DESC LIMIT 10", (platform, user_id)
            ).fetchall()
            lines = ["<b>Последние рефералы</b>", ""]
            lines += [f"• Уровень {row['level']}: {escape(str(row['display_name'] or row['referred_user_id']))} ({row['referred_platform']})" for row in rows] or ["Рефералов пока нет."]
            transport.send(user_id, "\n".join(lines), inline=self.profile_menu())
            return True
        if data == "agent:bonuses":
            totals = self._bonus_totals(platform, user_id)
            rows = self.store.db.execute("SELECT * FROM bonuses WHERE agent_platform=? AND agent_user_id=? ORDER BY id DESC LIMIT 10", (platform, user_id)).fetchall()
            lines = ["<b>Заработанные бонусы</b>", "", f"Всего начислено: {totals['total']} ₽", f"Выплачено: {totals['paid']} ₽", f"Ожидает выплаты: {totals['pending']} ₽", "", "Последние начисления:"]
            lines += [f"• {row['amount']} ₽ — {escape(str(row['status']))}: {escape(str(row['comment'] or 'без комментария'))}" for row in rows] or ["Начислений пока нет."]
            transport.send(user_id, "\n".join(lines), inline=self.menu())
            return True
        if data == "agent:new_client":
            if not self._profile(platform, user_id) or not self._profile(platform, user_id)["is_agent"]:
                self.join(platform, user_id, "self")
            current = self._session(platform, user_id)
            submission_key = secrets.token_urlsafe(16)
            self._set_session(platform, user_id, "client_name", {"submission_key": submission_key})
            transport.send(user_id, "<b>Новый клиент</b>\n\nВведите имя клиента.", preserve=True)
            return True
        if data in {"agent:warn:yes", "agent:warn:no", "agent:call:yes", "agent:call:no"}:
            return self._followup_callback(transport, platform, user_id, data)
        return False

    def _followup_callback(self, transport: Any, platform: str, user_id: str, data: str) -> bool:
        session = self._session(platform, user_id)
        if not session or "lead_id" not in session[1]:
            transport.send(user_id, "Эта кнопка устарела.", inline=self.menu())
            return True
        state, payload = session
        lead_id = int(payload["lead_id"])
        if state == "client_warning" and data.startswith("agent:warn:"):
            answer = "Да" if data.endswith("yes") else "Нет"
            with self.store.db:
                self.store.db.execute("UPDATE referral_leads SET warning_answer=?,updated_at=? WHERE id=?", (answer, int(time.time()), lead_id))
            self.executor.submit(self._sync_followup, lead_id, "warning_answer", "Получится предупредить знакомого")
            self._clear_session(platform, user_id)
            transport.send(user_id, "Спасибо! Клиент передан менеджеру А7 Консалт.", inline=self.menu())
            return True
        if state == "client_call" and data.startswith("agent:call:"):
            answer = "Да" if data.endswith("yes") else "Нет"
            with self.store.db:
                self.store.db.execute("UPDATE referral_leads SET call_phone_answer=?,updated_at=? WHERE id=?", (answer, int(time.time()), lead_id))
            self.executor.submit(self._sync_followup, lead_id, "call_phone_answer", "Получится передать номер звонящего менеджера")
            self._clear_session(platform, user_id)
            final = "Спасибо! Клиент передан менеджеру А7 Консалт."
            if answer == "Да" and self.config.manager_contact_url:
                final += f"\n\nКонтакт менеджера: {escape(self.config.manager_contact_url)}"
            transport.send(user_id, final, inline=self.menu())
            return True
        transport.send(user_id, "Эта кнопка устарела.", inline=self.menu())
        return True

    def handle_text(self, transport: Any, platform: str, user_id: str, text: str) -> bool:
        session = self._session(platform, user_id)
        if not session:
            return False
        state, data = session
        clean = text.strip()
        if state == "client_name":
            if not clean:
                transport.send(user_id, "Имя не должно быть пустым.", preserve=True)
                return True
            data["client_name"] = clean[:255]
            self._set_session(platform, user_id, "client_phone", data)
            transport.send(user_id, "Введите телефон клиента.", preserve=True)
            return True
        if state == "client_phone":
            phone = normalize_phone(clean)
            if not phone:
                transport.send(user_id, "Телефон выглядит некорректно. Введите номер ещё раз.", preserve=True)
                return True
            data["phone"] = phone
            lead_id = self._ensure_collecting_lead(platform, user_id, data)
            if lead_id is None:
                self._clear_session(platform, user_id)
                transport.send(user_id, "Этот клиент уже зарегистрирован в партнёрской программе. Повторная заявка не создана.", inline=self.menu())
                return True
            data["lead_id"] = lead_id
            self.mailings.disable(platform, user_id, "client_transferred")
            self.executor.submit(self._sync_lead, lead_id)
            self.executor.submit(self._notify_new_lead, platform, lead_id, user_id, data["client_name"], data["phone"])
            self._set_session(platform, user_id, "client_relation", data)
            transport.send(user_id, "Кем клиент вам приходится? Может ли он на вас сослаться?", preserve=True)
            return True
        if state == "client_relation":
            lead_id = int(data.get("lead_id") or self._ensure_collecting_lead(platform, user_id, data) or 0)
            if not lead_id:
                self._clear_session(platform, user_id)
                transport.send(user_id, "Этот клиент уже зарегистрирован в партнёрской программе. Повторная заявка не создана.", inline=self.menu())
                return True
            data["lead_id"] = lead_id
            data["relation_to_agent"] = clean[:255] or "не указано"
            with self.store.db:
                self.store.db.execute("UPDATE referral_leads SET relation_to_agent=?,updated_at=? WHERE id=?", (data["relation_to_agent"], int(time.time()), lead_id))
            self.executor.submit(self._sync_lead, lead_id)
            self.executor.submit(self._sync_followup, lead_id, "relation_to_agent", "Связь клиента с агентом")
            self._set_session(platform, user_id, "client_permission", data)
            transport.send(user_id, "Можно ли сообщить, что номер получили от вас?", preserve=True)
            return True
        if state == "client_permission":
            lead_id = int(data.get("lead_id") or self._ensure_collecting_lead(platform, user_id, data) or 0)
            if not lead_id:
                self._clear_session(platform, user_id)
                transport.send(user_id, "Этот клиент уже зарегистрирован в партнёрской программе. Повторная заявка не создана.", inline=self.menu())
                return True
            data["lead_id"] = lead_id
            data["source_permission"] = clean[:255] or "не указано"
            with self.store.db:
                self.store.db.execute("UPDATE referral_leads SET source_permission=?,updated_at=? WHERE id=?", (data["source_permission"], int(time.time()), lead_id))
            self.executor.submit(self._sync_lead, lead_id)
            self.executor.submit(self._sync_followup, lead_id, "source_permission", "Можно сообщить источник контакта")
            self._set_session(platform, user_id, "client_payout", data)
            transport.send(user_id, "По какому номеру с вами связываться для выплаты бонуса?", preserve=True)
            return True
        if state == "client_payout":
            payout = normalize_phone(clean)
            if not payout:
                transport.send(user_id, "Телефон выглядит некорректно. Введите номер ещё раз.", preserve=True)
                return True
            now = int(time.time())
            lead_id = int(data.get("lead_id") or 0)
            if not lead_id:
                lead_id = self._ensure_collecting_lead(platform, user_id, data) or 0
            if not lead_id:
                self._clear_session(platform, user_id)
                transport.send(user_id, "Этот клиент уже зарегистрирован в партнёрской программе. Повторная заявка не создана.", inline=self.menu())
                return True
            with self.store.db:
                self.store.db.execute(
                    "UPDATE referral_leads SET relation_to_agent=?,source_permission=?,agent_payout_phone=?,status='submitted',submitted_at=COALESCE(submitted_at,?),updated_at=? WHERE id=?",
                    (data.get("relation_to_agent"), data.get("source_permission"), payout, now, now, lead_id),
                )
                self.store.db.execute("UPDATE agent_profiles SET phone=?,updated_at=? WHERE platform=? AND user_id=?", (payout, now, platform, user_id))
            self.executor.submit(self._sync_lead, lead_id)
            self.executor.submit(self._sync_followup, lead_id, "agent_payout_phone", "Телефон агента для выплаты")
            data["lead_id"] = lead_id
            self._set_session(platform, user_id, "client_warning", data)
            transport.send(user_id, "Получится предупредить знакомого, что ему позвонит менеджер А7 Консалт?", inline=[[button("Да", "agent:warn:yes"), button("Нет", "agent:warn:no")]], preserve=True)
            return True
        return False

    def start_payload(self, platform: str, user_id: str, text: str) -> bool:
        payload = text.partition(" ")[2].strip()
        match = re.fullmatch(r"ref_(telegram|max)_(.+)", payload)
        return self.attach_referrer(platform, user_id, match.group(1), match.group(2)) if match else False

    def admin_command(self, transport: Any, platform: str, user_id: str, text: str) -> str | None:
        if text.startswith("/bonus_paid "):
            bonus_id = int(text.split()[1])
            row = self.store._one("SELECT * FROM bonuses WHERE id=?", (bonus_id,))
            if not row:
                return "Бонус не найден."
            with self.store.db:
                self.store.db.execute("UPDATE bonuses SET status='paid',paid_at=? WHERE id=? AND status<>'paid'", (int(time.time()), bonus_id))
            target = self.transports.get(str(row["agent_platform"]))
            if target:
                self.executor.submit(self._safe_send, target, str(row["agent_user_id"]), f"Бонус {row['amount']} ₽ отмечен как выплаченный.", self.menu())
            return "Бонус отмечен как выплаченный."
        if text.startswith("/bonus "):
            parts = text.split(" ", 5)
            if len(parts) < 5:
                return "Использование: /bonus PLATFORM USER_ID AMOUNT LEAD_ID [комментарий]"
            _, target_platform, target_user, amount, lead_id, *comment = parts
            now = int(time.time())
            with self.store.db:
                bonus_id = self.store.db.execute(
                    "INSERT INTO bonuses(agent_platform,agent_user_id,lead_id,amount,status,comment,created_by,created_at) VALUES(?,?,?,?, 'pending',?,?,?)",
                    (target_platform, target_user, None if lead_id == "-" else int(lead_id), int(amount), comment[0] if comment else None, f"{platform}/{user_id}", now),
                ).lastrowid
            target = self.transports.get(target_platform)
            if target:
                self.executor.submit(self._safe_send, target, target_user, f"Вам начислен бонус {int(amount)} ₽. Статус: ожидает выплаты.", self.menu())
            return f"Бонус #{bonus_id} начислен."
        return None
