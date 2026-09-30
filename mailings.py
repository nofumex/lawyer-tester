from __future__ import annotations

import calendar
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from seed import SPECIAL_ANSWER

LOG = logging.getLogger(__name__)
DAY = 86400
LEASE_SECONDS = 300

GROUP_MESSAGES: dict[int, tuple[str, ...]] = {
    1: (
        "Здравствуйте! Вы ранее проходили тестирование на должность юриста в нашей компании. Мы решили остановиться на другом кандидате, однако вы указали, что готовы сотрудничать с нами и передавать клиентов на банкротство за вознаграждение в 10-15 тысяч рублей за клиента.\n\nПредлагаем начать сотрудничество! Если вам интересно, нажмите кнопку ниже",
        "Напоминаем о возможности дополнительного заработка! Вы можете передавать нам клиентов на банкротство и получать до 15 000 рублей за каждого привлечённого клиента, заключившего договор. Хотите попробовать?",
        "Часто встречаетесь с людьми, которым сложно справляться с кредитами или микрозаймами? Передайте их нам, а мы возьмём на себя консультации и юридическое сопровождение. За успешную рекомендацию вы получите вознаграждение до 15 000 рублей!",
        "Для участия в нашей партнёрской программе не нужно устраиваться на работу или тратить время на ведение дел. Достаточно рекомендовать нашу компанию людям с долгами. Хотите узнать подробности?",
        "Наша партнёрская программа продолжает работать! Если среди ваших знакомых есть люди, которым нужна помощь с банкротством, вы можете получить вознаграждение за их привлечение",
        "Хотите получать дополнительный доход от рекомендаций? Наша программа сотрудничества по-прежнему открыта для вас",
    ),
    2: (
        "Хотите заработать до 15 000 рублей за передачу нам клиента на банкротство?\n\nМы предлагаем партнёрскую программу: вы рекомендуете нам человека с долгами, а мы занимаемся всей юридической работой. Вознаграждение начисляется после заключения договора с клиентом",
        "Знаете людей, у которых проблемы с кредитами? Вы можете помочь им избавиться от долгов и получить за это вознаграждение. Интересно?",
        "Дополнительный заработок без трудоустройства! Передавайте нам клиентов, которым требуется банкротство, и получайте до 15 000 рублей за успешную рекомендацию. Хотите присоединиться?",
        "Напоминаем, что у нас действует партнёрская программа. Если вам интересно получать вознаграждение за рекомендации, можем рассказать, как начать",
        "Наша партнёрская программа по-прежнему открыта. Есть знакомые с долговой нагрузкой или часто встречаетесь с такими людьми? Вы можете приводить их в нашу компанию и получать вознаграждение",
    ),
}


def _add_months(timestamp: int, months: int) -> int:
    value = datetime.fromtimestamp(timestamp, timezone.utc)
    month_index = value.month - 1 + months
    year, month = value.year + month_index // 12, month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return int(value.replace(year=year, month=month, day=day).timestamp())


def due_for_step(group_no: int, step: int, previous_at: int) -> int:
    if step == 1:
        return previous_at + 7 * DAY if group_no == 1 else _add_months(previous_at, 1)
    intervals = {2: 1 if group_no == 1 else 2, 3: 2 if group_no == 1 else 5,
                 4: 5 if group_no == 1 else 12, 5: 12 if group_no == 1 else 24}
    return _add_months(previous_at, intervals.get(step, 24))


def message_for(group_no: int, step: int) -> str:
    messages = GROUP_MESSAGES[group_no]
    if group_no == 1 and step > len(messages):
        return messages[4 if step % 2 else 5]
    return messages[min(step, len(messages)) - 1]


def mailing_buttons(step: int) -> list[list[dict[str, str]]]:
    rows = [[{"text": "Интересно", "callback_data": "mail:interest"}]]
    if step >= 3:
        rows.append([{"text": "Не присылать напоминания", "callback_data": "mail:disable"}])
    return rows


@dataclass(slots=True)
class MailingService:
    store: Any
    transports: dict[str, Any]
    interval_seconds: int = 30

    def setting(self, key: str, default: str = "") -> str:
        row = self.store._one("SELECT value FROM app_settings WHERE key=?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        now = int(time.time())
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, now),
            )

    def enabled(self) -> bool:
        return self.setting("mailings_enabled", "0") == "1"

    def _classification(self, platform: str, user_id: str, created_at: int) -> tuple[int, int]:
        completed = self.store.db.execute(
            "SELECT id,last_activity_at FROM attempts WHERE user_platform=? AND user_id=? AND status='completed' ORDER BY id DESC LIMIT 1",
            (platform, user_id),
        ).fetchall()
        for attempt in completed:
            answers = self.store.db.execute(
                "SELECT value_json FROM answers WHERE attempt_id=?", (attempt["id"],)
            ).fetchall()
            if any(SPECIAL_ANSWER in str(answer["value_json"]) for answer in answers):
                return 1, int(attempt["last_activity_at"])
        return 2, int(created_at)

    def _schedule(self, platform: str, user_id: str, group_no: int, step: int,
                  due_at: int, status: str = "pending", batch_id: int | None = None) -> None:
        now = int(time.time())
        self.store.db.execute(
            "INSERT INTO mailing_jobs(platform,user_id,group_no,step,due_at,status,backfill_batch_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(platform,user_id,step) DO UPDATE SET "
            "group_no=excluded.group_no,due_at=excluded.due_at,status=CASE WHEN mailing_jobs.status IN ('sent','uncertain') "
            "THEN mailing_jobs.status ELSE excluded.status END,backfill_batch_id=COALESCE(excluded.backfill_batch_id,mailing_jobs.backfill_batch_id)",
            (platform, user_id, group_no, step, due_at, status, batch_id, now),
        )

    def reconcile_user(self, platform: str, user_id: str) -> None:
        user = self.store._one("SELECT * FROM users WHERE platform=? AND user_id=?", (platform, user_id))
        if not user:
            return
        now = int(time.time())
        group_no, anchor_at = self._classification(platform, user_id, int(user["created_at"]))
        state = self.store._one("SELECT * FROM mailing_states WHERE platform=? AND user_id=?", (platform, user_id))
        implemented_at = int(self.setting("mailings_implemented_at", str(now)))
        if state is None:
            due_at = due_for_step(group_no, 1, anchor_at)
            historical = due_at <= implemented_at or due_at <= now
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO mailing_states(platform,user_id,group_no,anchor_at,next_step,overdue,updated_at) VALUES(?,?,?,?,1,?,?)",
                    (platform, user_id, group_no, anchor_at, int(historical), now),
                )
                self._schedule(platform, user_id, group_no, 1, due_at, "overdue" if historical else "pending")
            return
        if int(state["group_no"]) != group_no or int(state["anchor_at"]) != anchor_at:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE mailing_states SET group_no=?,anchor_at=?,updated_at=? WHERE platform=? AND user_id=?",
                    (group_no, anchor_at, now, platform, user_id),
                )
                pending = self.store._one(
                    "SELECT * FROM mailing_jobs WHERE platform=? AND user_id=? AND status IN ('pending','overdue') ORDER BY step LIMIT 1",
                    (platform, user_id),
                )
                if pending:
                    base = int(state["last_sent_at"] or anchor_at)
                    due_at = due_for_step(group_no, int(pending["step"]), base)
                    status = "overdue" if pending["status"] == "overdue" or due_at <= now else "pending"
                    self.store.db.execute(
                        "UPDATE mailing_jobs SET group_no=?,due_at=?,status=? WHERE id=?",
                        (group_no, due_at, status, pending["id"]),
                    )
                    if status == "overdue":
                        self.store.db.execute(
                            "UPDATE mailing_states SET overdue=1 WHERE platform=? AND user_id=?",
                            (platform, user_id),
                        )

    def reconcile_all(self) -> int:
        rows = self.store.db.execute("SELECT platform,user_id FROM users ORDER BY created_at").fetchall()
        for row in rows:
            self.reconcile_user(str(row["platform"]), str(row["user_id"]))
        return len(rows)

    def _is_test_user(self, platform: str, user_id: str) -> bool:
        return self.store._one(
            "SELECT 1 FROM mailing_test_users WHERE platform=? AND user_id=?", (platform, user_id)
        ) is not None

    def quarantine_due(self, now: int | None = None) -> int:
        now = now or int(time.time())
        with self.store.db:
            result = self.store.db.execute(
                "UPDATE mailing_jobs SET status='overdue',error_message='deadline passed while automatic mailings were disabled' "
                "WHERE status='pending' AND due_at<=? AND NOT EXISTS (SELECT 1 FROM mailing_test_users t "
                "WHERE t.platform=mailing_jobs.platform AND t.user_id=mailing_jobs.user_id)", (now,)
            )
            self.store.db.execute(
                "UPDATE mailing_states SET overdue=1,updated_at=? WHERE EXISTS (SELECT 1 FROM mailing_jobs j "
                "WHERE j.platform=mailing_states.platform AND j.user_id=mailing_states.user_id AND j.status='overdue')", (now,)
            )
        return int(result.rowcount or 0)

    def set_enabled(self, enabled: bool) -> int:
        quarantined = self.quarantine_due() if enabled else 0
        self.set_setting("mailings_enabled", "1" if enabled else "0")
        return quarantined

    def interest(self, platform: str, user_id: str) -> bool:
        self.reconcile_user(platform, user_id)
        now = int(time.time())
        with self.store.db:
            state = self.store._one("SELECT * FROM mailing_states WHERE platform=? AND user_id=?", (platform, user_id))
            if not state:
                return False
            changed = state["paused_at"] is None and state["stopped_reason"] is None
            self.store.db.execute(
                "UPDATE mailing_states SET paused_at=COALESCE(paused_at,?),resume_at=COALESCE(resume_at,?),updated_at=? "
                "WHERE platform=? AND user_id=? AND stopped_reason IS NULL",
                (now, now + 30 * DAY, now, platform, user_id),
            )
            self.store.db.execute(
                "UPDATE mailing_jobs SET status='cancelled',cancelled_at=? WHERE platform=? AND user_id=? AND status='pending'",
                (now, platform, user_id),
            )
        return changed

    def disable(self, platform: str, user_id: str, reason: str = "disabled_by_user") -> bool:
        self.reconcile_user(platform, user_id)
        now = int(time.time())
        with self.store.db:
            state = self.store._one("SELECT * FROM mailing_states WHERE platform=? AND user_id=?", (platform, user_id))
            changed = bool(state and not state["reminders_disabled"])
            self.store.db.execute(
                "UPDATE mailing_states SET reminders_disabled=1,stopped_reason=?,paused_at=NULL,resume_at=NULL,updated_at=? "
                "WHERE platform=? AND user_id=?", (reason, now, platform, user_id)
            )
            self.store.db.execute(
                "UPDATE mailing_jobs SET status='cancelled',cancelled_at=? WHERE platform=? AND user_id=? AND status IN ('pending','overdue')",
                (now, platform, user_id),
            )
        return changed

    def resume_paused(self) -> int:
        now = int(time.time())
        rows = self.store.db.execute(
            "SELECT * FROM mailing_states WHERE resume_at IS NOT NULL AND resume_at<=? AND stopped_reason IS NULL", (now,)
        ).fetchall()
        resumed = 0
        for state in rows:
            lead = self.store._one(
                "SELECT 1 FROM referral_leads WHERE agent_platform=? AND agent_user_id=? AND status<>'draft' LIMIT 1",
                (state["platform"], state["user_id"]),
            )
            if lead:
                self.disable(str(state["platform"]), str(state["user_id"]), "client_transferred")
                continue
            step = int(state["next_step"])
            with self.store.db:
                self.store.db.execute(
                    "UPDATE mailing_states SET paused_at=NULL,resume_at=NULL,updated_at=? WHERE platform=? AND user_id=?",
                    (now, state["platform"], state["user_id"]),
                )
                self._schedule(str(state["platform"]), str(state["user_id"]), int(state["group_no"]), step, now)
            resumed += 1
        return resumed

    def _eligible(self, job: Any) -> bool:
        state = self.store._one("SELECT * FROM mailing_states WHERE platform=? AND user_id=?", (job["platform"], job["user_id"]))
        return bool(state and not state["reminders_disabled"] and state["stopped_reason"] is None and state["paused_at"] is None)

    def deliver_job(self, job_id: int) -> bool:
        now = int(time.time())
        with self.store.db:
            job = self.store._one("SELECT * FROM mailing_jobs WHERE id=?", (job_id,))
            if not job or job["status"] not in {"pending", "backfill_pending"} or not self._eligible(job):
                return False
            claimed = self.store.db.execute(
                "UPDATE mailing_jobs SET status='sending',attempts=attempts+1,claimed_at=?,lease_until=? "
                "WHERE id=? AND status IN ('pending','backfill_pending')", (now, now + LEASE_SECONDS, job_id)
            )
            if claimed.rowcount != 1:
                return False
        job = self.store._one("SELECT * FROM mailing_jobs WHERE id=?", (job_id,))
        if not job or not self._eligible(job):
            with self.store.db:
                self.store.db.execute("UPDATE mailing_jobs SET status='cancelled',cancelled_at=?,lease_until=NULL WHERE id=?", (int(time.time()), job_id))
            return False
        transport = self.transports.get(str(job["platform"]))
        if transport is None:
            with self.store.db:
                self.store.db.execute("UPDATE mailing_jobs SET status='pending',lease_until=NULL,error_message='transport unavailable' WHERE id=?", (job_id,))
            return False
        try:
            transport.send(str(job["user_id"]), message_for(int(job["group_no"]), int(job["step"])), inline=mailing_buttons(int(job["step"])))
        except Exception as exc:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE mailing_jobs SET status='uncertain',uncertain_at=?,lease_until=NULL,error_message=? WHERE id=?",
                    (int(time.time()), str(exc)[:1000], job_id),
                )
            LOG.exception("Mailing delivery outcome is uncertain job_id=%s", job_id)
            return False
        sent_at = int(time.time())
        with self.store.db:
            self.store.db.execute("UPDATE mailing_jobs SET status='sent',sent_at=?,lease_until=NULL,error_message=NULL WHERE id=?", (sent_at, job_id))
            self.store.db.execute(
                "UPDATE mailing_states SET last_sent_step=?,last_sent_at=?,next_step=?,overdue=0,updated_at=? WHERE platform=? AND user_id=?",
                (job["step"], sent_at, int(job["step"]) + 1, sent_at, job["platform"], job["user_id"]),
            )
            self._schedule(str(job["platform"]), str(job["user_id"]), int(job["group_no"]), int(job["step"]) + 1,
                           due_for_step(int(job["group_no"]), int(job["step"]) + 1, sent_at))
        return True

    def recover_expired_claims(self) -> int:
        now = int(time.time())
        with self.store.db:
            result = self.store.db.execute(
                "UPDATE mailing_jobs SET status='uncertain',uncertain_at=?,lease_until=NULL,error_message='worker lease expired during delivery' "
                "WHERE status='sending' AND (lease_until IS NULL OR lease_until<?)", (now, now)
            )
        return int(result.rowcount or 0)

    def resolve_uncertain(self, job_id: int, delivered: bool) -> str:
        job = self.store._one("SELECT * FROM mailing_jobs WHERE id=? AND status='uncertain'", (job_id,))
        if not job:
            return "Неопределённая отправка не найдена."
        now = int(time.time())
        if not delivered:
            with self.store.db:
                self.store.db.execute(
                    "UPDATE mailing_jobs SET status='backfill_pending',due_at=?,uncertain_at=NULL,error_message=NULL WHERE id=?",
                    (now, job_id),
                )
            return "Результат отмечен как «не доставлено»; разрешена одна явная повторная попытка."
        with self.store.db:
            self.store.db.execute(
                "UPDATE mailing_jobs SET status='sent',sent_at=?,lease_until=NULL,uncertain_at=NULL,error_message=NULL WHERE id=?",
                (int(job["claimed_at"] or now), job_id),
            )
            self.store.db.execute(
                "UPDATE mailing_states SET last_sent_step=?,last_sent_at=?,next_step=?,overdue=0,updated_at=? WHERE platform=? AND user_id=?",
                (job["step"], int(job["claimed_at"] or now), int(job["step"]) + 1, now, job["platform"], job["user_id"]),
            )
            self._schedule(str(job["platform"]), str(job["user_id"]), int(job["group_no"]), int(job["step"]) + 1,
                           due_for_step(int(job["group_no"]), int(job["step"]) + 1, int(job["claimed_at"] or now)))
        return "Отправка отмечена как доставленная; следующий срок рассчитан от времени попытки."

    def due_ids(self) -> list[int]:
        now = int(time.time())
        if self.enabled():
            rows = self.store.db.execute(
                "SELECT id FROM mailing_jobs WHERE status IN ('pending','backfill_pending') AND due_at<=? ORDER BY due_at,id LIMIT 100", (now,)
            ).fetchall()
        else:
            rows = self.store.db.execute(
                "SELECT j.id FROM mailing_jobs j WHERE j.status IN ('pending','backfill_pending') AND j.due_at<=? AND "
                "(j.status='backfill_pending' OR EXISTS (SELECT 1 FROM mailing_test_users t WHERE t.platform=j.platform AND t.user_id=j.user_id)) "
                "ORDER BY j.due_at,j.id LIMIT 100", (now,)
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def run(self, stop_event: threading.Event) -> None:
        self.reconcile_all()
        while not stop_event.is_set():
            try:
                self.reconcile_all()
                self.recover_expired_claims()
                self.resume_paused()
                if not self.enabled():
                    self.quarantine_due()
                for job_id in self.due_ids():
                    self.deliver_job(job_id)
            except Exception:
                LOG.exception("Automatic mailing worker failed")
            stop_event.wait(self.interval_seconds)

    def preview_backfill(self, platform: str, user_id: str, group_no: int | None, limit: int) -> str:
        where = "status='overdue'" + (" AND group_no=?" if group_no else "")
        args: tuple[Any, ...] = (group_no,) if group_no else ()
        count = int(self.store._one(f"SELECT count(*) n FROM mailing_jobs WHERE {where}", args)["n"])
        token = secrets.token_urlsafe(9)
        now = int(time.time())
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO mailing_backfills(token,group_no,recipient_limit,candidate_count,status,created_by_platform,created_by_user_id,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (token, group_no, max(1, limit), count, "previewed", platform, user_id, now),
            )
        return f"Dry-run backfill: найдено {count}, лимит {max(1, limit)}. Ничего не отправлено. Для подтверждения: /mailings backfill confirm {token}"

    def confirm_backfill(self, token: str) -> str:
        batch = self.store._one("SELECT * FROM mailing_backfills WHERE token=? AND status='previewed'", (token,))
        if not batch:
            return "Предпросмотр не найден или уже использован."
        where = "status='overdue'" + (" AND group_no=?" if batch["group_no"] else "")
        args: tuple[Any, ...] = (batch["group_no"],) if batch["group_no"] else ()
        rows = self.store.db.execute(
            f"SELECT id FROM mailing_jobs WHERE {where} ORDER BY due_at,id LIMIT ?", args + (int(batch["recipient_limit"]),)
        ).fetchall()
        now = int(time.time())
        with self.store.db:
            for row in rows:
                self.store.db.execute("UPDATE mailing_jobs SET status='backfill_pending',due_at=?,backfill_batch_id=? WHERE id=?", (now, batch["id"], row["id"]))
            self.store.db.execute("UPDATE mailing_backfills SET status='confirmed',confirmed_at=? WHERE id=?", (now, batch["id"]))
        return f"Backfill подтверждён: в безопасную очередь добавлено {len(rows)}."

    def status_text(self) -> str:
        counts = {row["status"]: row["n"] for row in self.store.db.execute("SELECT status,count(*) n FROM mailing_jobs GROUP BY status")}
        return f"Авторассылки: {'ВКЛ' if self.enabled() else 'ВЫКЛ'}\nОчередь: {counts}"

    def admin_command(self, platform: str, user_id: str, text: str) -> str:
        parts = text.split()
        if len(parts) == 1 or parts[1] == "status":
            return self.status_text()
        if parts[1] == "enable":
            if len(parts) < 3 or parts[2] != "CONFIRM":
                return "Для включения: /mailings enable CONFIRM"
            return f"Авторассылки включены. Просрочено и изолировано: {self.set_enabled(True)}."
        if parts[1] == "disable":
            self.set_enabled(False)
            return "Авторассылки выключены. Наступающие сроки будут переводиться в просроченные."
        if parts[1] == "resolve" and len(parts) >= 4:
            if parts[3] not in {"delivered", "not-delivered"}:
                return "Использование: /mailings resolve JOB_ID delivered|not-delivered"
            try:
                job_id = int(parts[2])
            except ValueError:
                return "Использование: /mailings resolve JOB_ID delivered|not-delivered"
            return self.resolve_uncertain(job_id, parts[3] == "delivered")
        if parts[1] == "backfill" and len(parts) >= 4 and parts[2] == "confirm":
            return self.confirm_backfill(parts[3])
        if parts[1] == "backfill":
            try:
                group = None if len(parts) < 3 or parts[2] == "all" else int(parts[2].removeprefix("group"))
                limit = int(parts[3]) if len(parts) >= 4 else 100
            except ValueError:
                return "Использование: /mailings backfill all|group1|group2 LIMIT"
            if group not in {None, 1, 2} or limit < 1:
                return "Использование: /mailings backfill all|group1|group2 LIMIT"
            return self.preview_backfill(platform, user_id, group, limit)
        if parts[1] == "test" and len(parts) >= 3:
            action = parts[2]
            target_platform = parts[3] if len(parts) >= 5 else platform
            target_user = parts[4] if len(parts) >= 5 else user_id
            if action in {"add", "remove"}:
                with self.store.db:
                    if action == "add":
                        self.store.db.execute("INSERT OR IGNORE INTO mailing_test_users VALUES(?,?,?)", (target_platform, target_user, int(time.time())))
                    else:
                        self.store.db.execute("DELETE FROM mailing_test_users WHERE platform=? AND user_id=?", (target_platform, target_user))
                return f"Тестовый пользователь {action}: {target_platform}/{target_user}."
            if action == "send" and len(parts) >= 5:
                try:
                    group_no, step = int(parts[3]), int(parts[4])
                except ValueError:
                    return "Использование: /mailings test send GROUP STEP"
                if group_no not in {1, 2} or step < 1:
                    return "Использование: /mailings test send GROUP STEP"
                send_platform = parts[5] if len(parts) >= 7 else platform
                send_user = parts[6] if len(parts) >= 7 else user_id
                self.reconcile_user(send_platform, send_user)
                if not self.store._one("SELECT 1 FROM mailing_states WHERE platform=? AND user_id=?", (send_platform, send_user)):
                    return "Тестовый пользователь не зарегистрирован в боте. Сначала он должен открыть бот."
                with self.store.db:
                    self.store.db.execute("INSERT OR IGNORE INTO mailing_test_users VALUES(?,?,?)", (send_platform, send_user, int(time.time())))
                    self._schedule(send_platform, send_user, group_no, step, int(time.time()))
                    self.store.db.execute(
                        "UPDATE mailing_jobs SET group_no=?,due_at=?,status='pending',attempts=0,claimed_at=NULL,lease_until=NULL,sent_at=NULL,cancelled_at=NULL,uncertain_at=NULL,error_message=NULL "
                        "WHERE platform=? AND user_id=? AND step=?",
                        (group_no, int(time.time()), send_platform, send_user, step),
                    )
                    self.store.db.execute(
                        "UPDATE mailing_states SET group_no=?,next_step=?,paused_at=NULL,resume_at=NULL,reminders_disabled=0,stopped_reason=NULL,overdue=0,updated_at=? "
                        "WHERE platform=? AND user_id=?",
                        (group_no, step, int(time.time()), send_platform, send_user),
                    )
                return f"Тестовое сообщение группы {group_no}, шаг {step} поставлено в очередь только для {send_platform}/{send_user}."
            if action == "advance" and len(parts) >= 4:
                raw = parts[3].casefold()
                try:
                    seconds = int(raw[:-1]) * DAY if raw.endswith("d") else int(raw)
                except ValueError:
                    return "Использование: /mailings test advance 30d"
                advance_platform = parts[4] if len(parts) >= 6 else platform
                advance_user = parts[5] if len(parts) >= 6 else user_id
                with self.store.db:
                    self.store.db.execute("UPDATE mailing_jobs SET due_at=due_at-? WHERE platform=? AND user_id=? AND status='pending'", (seconds, advance_platform, advance_user))
                    self.store.db.execute("UPDATE mailing_states SET resume_at=resume_at-? WHERE platform=? AND user_id=? AND resume_at IS NOT NULL", (seconds, advance_platform, advance_user))
                return f"Тестовые часы сдвинуты на {seconds} секунд."
        return "Команды: /mailings status|enable CONFIRM|disable|resolve JOB_ID delivered|not-delivered|backfill all|group1|group2 LIMIT|backfill confirm TOKEN|test add|remove|send GROUP STEP|advance 30d"
