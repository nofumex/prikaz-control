from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo


def local_day_bounds(day: dt.date, timezone: ZoneInfo) -> tuple[str, str]:
    start = dt.datetime.combine(day, dt.time.min, timezone).astimezone(dt.timezone.utc).replace(tzinfo=None)
    end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, timezone).astimezone(dt.timezone.utc).replace(tzinfo=None)
    return start.isoformat(sep=" "), end.isoformat(sep=" ")


def _event_query(conn, table: str, event_type: str, start: str, end: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        f"SELECT id, user_id, case_id, amo_entity_id, created_at FROM {table} "
        "WHERE event_type=? AND success=1 AND amo_entity_type='lead' AND amo_entity_id IS NOT NULL "
        "AND created_at >= ? AND created_at < ? ORDER BY created_at, id",
        (event_type, start, end),
    )]


def _dedupe_people(rows: list[dict[str, Any]], case_platform: dict[int, tuple[str, int | None]]) -> dict[str, dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = int(row["case_id"] or 0)
        platform, user_id = case_platform.get(case_id, ("telegram", row["user_id"]))
        person_id = int(row["user_id"] or user_id or 0)
        key = f"{platform}:{person_id}"
        item = people.setdefault(key, {"platform": platform, "person_id": person_id, "lead_ids": set()})
        if row.get("amo_entity_id"):
            item["lead_ids"].add(int(row["amo_entity_id"]))
    return people


def calculate_metrics(conn, day: dt.date, timezone: ZoneInfo, pipeline_lead_ids: set[int] | None = None) -> dict[str, Any]:
    """Use source event timestamps and dedupe at person and deal level."""
    start, end = local_day_bounds(day, timezone)
    case_platform = {
        int(row["case_id"]): (str(row["platform"] or "telegram").lower(), int(row["user_id"]) if row["user_id"] is not None else None)
        for row in conn.execute("SELECT id AS case_id, platform, user_id FROM cases")
    }
    # Bot-start is the exact CRM stage event. Phone collection also has a durable
    # MailingAction timestamp when the consultation campaign captured the phone.
    started = _dedupe_people(_event_query(conn, "crm_sync_logs", "user_started_bot", start, end), case_platform)
    contacts = _dedupe_people(_event_query(conn, "crm_sync_logs", "phone_provided", start, end), case_platform)

    margin = dt.timedelta(days=1)
    query_start = (dt.datetime.fromisoformat(start) - margin).isoformat(sep=" ")
    query_end = (dt.datetime.fromisoformat(end) + margin).isoformat(sep=" ")
    mailing_rows = conn.execute(
        "SELECT ma.id, ma.user_id, ma.case_id, ma.payload_json, ma.created_at, c.platform, c.user_id AS case_user_id "
        "FROM mailing_actions ma JOIN cases c ON c.id=ma.case_id "
        "WHERE ma.event_type='mailing_phone_received' AND ma.created_at >= ? AND ma.created_at < ?",
        (query_start, query_end),
    )
    campaign_events: list[dict[str, Any]] = []
    for row in mailing_rows:
        record = dict(row)
        try:
            payload = json.loads(record.get("payload_json") or "{}")
        except (ValueError, TypeError):
            payload = {}
        occurred = payload.get("mailing_event_at") or record.get("created_at")
        if not occurred:
            continue
        parsed = dt.datetime.fromisoformat(str(occurred).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        if parsed.astimezone(timezone).date() != day:
            continue
        campaign_events.append({"user_id": record["user_id"], "case_id": record["case_id"], "amo_entity_id": None})
    campaign_people = _dedupe_people(campaign_events, case_platform)
    for key, person in campaign_people.items():
        existing = contacts.setdefault(key, {**person, "lead_ids": set()})
        # Include every lead associated to this person's case, including when the
        # campaign action itself has no amo log row.
        for row in conn.execute("SELECT amocrm_lead_id, amo_lead_id FROM cases WHERE user_id=?", (person["person_id"],)):
            for lead in row:
                if lead:
                    existing["lead_ids"].add(int(lead))

    payment_rows = conn.execute(
        "SELECT p.id, p.case_id, p.paid_at, c.user_id, c.platform, c.amocrm_lead_id, c.amo_lead_id "
        "FROM payments p JOIN cases c ON c.id=p.case_id "
        "WHERE p.status='paid' AND p.paid_at IS NOT NULL AND p.paid_at >= ? AND p.paid_at < ?",
        (start, end),
    )
    payers: dict[str, dict[str, Any]] = {}
    for row in payment_rows:
        record = dict(row)
        key = f"{str(record['platform'] or 'telegram').lower()}:{int(record['user_id'])}"
        item = payers.setdefault(key, {"platform": str(record['platform'] or 'telegram').lower(), "person_id": int(record['user_id']), "lead_ids": set()})
        for lead in (record.get("amocrm_lead_id"), record.get("amo_lead_id")):
            if lead:
                item["lead_ids"].add(int(lead))
    for people in (started, contacts, payers):
        for person in people.values():
            person["lead_ids"] = sorted(person["lead_ids"] if pipeline_lead_ids is None else person["lead_ids"] & pipeline_lead_ids)

    # Reporting is deal-based: a person without a linked Судебный приказ lead
    # cannot appear in these metrics or in the drill-down list.
    started = {key: person for key, person in started.items() if person["lead_ids"]}
    contacts = {key: person for key, person in contacts.items() if person["lead_ids"]}
    payers = {key: person for key, person in payers.items() if person["lead_ids"]}

    telegram = sum(p["platform"] == "telegram" for p in started.values())
    max_count = sum(p["platform"] == "max" for p in started.values())
    return {
        "date": day.isoformat(),
        "timezone": str(timezone),
        "subscribed": {"telegram": telegram, "max": max_count},
        "contact_count": len(contacts),
        "paid_count": len(payers),
        "deals": {
            "subscribed_telegram": sorted({lead for p in started.values() if p["platform"] == "telegram" for lead in p["lead_ids"]}),
            "subscribed_max": sorted({lead for p in started.values() if p["platform"] == "max" for lead in p["lead_ids"]}),
            "contact": sorted({lead for p in contacts.values() for lead in p["lead_ids"]}),
            "paid": sorted({lead for p in payers.values() for lead in p["lead_ids"]}),
        },
        # Temporary person-level detail lets the bot re-count after amoCRM
        # confirms which linked deals actually traversed the Judicial order stage.
        "_people": {
            key: [{"platform": p["platform"], "person_id": p["person_id"], "lead_ids": p["lead_ids"]} for p in people.values()]
            for key, people in (("started", started), ("contact", contacts), ("paid", payers))
        },
    }


def retain_verified_deals(report: dict[str, Any], verified_lead_ids: set[int]) -> dict[str, Any]:
    """Filter metrics and unique people to leads verified in the target funnel."""
    key_map = {
        "started": {"telegram": "subscribed_telegram", "max": "subscribed_max"},
        "contact": {"all": "contact"},
        "paid": {"all": "paid"},
    }
    counts: dict[str, int] = {}
    for group, dimensions in key_map.items():
        people = []
        for person in report["_people"][group]:
            person["lead_ids"] = sorted(set(person["lead_ids"]) & verified_lead_ids)
            if person["lead_ids"]:
                people.append(person)
        report["_people"][group] = people
        if group == "started":
            for platform, label in dimensions.items():
                counts[label] = sum(p["platform"] == platform for p in people)
        else:
            counts[next(iter(dimensions.values()))] = len(people)
        for label in dimensions.values():
            report["deals"][label] = sorted({lead_id for person in people for lead_id in person["lead_ids"]})
    report["subscribed"] = {"telegram": counts["subscribed_telegram"], "max": counts["subscribed_max"]}
    report["contact_count"] = counts["contact"]
    report["paid_count"] = counts["paid"]
    report.pop("_people", None)
    return report
