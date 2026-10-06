from __future__ import annotations

import datetime as dt
import html
import logging
import os
import re
import sys
import time
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .amocrm import AmoCRM, deal_detail_view
from .config import Config, load_config
from .metrics import calculate_metrics, retain_verified_deals
from .source_db import open_source_database
from .storage import Store

logger = logging.getLogger(__name__)
REPORT_KINDS = {
    "p": ("paid", "Оплатили"),
    "c": ("contact", "Указали контакт"),
    "s": ("subscribed", "Подписались на бота"),
}


def paginate(items: list[Any], page: int, page_size: int) -> tuple[list[Any], int, int]:
    pages = max(1, (len(items) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    return items[page * page_size:(page + 1) * page_size], page, pages


def split_html_lines(text: str, limit: int = 3500) -> list[str]:
    """Split card/report HTML between self-contained lines, below Telegram's limit."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines() or [""]:
        addition = ("\n" if current else "") + line
        if len(current) + len(addition) > limit and current:
            chunks.append(current)
            current = line
        else:
            current += addition
        if len(current) > limit:
            # A single CRM value can be exceptionally long; preserve valid
            # Telegram HTML by truncating that one line as plain escaped text.
            plain = html.unescape(re.sub(r"<[^>]+>", "", current))
            current = html.escape(plain[:limit - 1], quote=False) + "…"
    if current or not chunks:
        chunks.append(current)
    return chunks


class ReportBot:
    def __init__(self, config: Config, store: Store, amo: AmoCRM):
        self.config, self.store, self.amo = config, store, amo
        self.tz = ZoneInfo(config.timezone)
        self.base_url = f"https://api.telegram.org/bot{config.telegram_token}"
        self.session = requests.Session()
        self.session.trust_env = False

    def tg(self, method: str, payload: dict[str, Any], retries: int = 3) -> Any:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                response = self.session.post(f"{self.base_url}/{method}", json=payload, timeout=45)
                if not response.ok:
                    raise RuntimeError(f"Telegram {method} failed {response.status_code}: {response.text[:400]}")
                body = response.json()
                if not body.get("ok"):
                    raise RuntimeError(f"Telegram {method} failed: {body}")
                return body.get("result")
            except (requests.RequestException, RuntimeError) as exc:
                last_error = exc
                if attempt + 1 < retries:
                    time.sleep(attempt + 1)
        if last_error:
            logger.error("Telegram API error method=%s: %s", method, last_error, exc_info=(type(last_error), last_error, last_error.__traceback__))
        raise last_error or RuntimeError(f"Telegram {method} failed")

    def send(self, chat_id: int, text: str, markup: dict[str, Any] | None = None) -> None:
        try:
            chunks = split_html_lines(text)
            for index, chunk in enumerate(chunks):
                payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True}
                if markup is not None and index == len(chunks) - 1:
                    payload["reply_markup"] = markup
                self.tg("sendMessage", payload, retries=1)
        except Exception:
            logger.exception("Telegram message delivery failed chat_id=%s", chat_id)
            raise

    def edit(self, chat_id: int, message_id: int, text: str, markup: dict[str, Any]) -> None:
        chunks = split_html_lines(text)
        payload = {"chat_id": chat_id, "message_id": message_id, "text": chunks[0], "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": markup if len(chunks) == 1 else None}
        try:
            self.tg("editMessageText", payload, retries=1)
            for index, chunk in enumerate(chunks[1:], start=1):
                self.tg("sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": markup if index == len(chunks) - 1 else None}, retries=1)
        except Exception as exc:
            if "message is not modified" not in str(exc).lower():
                logger.exception("Telegram panel update failed chat_id=%s message_id=%s", chat_id, message_id)
                if len(text) <= 3900:
                    self.send(chat_id, text, markup)

    def answer_callback(self, callback_id: str) -> None:
        try:
            self.tg("answerCallbackQuery", {"callback_query_id": callback_id}, retries=1)
        except Exception:
            logger.exception("Telegram answerCallbackQuery failed")

    def build_report(self, day: dt.date) -> dict[str, Any]:
        conn = open_source_database(self.config.source_database_url)
        try:
            pipeline_id, pipeline_names, statuses = self.amo.pipeline_catalog(self.config.pipeline_name)
            report = calculate_metrics(conn, day, self.tz)
            referenced = {lead_id for ids in report["deals"].values() for lead_id in ids}
            details = {}
            expected_stage = f"{self.config.pipeline_name} → Подписался на бота"
            for lead_id in sorted(referenced):
                try:
                    detail = self.amo.lead_detail(lead_id, pipeline_names, statuses, self.tz, self.config.monitoring_source)
                    if any(item.get("label") == expected_stage for item in detail.get("timeline", [])):
                        details[str(lead_id)] = detail
                except Exception:
                    logger.exception("Could not verify linked amoCRM deal lead_id=%s", lead_id)
            report = retain_verified_deals(report, {int(lead_id) for lead_id in details})
            report["deal_details"] = details
            report["deal_names"] = {lead_id: str(detail.get("name") or lead_id) for lead_id, detail in details.items()}
            report["pipeline_id"] = pipeline_id
            report["built_at"] = int(time.time())
            self.store.save_report(report)
            return report
        finally:
            conn.close()

    def get_report(self, day: dt.date, refresh: bool = False) -> dict[str, Any]:
        raw = None if refresh else self.store.report(day.isoformat())
        return raw or self.build_report(day)

    @staticmethod
    def inline(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
        return {"inline_keyboard": rows}

    def main_menu_screen(self, chat_id: int, day: dt.date) -> tuple[str, dict[str, Any]]:
        rows = [
            [{"text": title, "callback_data": f"l:{day}:{kind}:0"}]
            for kind, (_, title) in REPORT_KINDS.items()
        ]
        day_nav = [
            {"text": "←", "callback_data": f"h:{day - dt.timedelta(days=1)}"},
            {"text": day.strftime("%d.%m"), "callback_data": "noop"},
        ]
        latest_day = self.latest_allowed_day(chat_id)
        if day < latest_day:
            day_nav.append({"text": "→", "callback_data": f"h:{day + dt.timedelta(days=1)}"})
        else:
            day_nav.append({"text": "→", "callback_data": "noop"})
        rows.append(day_nav)
        return f"<b>Главное меню</b>\n📊 Судебный приказ · {day.strftime('%d.%m.%Y')}", self.inline(rows)

    def show_main_menu(self, chat_id: int, day: dt.date, message_id: int = 0) -> None:
        if day > self.latest_allowed_day(chat_id):
            self.send(chat_id, "Доступны отчеты только за вчерашний день и ранее.")
            return
        text, markup = self.main_menu_screen(chat_id, day)
        if message_id:
            self.edit(chat_id, message_id, text, markup)
        else:
            self.send(chat_id, text, markup)

    def report_screen(self, report: dict[str, Any], allow_today: bool = False) -> tuple[str, dict[str, Any]]:
        day = dt.date.fromisoformat(report["date"])
        subs = report["subscribed"]
        subscribed_count = int(subs.get("telegram", 0)) + int(subs.get("max", 0))
        lines = [f"📊 <b>Судебный приказ · {day.strftime('%d.%m.%Y')}</b>", "", f"Оплатили — <b>{report['paid_count']}</b>", f"Указали контакт — <b>{report['contact_count']}</b>", f"Подписались на бота — <b>{subscribed_count}</b>"]
        rows = [
            [{"text": f"Оплатили — {report['paid_count']}", "callback_data": f"l:{day}:p:0"}],
            [{"text": f"Указали контакт — {report['contact_count']}", "callback_data": f"l:{day}:c:0"}],
            [{"text": f"Подписались на бота — {subscribed_count}", "callback_data": f"l:{day}:s:0"}],
        ]
        today = dt.datetime.now(self.tz).date()
        latest_day = today if allow_today else today - dt.timedelta(days=1)
        day_nav = []
        day_nav.append({"text": "←", "callback_data": f"r:{day - dt.timedelta(days=1)}"})
        day_nav.append({"text": day.strftime("%d.%m"), "callback_data": "noop"})
        if day < latest_day:
            day_nav.append({"text": "→", "callback_data": f"r:{day + dt.timedelta(days=1)}"})
        else:
            day_nav.append({"text": "→", "callback_data": "noop"})
        rows.append(day_nav)
        return "\n".join(lines), self.inline(rows)

    def deal_list_screen(self, report: dict[str, Any], kind: str, page: int) -> tuple[str, dict[str, Any]]:
        day = report["date"]
        data_key, title = REPORT_KINDS[kind]
        if data_key == "subscribed":
            ids = sorted(set(report["deals"].get("subscribed_telegram", [])) | set(report["deals"].get("subscribed_max", [])))
        else:
            ids = report["deals"].get(data_key, [])
        part, page, pages = paginate(ids, page, self.config.page_size)
        lines = [f"<b>{html.escape(title)} · {dt.date.fromisoformat(day).strftime('%d.%m.%Y')}</b>", f"Сделок: <b>{len(ids)}</b>", f"Страница <b>{page + 1}/{pages}</b>", ""]
        rows: list[list[dict[str, str]]] = []
        for lead_id in part:
            name = str(report.get("deal_names", {}).get(str(lead_id)) or lead_id).strip()
            label = f"{name} · ID {lead_id}"
            if len(label) > 60:
                label = label[:57].rstrip() + "…"
            rows.append([{"text": label, "callback_data": f"d:{lead_id}:{kind}:{page}:{day}"}])
        nav = []
        if page > 0:
            nav.append({"text": "←", "callback_data": f"l:{day}:{kind}:{page - 1}"})
        nav.append({"text": f"{page + 1}/{pages}", "callback_data": "noop"})
        if page + 1 < pages:
            nav.append({"text": "→", "callback_data": f"l:{day}:{kind}:{page + 1}"})
        rows.append(nav)
        rows.append([{"text": "← К отчету", "callback_data": f"r:{day}"}])
        return "\n".join(lines).rstrip(), self.inline(rows)

    def show_report(self, chat_id: int, day: dt.date, message_id: int = 0) -> None:
        try:
            if day > self.latest_allowed_day(chat_id):
                self.send(chat_id, "Доступны отчеты только за вчерашний день и ранее.")
                return
            report = self.get_report(day)
            text, markup = self.report_screen(report, allow_today=chat_id == self.config.report_chat_id)
            if message_id:
                self.edit(chat_id, message_id, text, markup)
            else:
                self.send(chat_id, text, markup)
        except Exception:
            logger.exception("Report generation/display failed date=%s", day)
            self.send(chat_id, "Не удалось сформировать отчет. Ошибка записана в журнал.")

    def scheduled_send(self, day: dt.date) -> bool:
        key = day.isoformat()
        recipients = tuple(
            chat_id for chat_id in (self.config.manager_ids or (self.config.report_chat_id,))
            if (activation_date := self.store.manager_activation_date(chat_id)) is not None
            and activation_date <= key
        )
        if not recipients:
            return False
        if all(self.store.was_sent(key, chat_id) for chat_id in recipients):
            return False
        # Manual requests reuse their snapshot; scheduled delivery must always
        # capture the latest source data immediately before it is sent.
        report = self.get_report(day, refresh=True)
        text, markup = self.report_screen(report)
        delivered = False
        errors = []
        for chat_id in recipients:
            if self.store.was_sent(key, chat_id) or not self.store.claim_delivery(key, chat_id):
                continue
            try:
                self.send(chat_id, text, markup)
            except Exception as exc:
                self.store.release_delivery(key, chat_id)
                logger.exception("Daily report send failed; delivery claim released for retry date=%s chat_id=%s", key, chat_id)
                errors.append(exc)
                continue
            self.store.mark_sent(key, chat_id)
            logger.info("Daily report delivered date=%s chat_id=%s", key, chat_id)
            delivered = True
        if errors:
            raise errors[0]
        return delivered

    def is_manager(self, chat_id: int) -> bool:
        managers = self.config.manager_ids or (() if self.config.report_chat_id is None else (self.config.report_chat_id,))
        return chat_id in managers or chat_id == self.config.report_chat_id

    def latest_allowed_day(self, chat_id: int) -> dt.date:
        today = dt.datetime.now(self.tz).date()
        return today if chat_id == self.config.report_chat_id else today - dt.timedelta(days=1)

    def scheduler_tick(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(self.tz)
        hour, minute = map(int, self.config.report_time.split(":"))
        if (now.hour, now.minute) < (hour, minute):
            return False
        return self.scheduled_send(now.date() - dt.timedelta(days=1))

    def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback:
            self.answer_callback(str(callback.get("id") or ""))
            message = callback.get("message") or {}
            chat_id = int((message.get("chat") or {}).get("id") or 0)
            if not self.is_manager(chat_id):
                return
            message_id = int(message.get("message_id") or 0)
            data = str(callback.get("data") or "")
            try:
                if data == "noop":
                    return
                if data.startswith("h:"):
                    target_day = dt.date.fromisoformat(data[2:])
                    self.show_main_menu(chat_id, target_day, message_id)
                if data.startswith("r:"):
                    target_day = dt.date.fromisoformat(data[2:])
                    if target_day > self.latest_allowed_day(chat_id):
                        raise ValueError("Report date is not available for this manager")
                    self.show_report(chat_id, target_day, message_id)
                elif data.startswith("l:"):
                    _, day_text, kind, page = data.split(":", 3)
                    target_day = dt.date.fromisoformat(day_text)
                    if target_day > self.latest_allowed_day(chat_id):
                        raise ValueError("Report date is not available for this manager")
                    report = self.get_report(target_day)
                    text, markup = self.deal_list_screen(report, kind, int(page))
                    self.edit(chat_id, message_id, text, markup)
                elif data.startswith("d:"):
                    _, lead_text, kind, page_text, day_text = data.split(":", 4)
                    target_day = dt.date.fromisoformat(day_text)
                    if target_day > self.latest_allowed_day(chat_id):
                        raise ValueError("Report date is not available for this manager")
                    report = self.get_report(target_day)
                    data_key = REPORT_KINDS[kind][0]
                    if data_key == "subscribed":
                        allowed = sorted(set(report.get("deals", {}).get("subscribed_telegram", [])) | set(report.get("deals", {}).get("subscribed_max", [])))
                    else:
                        allowed = report.get("deals", {}).get(data_key, [])
                    lead_id = int(lead_text)
                    if lead_id not in allowed:
                        raise ValueError("Deal is not part of this report snapshot")
                    detail = report.get("deal_details", {}).get(str(lead_id))
                    if not detail:
                        raise ValueError("Deal detail is missing from this report snapshot")
                    back = f"l:{day_text}:{kind}:{page_text}"
                    text, rows = deal_detail_view(detail, self.config.amocrm_base_url, back)
                    self.edit(chat_id, message_id, text, self.inline(rows))
            except Exception:
                logger.exception("Callback processing failed data=%s", data[:100])
                self.send(chat_id, "Не удалось открыть этот экран. Обратитесь к администратору.")
            return

        message = update.get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        if not self.is_manager(chat_id):
            return
        text = str(message.get("text") or "").strip()
        command = text.split()[0].split("@", 1)[0].lower() if text else ""
        if command == "/start":
            try:
                today = dt.datetime.now(self.tz).date()
                self.store.activate_manager(chat_id, today.isoformat())
                self.show_main_menu(chat_id, self.latest_allowed_day(chat_id))
            except Exception:
                logger.exception("Manager activation failed chat_id=%s", chat_id)
                self.send(chat_id, "Не удалось открыть меню. Ошибка записана в журнал.")
            return
        if command in {"/help", "/report", "/yesterday"}:
            try:
                day = dt.date.fromisoformat(text.split(maxsplit=1)[1]) if command == "/report" and len(text.split()) > 1 else dt.datetime.now(self.tz).date() - dt.timedelta(days=1)
                self.show_report(chat_id, day)
            except Exception:
                logger.exception("Manual report request failed")
                self.send(chat_id, "Формат даты: /report YYYY-MM-DD")

    def run(self) -> None:
        try:
            self.tg("deleteWebhook", {"drop_pending_updates": False})
            offset = self.store.telegram_offset()
            logger.info("Report bot started timezone=%s report_time=%s", self.config.timezone, self.config.report_time)
            while True:
                try:
                    self.scheduler_tick()
                    response = self.session.get(f"{self.base_url}/getUpdates", params={"timeout": 25, "offset": offset, "allowed_updates": '["message","callback_query"]'}, timeout=35)
                    response.raise_for_status()
                    payload = response.json()
                    if not payload.get("ok"):
                        raise RuntimeError(f"Telegram getUpdates failed: {payload}")
                    for update in payload.get("result") or []:
                        offset = int(update["update_id"]) + 1
                        self.handle_update(update)
                        self.store.set_telegram_offset(offset)
                except KeyboardInterrupt:
                    raise
                except Exception:
                    logger.exception("Telegram polling/scheduler cycle failed")
                    time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Report bot stopped")


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(name)s:%(lineno)d | %(message)s",
        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler("report-bot.log", encoding="utf-8")],
    )
    config = load_config()
    store = Store(config.report_database_path)
    amo = AmoCRM(config.amocrm_base_url, config.amocrm_access_token)
    try:
        ReportBot(config, store, amo).run()
    finally:
        store.conn.close()


if __name__ == "__main__":
    main()
