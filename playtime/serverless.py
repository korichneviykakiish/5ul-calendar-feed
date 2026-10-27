"""One-minute API polling, independent of GitHub Actions and local files."""

import argparse
import copy
import hmac
import json
import os
import time
from pathlib import Path

from .auth import login_client
from .model import interrupt, leaderboard, new_state
from .run import process
from .serverless_store import RedisStore
from .vrchat import ApiFailure, Client
from .scheduler import configure as configure_schedule, status as schedule_status

POLL_SECONDS = 60
WORK_SECONDS = 18
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
        if not resume and (state["status"] in BLOCKED or not state["cookies"].get("auth")):
            return {"status": state["status"] if state["status"] in BLOCKED else "login_required", "sampled": False}

        # Persist the attempt before external effects. A timeout cannot trigger
        # another login in the same minute or credit an unobserved interval.
        attempt = interrupt(state, "login_required" if resume else "sampling", now)
        attempt.update(last_tick=bucket, last_attempt=now)
        store.save(attempt)
        state = copy.deepcopy(state)
        state.update(revision=attempt["revision"], last_tick=bucket, last_attempt=now)
        previous_success = state["last_success"]
        try:
            if resume:
                client = login_client(config["request_spacing_seconds"], persist=False)
            else:
                client = Client(state["cookies"], contact, config["request_spacing_seconds"])
            client.deadline = time.monotonic() + WORK_SECONDS
            # Local monotonic sequence, independent of Actions run numbers.
            state, _ = process(state, client, config, state["last_run"] + 1, now=now)
        except ApiFailure as error:
            state = interrupt(state, error.status, int(time.time()), error.retry_after)
            if resume:
                state["cookies"] = {}
        store.save(state)
        return {"status": state["status"], "sampled": state["last_success"] != previous_success,
                "last_success": state["last_success"], "poll_seconds": POLL_SECONDS}
    finally:
        store.release()


def handle(method, authorization="", action="sample"):
    """Return safe HTTP responses. Neither cron credentials nor queries can log in."""
    if method not in {"GET", "POST"}:
        return 405, {"status": "method_not_allowed"}
    if method == "POST":
        if action not in {"sample", "resume", "initialize", "schedule", "schedule-status", "forget-previous-world"}:
            return 400, {"status": "invalid_action"}
        admin_action = action != "sample"
        secret_name = "PLAYTIME_ADMIN_SECRET" if admin_action else "CRON_SECRET"
        secret = os.environ.get(secret_name, "")
        if admin_action and secret == os.environ.get("CRON_SECRET", ""):
            return 503, {"status": "separate_admin_secret_required"}
        if not authorized(authorization, secret):
            return 401, {"status": "unauthorized"}
        if (os.environ.get("VERCEL_ENV") != "production"
                or (action not in {"initialize", "schedule-status", "forget-previous-world"}
                    and os.environ.get("PLAYTIME_ENABLED") != "true")):
            return 503, {"status": "disabled"}
    try:
        config = configuration()
        store = configured_store(config)
        if method == "GET":
            return 200, public_report(store.load(), config, int(time.time()))
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
        result = tick(store, config, os.environ["VRC_CONTACT"], resume=action == "resume")
        # Skips are healthy; an auth/API failure must not masquerade as success.
        healthy = {"ok", "busy", "already_checked"}
        return (200 if result["status"] in healthy else 503), result
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
