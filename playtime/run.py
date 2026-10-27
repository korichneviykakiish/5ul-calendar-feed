"""Publish the playtime report, optionally sampling opted-in VRChat friends."""

import argparse
import json
import os
import time
from pathlib import Path

from session_state import load_cookies
from .auth import login_client
from .model import apply_sample, interrupt, leaderboard
from .storage import Store
from .vrchat import ApiFailure, Client

BLOCKED = {"waiting_for_login", "login_required", "access_denied"}


def process_with_login(state, config, run_number, resume=False, now=None):
    now = int(time.time()) if now is None else now
    if run_number <= state["last_run"] or now < state["retry_at"]:
        print("[playtime] Login skipped: cooldown or already processed run")
        return state, False
    if not resume and state["status"] in {"login_required", "access_denied", "configuration_error", "logout_failed"}:
        print("[playtime] Login needs manual confirmation; no request sent")
        return state, False
    try:
        client = login_client(config["request_spacing_seconds"])
    except ApiFailure as error:
        result = interrupt(state, error.status, int(time.time()), error.retry_after)
        result["last_run"] = run_number
        result["cookies"] = {}
        print("[playtime] Login paused: " + json.dumps({
            "status": error.status, "reason": error.reason, "http_status": error.http_status,
        }))
        return result, True
    result = None
    try:
        result, changed = process(state, client, config, run_number, now=now)
    finally:
        # Never follow a rate limit or access rejection with another API request.
        if result is None or result["status"] not in {"rate_limited", "access_denied", "login_required"}:
            try:
                client.logout()
                print("[playtime] Sampling session closed")
            except ApiFailure as error:
                if result is not None and error.status != "login_required":
                    status = "rate_limited" if error.status == "rate_limited" else "logout_failed"
                    result = interrupt(result, status, int(time.time()), error.retry_after)
                print("[playtime] Session cleanup: " + error.status + "; no retry")
    if changed:
        result["last_run"] = run_number
        # This job's IP-bound session must not be reused on another runner.
        result["cookies"] = {}
    return result, changed


def process(state, client, config, run_number, now=None):
    now = int(time.time()) if now is None else now
    if run_number <= state["last_run"] or now < state["retry_at"]:
        return state, False
    try:
        client.verify()
        friends = client.friends(config["max_friend_pages"], config["max_sample_duration_seconds"])
        sampled = int(time.time())
        state = apply_sample(state, friends, sampled, run_number, config["max_gap_seconds"])
        # Joining is explicit opt-in. Accept incoming requests only, never send any.
        accepted = client.accept_requests(config["max_friend_requests_per_run"])
        state["session_recovery_pending"] = False
        in_world = sum(1 for row in state["players"].values() if row["present"])
        print("[playtime] Sample complete; online friends=" + str(len(friends))
              + "; in configured world=" + str(in_world) + "; accepted=" + str(accepted))
    except ApiFailure as error:
        state = interrupt(state, error.status, int(time.time()), error.retry_after)
        rejected = (error.session_rejected and error.status == "login_required"
                    and error.operation == "verify-session" and error.http_status == 401
                    and error.reason in {"missing-credentials", "unauthorized"})
        state["session_recovery_pending"] = rejected and state["last_success"] is not None
        if (error.status in {"access_denied", "rate_limited", "configuration_error"}
                or error.http_status == 429
                or (error.status == "login_required" and not rejected)):
            state["auto_login_blocked"] = True
        cookies = client.cookies()
        print("[playtime] Sampling paused: " + json.dumps({
            "status": error.status,
            "operation": error.operation,
            "http_status": error.http_status,
            "reason": error.reason,
            "auth_cookie_present": bool(cookies.get("auth")),
            "two_factor_cookie_present": bool(cookies.get("twoFactorAuth")),
        }))
    except ValueError:
        state = interrupt(state, "invalid_response", int(time.time()))
        print("[playtime] Snapshot rejected; previous hours retained")
    state["cookies"] = client.cookies()
    return state, True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("publish", "sample", "resume"), default="sample")
    args = parser.parse_args()
    config = json.loads(Path("playtime/config.json").read_text(encoding="utf-8"))
    account = os.environ["VRC_USERNAME"].strip()
    key = os.environ["VRC_SESSION_KEY"]
    store = Store(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"], key, account, config["world_id"],
                  allow_create=not Path("docs/playtime.json").exists())
    state = store.load()
    dirty = state["revision"] == 0
    if args.action != "publish" and os.environ.get("VRC_AUTH_PER_RUN") == "true":
        state, sampled = process_with_login(state, config, int(os.environ["GITHUB_RUN_NUMBER"]),
                                            resume=args.action == "resume")
        dirty = dirty or sampled
    elif args.action != "publish":
        # A manual resume may adopt a session confirmed by the calendar login workflow.
        # Neither scheduled sampling nor resume ever sends a username/password login.
        if not state["cookies"].get("auth") or args.action == "resume":
            cookies = load_cookies("state/cookies.enc", key, account)
            if cookies.get("auth"):
                state["cookies"] = cookies
                if args.action == "resume":
                    state["status"] = "ready"
                dirty = True
        if not state["cookies"].get("auth"):
            state = interrupt(state, "waiting_for_login", int(time.time()))
            dirty = True
            print("[playtime] Waiting for a confirmed session; no VRChat request sent")
        elif state["status"] in BLOCKED:
            print("[playtime] Login needs manual confirmation; no VRChat request sent")
        else:
            client = Client(state["cookies"], os.environ["VRC_CONTACT"], config["request_spacing_seconds"])
            state, sampled = process(state, client, config, int(os.environ["GITHUB_RUN_NUMBER"]))
            dirty = dirty or sampled
    if dirty:
        store.save(state)
    report = leaderboard(state, config["leaderboard_size"])
    Path("docs").mkdir(exist_ok=True)
    Path("docs/playtime.json").write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write("status=" + state["status"] + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("## Playtime counter\n\nStatus: **" + state["status"] + "**\n\n"
                         "Ranked players: " + str(len(report["top"])) + "\n\n"
                         "Last complete sample: " + (report["updated"] or "none") + "\n\n"
                         "No legacy totals imported. Polling is an estimate, not exact session duration.\n")
    print("[playtime] Report prepared; status=" + state["status"])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Requests exceptions can contain sensitive URLs; never dump arbitrary bodies.
        print("[playtime] Stopped safely: " + type(error).__name__ + "; previous published report retained")
        raise SystemExit(1) from None
