"""One-time recovery from the last report published before the Redis outage."""

from datetime import datetime, timezone

from .calendar import validated_report as validate_calendar
from .model import IMPORTED_PLAYER_PREFIX, new_state
from .publish import fetch_json, validated_report

SOURCE_COMMIT = "525a7d9496aa10c35099a6d292377e532910f0cd"
SOURCE = f"https://raw.githubusercontent.com/korichneviykakiish/5ul-calendar-feed/{SOURCE_COMMIT}/docs"
# These existing jobs were verified against their respective 5ul endpoints before recovery.
SCHEDULER_JOB_ID = 8734098
PUBLICATION_JOB_ID = 8734939


def recovered_state(account, world_id):
    report = validated_report(fetch_json(SOURCE + "/playtime.json"))
    if report["status"] != "ok" or report["updated"] != "2026-11-24 22:09 UTC" or len(report["top"]) != 110:
        raise ValueError("Unexpected recovery snapshot")
    calendar = validate_calendar(fetch_json(SOURCE + "/events.json"))
    state = new_state(account, world_id)
    state["players"] = {
        f"{IMPORTED_PLAYER_PREFIX}{row['rank']:03}": {
            "name": row["name"], "seconds": row["seconds"], "present": False,
        }
        for row in report["top"]
    }
    state["last_success"] = int(datetime.strptime(report["updated"], "%Y-%m-%d %H:%M UTC")
                                .replace(tzinfo=timezone.utc).timestamp())
    state["calendar"] = {"report": calendar}
    state["scheduler_job_id"] = SCHEDULER_JOB_ID
    state["publication_job_id"] = PUBLICATION_JOB_ID
    state["initial_login_pending"] = True
    return state
