from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _csv_ids(value: str) -> set[str]:
    return {x.strip() for x in value.split(",") if x.strip()}


def load_dotenv(path: str = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True, slots=True)
class Config:
    database_path: str
    telegram_token: str
    max_token: str
    max_api_base_url: str
    admin_ids: frozenset[str]
    amo_base_url: str
    amo_token: str
    target_pipeline: str
    target_status: str
    inactivity_seconds: int
    poll_timeout: int
    telegram_bot_username: str = ""
    max_bot_link: str = ""
    a7_offer_url: str = ""
    manager_contact_url: str = ""
    default_bonus_per_client: int = 10000
    second_level_bonus: int = 5000
    referral_pipeline: str = "[A7] TG / Max - Боты"
    referral_status: str = ""
    mailing_interval_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Config":
        admin_ids=_csv_ids(os.getenv("ADMIN_IDS", ""))
        admin_ids.update(_csv_ids(os.getenv("MAX_ADMIN_ID", "")))
        return cls(
            database_path=os.getenv("DATABASE_PATH", "lawyer_tester.sqlite3"),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            max_token=os.getenv("MAX_BOT_TOKEN", "").strip(),
            max_api_base_url=os.getenv("MAX_API_BASE_URL", "https://platform-api2.max.ru").rstrip("/"),
            admin_ids=frozenset(admin_ids),
            amo_base_url=os.getenv("AMOCRM_BASE_URL", "").rstrip("/"),
            amo_token=os.getenv("AMOCRM_ACCESS_TOKEN", "").strip(),
            target_pipeline=os.getenv("AMOCRM_TARGET_PIPELINE_NAME", "Судебный приказ"),
            target_status=os.getenv("AMOCRM_TARGET_STATUS_NAME", "Готов к сотрудничеству"),
            inactivity_seconds=int(os.getenv("INACTIVITY_MINUTES", "30")) * 60,
            poll_timeout=int(os.getenv("POLL_TIMEOUT_SECONDS", "25")),
            telegram_bot_username=os.getenv("TELEGRAM_BOT_USERNAME", "").strip().lstrip("@"),
            max_bot_link=os.getenv("MAX_BOT_LINK", "").strip(),
            a7_offer_url=os.getenv("A7_OFFER_URL", "").strip(),
            manager_contact_url=os.getenv("A7_MANAGER_CONTACT_URL", "").strip(),
            default_bonus_per_client=int(os.getenv("DEFAULT_BONUS_PER_CLIENT", "10000")),
            second_level_bonus=int(os.getenv("SECOND_LEVEL_BONUS", "5000")),
            referral_pipeline=os.getenv("AMOCRM_REFERRAL_PIPELINE_NAME", "[A7] TG / Max - Боты").strip(),
            referral_status=os.getenv("AMOCRM_REFERRAL_STATUS_NAME", "").strip(),
            mailing_interval_seconds=max(1,int(os.getenv("MAILING_WORKER_INTERVAL_SECONDS", "30"))),
        )
