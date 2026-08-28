from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_path=os.getenv("DATABASE_PATH", "lawyer_tester.sqlite3"),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            max_token=os.getenv("MAX_BOT_TOKEN", "").strip(),
            max_api_base_url=os.getenv("MAX_API_BASE_URL", "https://platform-api2.max.ru").rstrip("/"),
            admin_ids=frozenset(x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()),
            amo_base_url=os.getenv("AMOCRM_BASE_URL", "").rstrip("/"),
            amo_token=os.getenv("AMOCRM_ACCESS_TOKEN", "").strip(),
            target_pipeline=os.getenv("AMOCRM_TARGET_PIPELINE_NAME", "Судебный приказ"),
            target_status=os.getenv("AMOCRM_TARGET_STATUS_NAME", "Готов к сотрудничеству"),
            inactivity_seconds=int(os.getenv("INACTIVITY_MINUTES", "30")) * 60,
            poll_timeout=int(os.getenv("POLL_TIMEOUT_SECONDS", "25")),
        )
