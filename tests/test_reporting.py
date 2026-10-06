import datetime as dt
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.amocrm import AmoCRM, AmoCRMResponseError, deal_detail_text
from app.bot import ReportBot, paginate
from app.config import Config
from app.metrics import calculate_metrics, local_day_bounds, retain_verified_deals
from app.storage import Store


def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE users(id INTEGER PRIMARY KEY, platform TEXT, platform_user_id TEXT, phone TEXT, created_at TEXT);
      CREATE TABLE cases(id INTEGER PRIMARY KEY, user_id INTEGER, platform TEXT, amocrm_lead_id INTEGER, amo_lead_id INTEGER);
      CREATE TABLE crm_sync_logs(id INTEGER PRIMARY KEY, user_id INTEGER, case_id INTEGER, event_type TEXT, success INTEGER, amo_entity_type TEXT, amo_entity_id INTEGER, created_at TEXT);
      CREATE TABLE payments(id INTEGER PRIMARY KEY, case_id INTEGER, status TEXT, paid_at TEXT, created_at TEXT);
      CREATE TABLE mailing_actions(id INTEGER PRIMARY KEY, user_id INTEGER, case_id INTEGER, event_type TEXT, payload_json TEXT, created_at TEXT);
    """)
    return conn


def test_three_metrics_are_date_based_platform_split_and_deduped():
    conn = db()
    tz = ZoneInfo("Asia/Krasnoyarsk")
    day = dt.date(2026, 10, 5)
    start, _ = local_day_bounds(day, tz)
    conn.executemany("INSERT INTO users VALUES(?,?,?,?,?)", [
        (1, "telegram", "tg-1", "+79990000001", "2025-01-01 00:00:00"),
        (2, "max", "max-2", None, "2025-01-01 00:00:00"),
        (3, "telegram", "tg-3", "+79990000003", "2025-01-01 00:00:00"),
    ])
    conn.executemany("INSERT INTO cases VALUES(?,?,?,?,?)", [
        (11, 1, "telegram", 101, 101), (12, 1, "telegram", 102, 102),
        (21, 2, "max", 201, 201), (31, 3, "telegram", 301, 301),
    ])
    # Duplicate stage syncs for the same person and a failed sync do not inflate subscription counts.
    conn.executemany("INSERT INTO crm_sync_logs VALUES(?,?,?,?,?,?,?,?)", [
        (1, 1, 11, "user_started_bot", 1, "lead", 101, start),
        (2, 1, 12, "user_started_bot", 1, "lead", 102, start),
        (3, 2, 21, "user_started_bot", 1, "lead", 201, start),
        (4, 3, 31, "user_started_bot", 0, "lead", 301, start),
        (5, 1, 11, "phone_provided", 1, "lead", 101, start),
        (6, 1, 12, "phone_provided", 1, "lead", 102, start),
        (7, 2, 21, "phone_provided", 1, "lead", 201, start),
        (8, 3, 31, "phone_provided", 1, "lead", 301, "2026-10-03 00:00:00"),
    ])
    conn.executemany("INSERT INTO payments VALUES(?,?,?,?,?)", [
        (1, 11, "pending", None, start),  # created today, not paid
        (2, 12, "paid", start, "2026-10-04 00:00:00"),
        (3, 21, "paid", start, "2026-10-04 00:00:00"),
        (4, 31, "refunded", start, "2026-10-04 00:00:00"),
        (5, 12, "paid", start, "2026-10-04 00:00:00"),  # duplicate payment record, same person/deal
        (6, 11, "paid", "2026-10-04 16:59:59", "2026-10-04 00:00:00"),  # before local report day
    ])
    result = calculate_metrics(conn, day, tz, {101, 102, 201, 301})
    assert result["subscribed"] == {"telegram": 1, "max": 1}
    assert result["contact_count"] == 2
    assert result["paid_count"] == 2
    assert result["deals"]["subscribed_telegram"] == [101, 102]
    assert result["deals"]["paid"] == [102, 201]


def test_contact_uses_campaign_event_timestamp_not_old_phone_presence():
    conn = db()
    tz = ZoneInfo("Asia/Krasnoyarsk")
    day = dt.date(2026, 10, 5)
    start, _ = local_day_bounds(day, tz)
    conn.execute("INSERT INTO users VALUES(1,'max','x','+79990000000','2024-01-01 00:00:00')")
    conn.execute("INSERT INTO cases VALUES(5,1,'max',55,55)")
    conn.execute("INSERT INTO mailing_actions VALUES(1,1,5,'mailing_phone_received',?,?)", ('{"mailing_event_at":"' + start + '"}', start))
    result = calculate_metrics(conn, day, tz, {55})
    assert result["contact_count"] == 1
    assert result["deals"]["contact"] == [55]


def test_pagination_clamps_page_and_returns_expected_total():
    part, page, pages = paginate(list(range(12)), 99, 5)
    assert (part, page, pages) == ([10, 11], 2, 3)
    assert paginate([], 0, 5) == ([], 0, 1)


def test_pagination_keeps_compact_telegram_callbacks():
    cfg = Config("token", 77, "sqlite:///unused", "https://amo.test", "x", "Судебный приказ", "Клиенты по судебному приказу", "Asia/Krasnoyarsk", "09:00", Path("reports.sqlite3"), page_size=5)
    bot = ReportBot(cfg, object(), object())
    report = {"date": "2026-10-05", "deals": {"paid": list(range(1, 18))}, "deal_names": {}}
    _, markup = bot.deal_list_screen(report, "p", 1)
    callbacks = [button["callback_data"] for row in markup["inline_keyboard"] for button in row if "callback_data" in button]
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
    assert [button["text"] for button in markup["inline_keyboard"][-2]] == ["←", "2/4", "→"]


def test_deal_that_moved_to_sales_stays_in_judicial_source_metrics():
    report = {
        "subscribed": {"telegram": 1, "max": 0}, "contact_count": 1, "paid_count": 1,
        "deals": {"subscribed_telegram": [101], "subscribed_max": [], "contact": [101], "paid": [101]},
        "_people": {
            "started": [{"platform": "telegram", "person_id": 1, "lead_ids": [101]}],
            "contact": [{"platform": "telegram", "person_id": 1, "lead_ids": [101]}],
            "paid": [{"platform": "telegram", "person_id": 1, "lead_ids": [101]}],
        },
    }
    filtered = retain_verified_deals(report, {101})
    assert filtered["paid_count"] == 1
    assert filtered["deals"]["paid"] == [101]


def test_deal_card_matches_crm_controller_detail_fields_and_escapes_html():
    detail = {
        "id": 32430616, "name": "Судебный приказ — max ID 274286535 <клиент>", "price": 0,
        "responsible": "Павел", "current_stage": "Отдел продаж → Не смог реализовать",
        "initial_stage": "Судебный приказ → Подписался на бота", "failure_reason": "не выгодно & поздно",
        "created_at": 1785803940, "updated_at": 1788183897,
        "sources": ["Клиенты по судебному приказу"],
        "timeline": [
            {"label": "Судебный приказ → Подписался на бота", "at": 1785803947},
            {"label": "Отдел продаж → Не смог реализовать", "at": 1788183897},
        ],
    }
    text = deal_detail_text(detail, ZoneInfo("Asia/Krasnoyarsk"))
    assert "(<code>32430616</code>)" in text
    assert "Ответственный: <b>Павел</b>" in text
    assert "Причина «Не смог реализовать»: <b>не выгодно &amp; поздно</b>" in text
    assert "Источник мониторинга: <b>Клиенты по судебному приказу</b>" in text
    assert "Путь сделки:" in text and "Подписался на бота" in text
    assert "<клиент>" not in text


def test_scheduler_records_delivery_and_does_not_repeat_after_restart(tmp_path: Path):
    cfg = Config("token", 77, "sqlite:///unused", "https://amo.test", "x", "Судебный приказ", "Клиенты по судебному приказу", "Asia/Krasnoyarsk", "09:00", tmp_path / "reports.sqlite3")
    store = Store(cfg.report_database_path)
    report = {"date": "2026-10-05", "timezone": cfg.timezone, "subscribed": {"telegram": 0, "max": 0}, "paid_count": 0, "contact_count": 0, "deals": {"paid": [], "contact": [], "subscribed_telegram": [], "subscribed_max": []}, "deal_names": {}}
    store.save_report(report)

    class FakeAmo:
        pass

    bot = ReportBot(cfg, store, FakeAmo())
    bot.build_report = lambda day: report
    sent = []
    bot.send = lambda chat_id, text, markup=None: sent.append((chat_id, text))
    before_send = dt.datetime(2026, 10, 6, 8, 59, tzinfo=ZoneInfo(cfg.timezone))
    scheduled = dt.datetime(2026, 10, 6, 9, 0, tzinfo=ZoneInfo(cfg.timezone))
    assert bot.scheduler_tick(before_send) is False
    assert bot.scheduler_tick(scheduled) is True
    assert bot.scheduler_tick(scheduled + dt.timedelta(minutes=1)) is False
    assert bot.scheduled_send(dt.date(2026, 10, 5)) is False
    assert len(sent) == 1 and store.was_sent("2026-10-05", 77)
    store.conn.close()
    restarted_store = Store(cfg.report_database_path)
    restarted = ReportBot(cfg, restarted_store, FakeAmo())
    restarted.send = lambda chat_id, text, markup=None: sent.append((chat_id, text))
    assert restarted.scheduled_send(dt.date(2026, 10, 5)) is False
    assert len(sent) == 1
    restarted_store.conn.close()


def test_failed_telegram_send_releases_claim_and_next_attempt_succeeds(tmp_path: Path):
    cfg = Config("token", 77, "sqlite:///unused", "https://amo.test", "x", "Судебный приказ", "Клиенты по судебному приказу", "Asia/Krasnoyarsk", "09:00", tmp_path / "reports.sqlite3")
    store = Store(cfg.report_database_path)
    report = {"date": "2026-10-05", "timezone": cfg.timezone, "subscribed": {"telegram": 0, "max": 0}, "paid_count": 0, "contact_count": 0, "deals": {"paid": [], "contact": [], "subscribed_telegram": [], "subscribed_max": []}, "deal_names": {}}
    store.save_report(report)

    class FakeAmo:
        pass

    bot = ReportBot(cfg, store, FakeAmo())
    bot.build_report = lambda day: report
    attempts = []

    def fail_then_send(chat_id, text, markup=None):
        attempts.append(text)
        if len(attempts) == 1:
            raise RuntimeError("Telegram unavailable")

    bot.send = fail_then_send
    day = dt.date(2026, 10, 5)
    with pytest.raises(RuntimeError, match="Telegram unavailable"):
        bot.scheduled_send(day)
    assert store.was_sent(day.isoformat(), 77) is False
    # The next scheduler run performs its own claim/send sequence.
    assert bot.scheduled_send(day) is True
    assert len(attempts) == 2
    assert store.was_sent(day.isoformat(), 77) is True
    assert bot.scheduled_send(day) is False
    store.conn.close()


def test_scheduler_refreshes_report_created_by_manual_request(tmp_path: Path):
    cfg = Config("token", 77, "sqlite:///unused", "https://amo.test", "x", "Судебный приказ", "Клиенты по судебному приказу", "Asia/Krasnoyarsk", "09:00", tmp_path / "reports.sqlite3")
    store = Store(cfg.report_database_path)
    old = {"date": "2026-10-05", "timezone": cfg.timezone, "subscribed": {"telegram": 0, "max": 0}, "paid_count": 1, "contact_count": 0, "deals": {"paid": [], "contact": [], "subscribed_telegram": [], "subscribed_max": []}, "deal_names": {}}

    class FakeAmo:
        pass

    bot = ReportBot(cfg, store, FakeAmo())
    manual_sends = []

    def manual_build(day):
        store.save_report(old)
        return old

    bot.build_report = manual_build
    bot.send = lambda chat_id, text, markup=None: manual_sends.append(text)
    day = dt.date(2026, 10, 5)
    bot.show_report(cfg.report_chat_id, day)  # same path as /report when no snapshot exists
    assert "Оплатили — <b>1</b>" in manual_sends[0]

    fresh = {**old, "paid_count": 4, "built_at": 999}
    builds = []

    def build_report(day):
        builds.append(day)
        store.save_report(fresh)
        return fresh

    bot.build_report = build_report
    sent = []
    bot.send = lambda chat_id, text, markup=None: sent.append(text)
    assert store.report("2026-10-05")["paid_count"] == 1  # manual snapshot already exists
    assert bot.scheduled_send(day) is True
    assert builds == [day]
    assert "Оплатили — <b>4</b>" in sent[0]
    assert store.report("2026-10-05")["paid_count"] == 4
    store.conn.close()


def test_every_callback_is_answered():
    cfg = Config("token", 77, "sqlite:///unused", "https://amo.test", "x", "Судебный приказ", "Клиенты по судебному приказу", "Asia/Krasnoyarsk", "09:00", Path("reports.sqlite3"))
    bot = ReportBot(cfg, object(), object())
    calls = []
    bot.tg = lambda method, payload, retries=3: calls.append((method, payload))
    bot.handle_update({"callback_query": {"id": "cb-1", "data": "noop", "message": {"chat": {"id": 77}, "message_id": 9}}})
    assert calls == [("answerCallbackQuery", {"callback_query_id": "cb-1"})]


def test_amocrm_non_json_success_response_reports_status_and_does_not_retry():
    client = AmoCRM("https://example.amocrm.ru", "test-token")

    class Response:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        text = "<html><title>Login required</title></html>"

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            raise ValueError("not JSON")

    class Session:
        calls = 0

        def get(self, url, timeout):
            self.calls += 1
            return Response()

    session = Session()
    client.session = session
    with pytest.raises(AmoCRMResponseError, match="HTTP 200, Content-Type=text/html") as error:
        client.get("/api/v4/events")
    assert "Login required" in str(error.value)
    assert session.calls == 1
