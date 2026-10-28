"""One-minute API polling, independent of GitHub Actions and local files."""

import argparse
import copy
import hmac
import json
import os
import time
from pathlib import Path

from .auth import login_client
from .calendar import advance as advance_calendar, status as calendar_status, validated_report as calendar_report
from .model import interrupt, leaderboard, new_state
from .run import process
from .serverless_store import RedisStore
from .vrchat import ApiFailure, Client
from .scheduler import configure as configure_schedule, status as schedule_status, rearm as rearm_schedule
from .publication_scheduler import configure as configure_publication, status as publication_status

POLL_SECONDS = 60
WORK_SECONDS = 18
AUTO_LOGIN_COOLDOWN = 30 * 60
RECOVERY_FIELDS = ("session_recovery_pending", "auto_login_blocked", "last_login_attempt_at",
                   "last_login_method", "auto_login_attempts")
BLOCKED = {"waiting_for_login", "login_required", "access_denied", "configuration_error", "logout_failed"}
RETIRED_WORLD_ID = "wrld_febe4b16-315b-4b3b-9c7f-3ff92ee53e92"
REQUESTED_WORLD_ID = "wrld_bf7ec577-6b45-4aa2-839d-376b8a366c21"


def configuration():
    return json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))


def configured_store(config):
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url and not token:
        url = os.environ.get("KV_REST_API_URL", "")
        token = os.environ.get("KV_REST_API_TOKEN", "")
    if not url or not token:
        raise ValueError("Configure a complete Upstash REST URL/token pair")
    return RedisStore(url, token,
                      os.environ["VRC_SESSION_KEY"], os.environ["VRC_USERNAME"], config["world_id"])


def authorized(header, secret):
    return (len(secret) >= 32 and secret.isascii()
            and hmac.compare_digest(header.encode("utf-8"), ("Bearer " + secret).encode("ascii")))


def public_report(state, config, now):
    report = leaderboard(state, config["leaderboard_size"])
    if report["status"] == "ok" and (state["last_success"] is None
                                     or now - state["last_success"] > config["max_gap_seconds"]):
        report["status"] = "stale"
    return report


def recovery_status(state, config):
    previous = state.get("last_login_attempt_at")
    if previous is not None and (type(previous) is not int or previous < 0):
        raise ValueError("Invalid login attempt timestamp")
    pending = state.get("session_recovery_pending") is True
    blocked = state.get("auto_login_blocked") is True
    return {"enabled": config.get("auto_recover_session") is True,
            "recovery_pending": pending, "manual_required": blocked or (
                (state["status"] in BLOCKED or not state["cookies"].get("auth")) and not pending),
            "cooldown_seconds": AUTO_LOGIN_COOLDOWN, "last_login_attempt_at": previous,
            "last_login_method": state.get("last_login_method"),
            "auto_login_attempts": state.get("auto_login_attempts", 0),
            "next_login_allowed": max(state["retry_at"], previous + AUTO_LOGIN_COOLDOWN if previous is not None else 0)}


def tick(store, config, contact, resume=False, now=None):
    now = int(time.time()) if now is None else now
    bucket = now // POLL_SECONDS
    if not store.acquire():
        return {"status": "busy", "sampled": False}
    try:
        state = store.load()
        if now < state["retry_at"]:
            return {"status": state["status"], "sampled": False, "retry_at": state["retry_at"]}
        if bucket <= state.get("last_tick", 0):
            return {"status": "already_checked", "sampled": False}
        automatic = False
        if not resume and (state["status"] in BLOCKED or not state["cookies"].get("auth")):
            policy = recovery_status(state, config)
            automatic = (policy["enabled"] and state["status"] == "login_required"
                         and policy["recovery_pending"] and not policy["manual_required"]
                         and state["last_success"] is not None)
            if not automatic or now < policy["next_login_allowed"]:
                return {"status": state["status"] if state["status"] in BLOCKED else "login_required",
                        "sampled": False, "retry_at": policy["next_login_allowed"] if automatic else state["retry_at"],
                        "recovery_waiting": automatic}
        login_requested = resume or automatic

        # Persist the attempt before external effects. A timeout cannot trigger
        # another login in the same minute or credit an unobserved interval.
        attempt = interrupt(state, "login_required" if login_requested else "sampling", now)
        attempt.update(last_tick=bucket, last_attempt=now)
        attempt["session_recovery_pending"] = False
        if login_requested:
            # The durable latch makes a timeout/crash require manual recovery, not another login.
            attempt.update(auto_login_blocked=True, last_login_attempt_at=now,
                           last_login_method="automatic" if automatic else "manual")
            if automatic:
                attempt["auto_login_attempts"] = attempt.get("auto_login_attempts", 0) + 1
        store.save(attempt)
        state = copy.deepcopy(state)
        state.update(revision=attempt["revision"], last_tick=bucket, last_attempt=now)
        for key in RECOVERY_FIELDS:
            if key in attempt:
                state[key] = attempt[key]
        previous_success = state["last_success"]
        try:
            if login_requested:
                if automatic:
                    print("[playtime] Automatic session recovery started; one attempt; 30-minute guard")
                client = login_client(config["request_spacing_seconds"], persist=False)
            else:
                client = Client(state["cookies"], contact, config["request_spacing_seconds"])
            client.deadline = time.monotonic() + WORK_SECONDS
            # Local monotonic sequence, independent of Actions run numbers.
            state, _ = process(state, client, config, state["last_run"] + 1, now=now)
            if state["status"] == "ok" and config.get("calendar_enabled") is True:
                state = advance_calendar(state, client, int(time.time()))
                state["cookies"] = client.cookies()
        except ApiFailure as error:
            state = interrupt(state, error.status, int(time.time()), error.retry_after)
            if login_requested:
                state["cookies"] = {}
        if login_requested:
            state["session_recovery_pending"] = False
            state["auto_login_blocked"] = state["status"] != "ok"
            if state["status"] in BLOCKED or state["status"] == "rate_limited":
                state["cookies"] = {}
        store.save(state)
        return {"status": state["status"], "sampled": state["last_success"] != previous_success,
                "last_success": state["last_success"], "poll_seconds": POLL_SECONDS}
    finally:
        store.release()


def handle(method, authorization="", action="sample"):
    """Only trusted stored rejection evidence permits bounded automatic recovery."""
    if method not in {"GET", "POST"}:
        return 405, {"status": "method_not_allowed"}
    if method == "POST":
        if action not in {"sample", "resume", "initialize", "schedule", "schedule-status", "schedule-rearm", "forget-previous-world",
                          "publication-schedule", "publication-status", "storage-status", "recovery-status", "calendar-status"}:
            return 400, {"status": "invalid_action"}
        admin_action = action != "sample"
        secret_name = "PLAYTIME_ADMIN_SECRET" if admin_action else "CRON_SECRET"
        secret = os.environ.get(secret_name, "")
        if admin_action and secret == os.environ.get("CRON_SECRET", ""):
            return 503, {"status": "separate_admin_secret_required"}
        if not authorized(authorization, secret):
            return 401, {"status": "unauthorized"}
        if (os.environ.get("VERCEL_ENV") != "production"
                or (action not in {"initialize", "schedule-status", "forget-previous-world",
                                   "publication-schedule", "publication-status", "storage-status", "recovery-status", "calendar-status"}
                    and os.environ.get("PLAYTIME_ENABLED") != "true")):
            return 503, {"status": "disabled"}
    try:
        config = configuration()
        store = configured_store(config)
        if method == "GET":
            state = store.load()
            if action == "calendar":
                report = state.get("calendar", {}).get("report")
                return (200, calendar_report(report)) if report else (503, {"status": "calendar_not_ready"})
            return 200, public_report(state, config, int(time.time()))
        if action == "initialize":
            store.initialize(new_state(os.environ["VRC_USERNAME"], config["world_id"]))
            return 200, {"status": "initialized", "sampled": False, "poll_seconds": POLL_SECONDS}
        if action == "forget-previous-world":
            if config["world_id"] != REQUESTED_WORLD_ID:
                raise ValueError("Activate the requested world before removing the previous counter")
            removed = store.forget_world(RETIRED_WORLD_ID)
            return 200, {"status": "previous_world_removed", "deleted_state_keys": removed}
        if action == "schedule":
            return 200, configure_schedule(store)
        if action == "schedule-status":
            return 200, schedule_status(store)
        if action == "schedule-rearm":
            return 200, rearm_schedule(store, config)
        if action == "publication-schedule":
            return 200, configure_publication(store)
        if action == "publication-status":
            return 200, publication_status(store)
        if action == "storage-status":
            return 200, {"status": "storage_capability", "native_conditional_release": store.native_release_supported()}
        if action == "recovery-status":
            return 200, {"status": "recovery_policy", **recovery_status(store.load(), config)}
        if action == "calendar-status":
            return 200, {"status": "calendar_state", **calendar_status(store.load())}
        result = tick(store, config, os.environ["VRC_CONTACT"], resume=action == "resume")
        # A policy wait must keep its clock alive, without claiming a successful sample.
        healthy = {"ok", "busy", "already_checked"}
        waiting = (action == "sample" and result["status"] == "login_required"
                   and result.get("recovery_waiting") is True and result.get("sampled") is False)
        return (200 if result["status"] in healthy or waiting else 503), result
    except Exception as error:
        print("[playtime] Serverless check stopped safely: " + type(error).__name__)
        return 503, {"status": "check_failed", "sampled": False}


def initialize_empty():
    config = configuration()
    state = new_state(os.environ["VRC_USERNAME"], config["world_id"])
    configured_store(config).initialize(state)
    print("[playtime] Empty counter initialized; no old hours read; manual login required")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize-empty", action="store_true", required=True)
    parser.parse_args()
    try:
        initialize_empty()
    except Exception as error:
        print("[playtime] Initialization stopped safely: " + type(error).__name__)
        raise SystemExit(1) from None
