"""Publish the playtime report, optionally sampling opted-in VRChat friends."""

import argparse
import json
import os
import time
from pathlib import Path

from session_state import load_cookies
from .model import apply_sample, interrupt, leaderboard
from .storage import Store
from .vrchat import ApiFailure, Client

BLOCKED = {"waiting_for_login", "login_required", "access_denied"}


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
        print("[playtime] Sample complete; online friends=" + str(len(friends)) + "; accepted=" + str(accepted))
    except ApiFailure as error:
        state = interrupt(state, error.status, int(time.time()), error.retry_after)
        print("[playtime] Sampling paused: " + error.status)
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
    if args.action != "publish":
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
