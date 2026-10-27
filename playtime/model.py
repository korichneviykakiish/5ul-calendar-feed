"""Deterministic accounting: no network, credentials, or wall-clock reads."""

import copy
import re
from datetime import datetime, timezone


def new_state(account, world_id):
    return {
        "schema": 1, "account": account.strip().casefold(), "world_id": world_id,
        "revision": 0, "last_run": 0, "last_sample": None, "last_success": None,
        "players": {}, "cookies": {}, "status": "waiting_for_login",
        "retry_at": 0, "rate_limits": 0,
    }


def validate_state(state, account, world_id):
    if not isinstance(state, dict) or state.get("schema") != 1:
        raise ValueError("Unsupported state; refusing to reset accumulated hours")
    if state.get("account") != account.strip().casefold() or state.get("world_id") != world_id:
        raise ValueError("Account/world mismatch; refusing to merge different counters")
    for key in ("revision", "last_run", "retry_at", "rate_limits"):
        if type(state.get(key)) is not int or state[key] < 0:
            raise ValueError("Invalid state counter")
    for key in ("last_sample", "last_success"):
        if state.get(key) is not None and (type(state[key]) is not int or state[key] < 0):
            raise ValueError("Invalid sample timestamp")
    for key in ("last_login_attempt_at", "auto_login_attempts"):
        if key in state and (type(state[key]) is not int or state[key] < 0):
            raise ValueError("Invalid recovery counter")
    for key in ("session_recovery_pending", "auto_login_blocked"):
        if key in state and type(state[key]) is not bool:
            raise ValueError("Invalid recovery flag")
    if "last_login_method" in state and (not isinstance(state["last_login_method"], str)
                                         or state["last_login_method"] not in {"manual", "automatic"}):
        raise ValueError("Invalid recovery method")
    if not isinstance(state.get("cookies"), dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in state["cookies"].items()
    ):
        raise ValueError("Invalid session state")
    if not isinstance(state.get("players"), dict):
        raise ValueError("Invalid player state")
    for uid, row in state["players"].items():
        if not isinstance(uid, str) or not isinstance(row, dict):
            raise ValueError("Invalid player record")
        if type(row.get("seconds")) is not int or row["seconds"] < 0:
            raise ValueError("Invalid accumulated time")
        if type(row.get("present")) is not bool or not isinstance(row.get("name"), str):
            raise ValueError("Invalid player snapshot")
    return state


def apply_sample(state, friends, sampled_at, run_number, max_gap):
    if run_number <= state["last_run"]:
        return copy.deepcopy(state)
    if type(sampled_at) is not int or sampled_at < 0:
        raise ValueError("Invalid sample time")
    if state["last_success"] is not None and sampled_at <= state["last_success"]:
        raise ValueError("Out-of-order sample; refusing to move the accounting clock backwards")
    result = copy.deepcopy(state)
    previous = state["last_sample"]
    elapsed = sampled_at - previous if previous is not None else 0
    # Two sightings estimate continuity; they are not proof of uninterrupted play.
    credit = elapsed if 0 < elapsed <= max_gap else 0
    current = {}
    for friend in friends:
        uid = friend.get("id")
        name = friend.get("displayName")
        location = friend.get("location")
        if not isinstance(uid, str) or not uid or not isinstance(name, str):
            raise ValueError("Incomplete friend snapshot")
        if uid in current:
            raise ValueError("Duplicate friend across pages; discard unstable snapshot")
        if location is not None and not isinstance(location, str):
            raise ValueError("Invalid location in friend snapshot")
        inside = isinstance(location, str) and location.split(":", 1)[0] == state["world_id"]
        # Never persist locations outside our world, or private instance identifiers.
        current[uid] = (name, inside)
    for uid, row in result["players"].items():
        if uid not in current:
            row["present"] = False
    for uid, (name, inside) in current.items():
        if uid not in result["players"]:
            if not inside:
                continue
            result["players"][uid] = {"name": name, "seconds": 0, "present": False}
        row = result["players"][uid]
        if inside and row["present"]:
            row["seconds"] += credit
        row["name"] = name
        row["present"] = inside
    result.update(last_run=run_number, last_sample=sampled_at, last_success=sampled_at,
                  status="ok", retry_at=0, rate_limits=0)
    return result


def interrupt(state, status, now, retry_after=0):
    result = copy.deepcopy(state)
    result["last_sample"] = None
    for row in result["players"].values():
        row["present"] = False
    result["status"] = status
    if status == "rate_limited":
        result["rate_limits"] += 1
        # Our conservative backoff, not a claim about the server's expiry time.
        delay = min(86400, 3600 * 2 ** min(result["rate_limits"] - 1, 5))
        result["retry_at"] = now + max(delay, int(retry_after))
    elif retry_after > 0:
        # Manual recovery must also respect a server-provided wait period.
        result["retry_at"] = max(result["retry_at"], now + int(retry_after))
    return result


def display_name(name):
    name = re.sub(r"[<>\x00-\x1f\x7f]", "", name)
    return name.strip()[:64] or "Player"


def leaderboard(state, limit=120):
    rows = [(uid, p) for uid, p in state["players"].items() if p["seconds"] > 0]
    rows.sort(key=lambda pair: (-pair[1]["seconds"], pair[0]))
    updated = state["last_success"]
    return {
        "updated": datetime.fromtimestamp(updated, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if updated else "",
        "status": state["status"], "estimated": True,
        "top": [{"rank": i + 1, "name": display_name(p["name"]), "seconds": p["seconds"]}
                for i, (_, p) in enumerate(rows[:limit])],
    }
