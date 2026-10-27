"""Publish only Vercel's public report to the existing VRChat-trusted Pages URL."""

import json
import os
from pathlib import Path
import re
from urllib.request import HTTPRedirectHandler, Request, build_opener

SOURCE = "https://5ul-calendar-feed.vercel.app/playtime.json"
MAX_BYTES = 256 * 1024
STATUSES = {"ok", "stale", "sampling", "waiting_for_login", "login_required", "access_denied",
            "configuration_error", "logout_failed", "rate_limited", "api_error", "network_error",
            "invalid_response", "unstable_snapshot", "snapshot_too_slow", "page_limit", "time_budget", "ready"}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


def validated_report(value):
    if not isinstance(value, dict) or value.get("status") not in STATUSES or value.get("estimated") is not True:
        raise ValueError("Invalid public counter report")
    updated, rows = value.get("updated"), value.get("top")
    if (not isinstance(updated, str) or (updated and not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", updated))
            or not isinstance(rows, list) or len(rows) > 120):
        raise ValueError("Invalid public report structure")
    safe = []
    for rank, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or type(row.get("rank")) is not int or row["rank"] != rank
                or type(row.get("seconds")) is not int or row["seconds"] < 0
                or not isinstance(row.get("name"), str) or not 0 < len(row["name"]) <= 64
                or re.search(r"[<>\x00-\x1f\x7f]", row["name"])):
            raise ValueError("Invalid leaderboard row")
        safe.append({key: row[key] for key in ("rank", "name", "seconds")})
    # No unexpected private fields from a future backend version are forwarded.
    return {"updated": updated, "status": value["status"], "estimated": True, "top": safe}


def fetch_report():
    request = Request(SOURCE, headers={"User-Agent": "FiveUlPublicReportPublisher/1.0"})
    with build_opener(NoRedirects()).open(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError("Public report unavailable")
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("Public report exceeds size limit")
    return validated_report(json.loads(payload))


def main():
    report = fetch_report()
    destination = Path("docs/playtime.json")
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("## Vercel public report\n\nCounter status: **" + report["status"]
                          + "**\n\nLast successful sample: " + (report["updated"] or "none")
                          + "\n\nThis job only publishes a public JSON report. It does not log in to VRChat.\n")
    print("[publish] Counter status=" + report["status"] + "; last_sample=" + (report["updated"] or "none"))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("[publish] Stopped safely: " + type(error).__name__ + "; previous Pages report retained")
        raise SystemExit(1) from None
