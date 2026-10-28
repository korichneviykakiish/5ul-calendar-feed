"""Incremental calendar reads on the counter's existing, leased API session.

Public validation stays standard-library-only for the GitHub publisher.
"""

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re

INTERVAL = 1800
RETRY_SECONDS = 300
REQUESTS_PER_TICK = 2
MAX_PAGES = 5
MAX_EVENTS = 90
GROUP_ID = re.compile(r"grp_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def clean(value, limit, multiline=False):
    if not isinstance(value, str):
        raise ValueError("Invalid calendar text")
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[^\u0410-\u044f\u0401\u0451A-Za-z0-9 .,:;!?()\[\]{}+\-=/*%\"'#&_|\\\u2014\u2013\u00ab\u00bb\u2192\u2022\u2116\u2026\n]", " ", value)
    if not multiline:
        value = value.replace("\n", " ")
    return re.sub(r" +", " ", value).strip()[:limit]


def groups_config():
    value = json.loads((Path(__file__).parents[1] / "groups.json").read_text(encoding="utf-8"))
    groups, seen = [], set()
    for group in value["groups"]:
        gid = group["id"]
        if not isinstance(gid, str) or not GROUP_ID.fullmatch(gid) or gid in seen:
            raise ValueError("Invalid or duplicate calendar group")
        seen.add(gid)
        groups.append({"id": gid, "name": clean(group.get("name", gid), 80)})
    if not 1 <= len(groups) <= 24:
        raise ValueError("Invalid calendar group count")
    return groups


def validated_report(value):
    if (not isinstance(value, dict) or type(value.get("gen")) is not int or value["gen"] <= 0
            or not isinstance(value.get("groups"), list) or not 1 <= len(value["groups"]) <= 24):
        raise ValueError("Invalid public calendar")
    groups, seen, total = [], set(), 0
    for group in value["groups"]:
        if not isinstance(group, dict):
            raise ValueError("Invalid calendar group")
        gid = group.get("id")
        if not isinstance(gid, str) or not GROUP_ID.fullmatch(gid) or gid in seen:
            raise ValueError("Invalid calendar group identifier")
        seen.add(gid)
        rows = group.get("events")
        if not isinstance(rows, list):
            raise ValueError("Invalid calendar events")
        events = []
        for row in rows:
            if (not isinstance(row, dict) or row.get("acc") != "public"
                    or any(type(row.get(k)) is not int for k in ("s", "e", "cnt", "q"))
                    or row["s"] < 0 or row["e"] < row["s"] or row["cnt"] < 0 or row["q"] not in (0, 1)):
                raise ValueError("Invalid or non-public event")
            events.append({"t": clean(row.get("t"), 80), "d": clean(row.get("d"), 280, True),
                           **{key: row[key] for key in ("s", "e", "cnt", "q", "acc")}})
        total += len(events)
        groups.append({"id": gid, "name": clean(group.get("name"), 80), "events": events})
    if total > MAX_EVENTS:
        raise ValueError("Calendar exceeds the world's event capacity")
    return {"gen": value["gen"], "groups": groups}


def month_queries(now):
    start = datetime.fromtimestamp(now, timezone.utc)
    end = (start + timedelta(days=35)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = (start - timedelta(hours=3)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    months = []
    while month <= end:
        months.append(month.strftime("%Y-%m-%dT00:00:00.000Z"))
        month = (month + timedelta(days=32)).replace(day=1)
    return months


def normalized_event(row, now):
    if not isinstance(row, dict):
        raise ValueError("Invalid calendar event")
    if row.get("accessType") != "public" or row.get("isDraft") or row.get("deletedAt"):
        return None
    start = datetime.fromisoformat(row["startsAt"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(row["endsAt"].replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("Invalid event time")
    starts, ends = int(start.timestamp()), int(end.timestamp())
    if ends < now - 10800 or starts > now + 35 * 86400:
        return None
    count = row.get("interestedUserCount", 0) or 0
    if type(count) is not int or count < 0 or not isinstance(row.get("id"), str):
        raise ValueError("Invalid event identity or count")
    event = {"t": clean(row.get("title", ""), 80), "d": clean(row.get("description", ""), 280, True),
             "s": starts, "e": ends, "cnt": count, "q": int("android" in (row.get("platforms") or [])),
             "acc": "public"}
    # Recurring occurrences can share a calendar ID but have different start times.
    return row["id"] + ":" + str(starts), event


def status(state):
    calendar = state.get("calendar", {})
    cycle = calendar.get("cycle") or {}
    return {"calendar_status": calendar.get("status", "not_started"),
            "calendar_generated": calendar.get("report", {}).get("gen"),
            "calendar_completed_queries": cycle.get("cursor", 0),
            "calendar_total_queries": len(cycle.get("months", [])) * len(cycle.get("groups", [])),
            "calendar_retry_at": calendar.get("retry_at", 0),
            "calendar_unavailable_groups": calendar.get("unavailable_groups", [])}


def _advance(state, client, now, groups):
    from .vrchat import ApiFailure

    calendar = copy.deepcopy(state.get("calendar", {}))
    if now < max(calendar.get("retry_at", 0), calendar.get("next_cycle_at", 0)):
        return
    signature = hashlib.sha256(json.dumps(groups, sort_keys=True).encode()).hexdigest()
    cycle = calendar.get("cycle")
    if not cycle or cycle["signature"] != signature or now - cycle["started"] > 3600:
        cycle = {"started": now, "signature": signature, "months": month_queries(now),
                 "groups": groups, "cursor": 0, "offset": 0, "pending": {}, "unavailable": []}
    calendar.update(cycle=cycle, status="collecting", retry_at=0)
    state["calendar"] = calendar
    total = len(groups) * len(cycle["months"])
    for _ in range(REQUESTS_PER_TICK):
        group = groups[cycle["cursor"] // len(cycle["months"])]
        gid = group["id"]
        month = cycle["months"][cycle["cursor"] % len(cycle["months"])]
        try:
            body = client.call("GET", "/calendar/" + gid,
                               {"date": month, "n": 100, "offset": cycle["offset"]}, operation="calendar-events")
        except ApiFailure as error:
            if error.http_status != 404:
                raise
            # A removed group must not block all other groups or erase its cached events.
            cycle["unavailable"].append(gid)
            previous = (g for g in calendar.get("report", {}).get("groups", []) if g["id"] == gid)
            cycle["pending"][gid] = {str(i): e for i, e in enumerate(next(previous, {}).get("events", []))}
            cycle["cursor"] = (cycle["cursor"] // len(cycle["months"]) + 1) * len(cycle["months"])
            cycle["offset"] = 0
        else:
            if (not isinstance(body, dict) or not isinstance(body.get("results"), list)
                    or len(body["results"]) > 100 or type(body.get("hasNext")) is not bool):
                raise ValueError("Invalid calendar page")
            rows = body["results"]
            if body["hasNext"] and (not rows or cycle["offset"] + len(rows) >= 100 * MAX_PAGES):
                raise ValueError("Calendar pagination limit")
            pending = dict(cycle["pending"].get(gid, {}))
            for row in rows:
                event = normalized_event(row, cycle["started"])
                if event:
                    pending[event[0]] = event[1]
            cycle["pending"][gid] = dict(sorted(pending.items(), key=lambda pair: pair[1]["s"])[:MAX_EVENTS])
            if body["hasNext"]:
                cycle["offset"] += len(rows)
            else:
                cycle["cursor"] += 1
                cycle["offset"] = 0
        if cycle["cursor"] == total:
            selected = sorted(((e["s"], g["id"], e) for g in groups
                               for e in cycle["pending"].get(g["id"], {}).values() if e["e"] >= now - 10800),
                              key=lambda row: (row[0], row[1]))[:MAX_EVENTS]
            report_groups = [{**g, "events": [e for _, gid2, e in selected if gid2 == g["id"]]} for g in groups]
            generated = calendar.get("report", {}).get("gen", cycle["started"]) if cycle["unavailable"] else cycle["started"]
            calendar.update(report=validated_report({"gen": generated, "groups": report_groups}),
                            status="partial" if cycle["unavailable"] else "ok", cycle=None,
                            next_cycle_at=cycle["started"] + INTERVAL, unavailable_groups=cycle["unavailable"])
            return


def advance(state, client, now):
    from .model import interrupt
    from .vrchat import ApiFailure

    try:
        _advance(state, client, now, groups_config())
    except ApiFailure as error:
        if error.status == "time_budget":
            return state
        calendar = state.setdefault("calendar", {})
        calendar.update(status=error.status, retry_at=now + max(RETRY_SECONDS, error.retry_after))
        if error.status in {"login_required", "access_denied", "rate_limited"}:
            state = interrupt(state, error.status, now, error.retry_after)
            state.update(auto_login_blocked=True, session_recovery_pending=False)
        print("[calendar] Read stopped: " + error.status + "; previous feed retained")
    except (ValueError, KeyError, TypeError, OverflowError, AttributeError, OSError):
        state.setdefault("calendar", {}).update(status="invalid_response", retry_at=now + RETRY_SECONDS)
        print("[calendar] Invalid data; previous feed retained")
    return state
