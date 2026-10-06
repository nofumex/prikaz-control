from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    telegram_token: str
    report_chat_id: int
    source_database_url: str
    amocrm_base_url: str
    amocrm_access_token: str
    pipeline_name: str
    monitoring_source: str
    timezone: str
    report_time: str
    report_database_path: Path
    page_size: int = 5


def load_config() -> Config:
    load_dotenv()
    required = {
        key: os.getenv(key, "").strip()
        for key in ("REPORT_TG_BOT_TOKEN", "REPORT_CHAT_ID", "SOURCE_DATABASE_URL", "AMOCRM_BASE_URL", "AMOCRM_ACCESS_TOKEN")
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError("Missing required environment variables: " + ", ".join(missing))
    timezone = os.getenv("TIMEZONE", "Asia/Krasnoyarsk").strip()
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown IANA timezone: {timezone}") from exc
    report_time = os.getenv("DAILY_REPORT_TIME", "09:00").strip()
    try:
        hour, minute = (int(value) for value in report_time.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("DAILY_REPORT_TIME must be HH:MM") from exc
    return Config(
        telegram_token=required["REPORT_TG_BOT_TOKEN"],
        report_chat_id=int(required["REPORT_CHAT_ID"]),
        source_database_url=required["SOURCE_DATABASE_URL"],
        amocrm_base_url=required["AMOCRM_BASE_URL"].rstrip("/"),
        amocrm_access_token=required["AMOCRM_ACCESS_TOKEN"],
        pipeline_name=os.getenv("AMOCRM_PIPELINE_NAME", "Судебный приказ").strip(),
        monitoring_source=os.getenv("AMOCRM_MONITORING_SOURCE", "Клиенты по судебному приказу").strip(),
        timezone=timezone,
        report_time=report_time,
        report_database_path=Path(os.getenv("REPORT_DATABASE_PATH", "data/reporting.sqlite3")),
        page_size=max(1, min(20, int(os.getenv("REPORT_PAGE_SIZE", "5")))),
    )
