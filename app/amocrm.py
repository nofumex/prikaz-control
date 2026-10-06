from __future__ import annotations

import datetime as dt
import html
import logging
import time
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


class AmoCRM:
    """Read-only amoCRM client. This class intentionally exposes GET only."""
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def get(self, path: str, params: list[tuple[str, Any]] | None = None) -> dict[str, Any]:
        url = self.base_url + path
        if params:
            url += "?" + urlencode(params)
        for attempt in range(4):
            try:
                response = self.session.get(url, timeout=45)
                if response.status_code == 429 and attempt < 3:
                    time.sleep(int(response.headers.get("Retry-After", "2")))
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException:
                logger.exception("amoCRM GET failed path=%s", path)
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)
        raise RuntimeError("amoCRM request failed")

    def pipeline_catalog(self, expected_name: str = "Судебный приказ") -> tuple[int, dict[int, str], dict[tuple[int, int], str]]:
        pipelines = self.get("/api/v4/leads/pipelines").get("_embedded", {}).get("pipelines", [])
        names = {int(p["id"]): str(p.get("name") or p["id"]) for p in pipelines}
        statuses: dict[tuple[int, int], str] = {}
        match = next((p for p in pipelines if str(p.get("name", "")).strip().casefold() == expected_name.strip().casefold()), None)
        if not match:
            raise RuntimeError("amoCRM pipeline 'Судебный приказ' was not found")
        pipeline_id = int(match["id"])
        for pipeline in pipelines:
            pid = int(pipeline["id"])
            for item in pipeline.get("_embedded", {}).get("statuses", []):
                statuses[(pid, int(item["id"]))] = str(item.get("name") or item["id"])
        return pipeline_id, names, statuses

    def list_pipeline_leads(self, pipeline_id: int, statuses: dict[tuple[int, int], str]) -> dict[int, dict[str, Any]]:
        found: dict[int, dict[str, Any]] = {}
        for (pid, status_id) in statuses:
            if pid != pipeline_id:
                continue
            page = 1
            while True:
                data = self.get("/api/v4/leads", [
                    ("filter[statuses][0][pipeline_id]", pipeline_id),
                    ("filter[statuses][0][status_id]", status_id),
                    ("limit", 250), ("page", page),
                ])
                batch = data.get("_embedded", {}).get("leads", [])
                found.update({int(lead["id"]): lead for lead in batch})
                if not batch or not data.get("_links", {}).get("next"):
                    break
                page += 1
        return found

    def lead_detail(self, lead_id: int, pipeline_names: dict[int, str], statuses: dict[tuple[int, int], str], timezone: ZoneInfo, source: str) -> dict[str, Any]:
        lead = self.get(f"/api/v4/leads/{lead_id}")
        events: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.get("/api/v4/events", [
                ("filter[type]", "lead_status_changed"), ("filter[entity]", "lead"),
                ("filter[entity_id][]", lead_id), ("limit", 100), ("page", page),
            ])
            batch = data.get("_embedded", {}).get("events", [])
            events.extend(event for event in batch if int(event.get("entity_id") or 0) == lead_id)
            if not batch or not data.get("_links", {}).get("next"):
                break
            page += 1
        events.sort(key=lambda e: (int(e.get("created_at") or 0), str(e.get("id") or "")))

        def status(value: Any) -> dict[str, int] | None:
            if isinstance(value, list):
                value = next((v for v in value if isinstance(v, dict)), None)
            if not isinstance(value, dict):
                return None
            pipeline_id = value.get("pipeline_id")
            status_id = value.get("id") if value.get("lead_status") else value.get("status_id")
            if value.get("lead_status"):
                value = value["lead_status"]
                pipeline_id = value.get("pipeline_id")
                status_id = value.get("id")
            if pipeline_id is None or status_id is None:
                return None
            return {"pipeline_id": int(pipeline_id), "status_id": int(status_id)}

        def label(state: dict[str, int]) -> str:
            pid, sid = state["pipeline_id"], state["status_id"]
            return f"{pipeline_names.get(pid, str(pid))} → {statuses.get((pid, sid), str(sid))}"

        timeline: list[dict[str, Any]] = []
        if events:
            before = status(events[0].get("value_before"))
            if before:
                timeline.append({"at": int(lead.get("created_at") or events[0].get("created_at") or 0), "label": label(before)})
            for event in events:
                after = status(event.get("value_after"))
                if after and (not timeline or timeline[-1]["label"] != label(after)):
                    timeline.append({"at": int(event.get("created_at") or 0), "label": label(after)})
        if not timeline:
            state = {"pipeline_id": int(lead["pipeline_id"]), "status_id": int(lead["status_id"])}
            timeline = [{"at": int(lead.get("created_at") or 0), "label": label(state)}]

        responsible_id = int(lead.get("responsible_user_id") or 0)
        responsible = "Не назначен"
        if responsible_id:
            try:
                responsible = str(self.get(f"/api/v4/users/{responsible_id}").get("name") or responsible_id)
            except Exception:
                logger.exception("amoCRM responsible lookup failed lead_id=%s user_id=%s", lead_id, responsible_id)
                responsible = str(responsible_id)
        current = label({"pipeline_id": int(lead["pipeline_id"]), "status_id": int(lead["status_id"])})
        failure_reason = None
        if current == "Отдел продаж → Не смог реализовать":
            for field in lead.get("custom_fields_values") or []:
                fname = str(field.get("field_name") or "").casefold()
                if "причин" in fname or "не смог реализовать" in fname:
                    failure_reason = ", ".join(str(v.get("value") or "").strip() for v in field.get("values", []) if v.get("value")) or "Не заполнена"
                    break
            failure_reason = failure_reason or "Не заполнена"
        return {
            "id": int(lead["id"]), "name": lead.get("name") or str(lead_id), "price": int(lead.get("price") or 0),
            "responsible": responsible, "created_at": int(lead.get("created_at") or 0),
            "updated_at": int(lead.get("updated_at") or 0), "current_stage": current,
            "initial_stage": timeline[0]["label"], "timeline": timeline, "sources": [source],
            "failure_reason": failure_reason, "timezone": str(timezone),
        }


def format_epoch(timestamp: int, timezone: ZoneInfo, seconds: bool = False) -> str:
    if not timestamp:
        return "неизвестно"
    fmt = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
    return dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).astimezone(timezone).strftime(fmt)


def deal_detail_text(detail: dict[str, Any], timezone: ZoneInfo) -> str:
    price = f"{int(detail.get('price') or 0):,}".replace(",", " ")
    lines = [
        f"📋 <b>{esc(detail.get('name') or detail['id'])} (<code>{int(detail['id'])}</code>)</b>", "",
        f"Текущий этап: <b>{esc(detail.get('current_stage'))}</b>",
        f"Начальный этап: <b>{esc(detail.get('initial_stage'))}</b>",
        f"Ответственный: <b>{esc(detail.get('responsible'))}</b>",
    ]
    if detail.get("current_stage") == "Отдел продаж → Не смог реализовать":
        lines.append(f"Причина «Не смог реализовать»: <b>{esc(detail.get('failure_reason') or 'Не заполнена')}</b>")
    lines.extend([
        f"Бюджет: <b>{price} ₽</b>",
        f"Создана: <b>{format_epoch(int(detail.get('created_at') or 0), timezone)}</b>",
        f"Обновлена: <b>{format_epoch(int(detail.get('updated_at') or 0), timezone)}</b>",
    ])
    if detail.get("sources"):
        lines.append(f"Источник мониторинга: <b>{esc(', '.join(detail['sources']))}</b>")
    lines.extend(["", "<b>Путь сделки:</b>"])
    timeline = detail.get("timeline") or []
    visible = timeline if len(timeline) <= 35 else timeline[:1] + timeline[-34:]
    omitted = len(timeline) - len(visible)
    for index, item in enumerate(visible):
        branch = "└" if len(visible) == 1 or index == len(visible) - 1 else "┌" if index == 0 else "├"
        lines.append(f"{branch} {esc(item.get('label'))} — {format_epoch(int(item.get('at') or 0), timezone, True)}")
        if index == 0 and omitted:
            lines.append(f"… пропущено промежуточных переходов: {omitted}")
    return "\n".join(lines)


def deal_detail_view(detail: dict[str, Any], base_url: str, back_callback: str) -> tuple[str, list[list[dict[str, str]]]]:
    return deal_detail_text(detail, ZoneInfo(detail.get("timezone", "Asia/Krasnoyarsk"))), [
        [{"text": "Сделка в amoCRM", "url": f"{base_url.rstrip('/')}/leads/detail/{int(detail['id'])}"}],
        [{"text": "← К списку сделок", "callback_data": back_callback}],
    ]
