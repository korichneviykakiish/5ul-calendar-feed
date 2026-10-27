"""Operator-only setup of an external clock for the public Pages publisher."""

import json
import os

import requests

from .scheduler import CronClient, job_id

OWNER = "korichneviykakiish"
REPOSITORY = OWNER + "/5ul-calendar-feed"
WORKFLOW = "publish-vercel-report.yml"
API = "https://api.github.com"
PREFIX = "/repos/" + REPOSITORY
TARGET = API + PREFIX + "/actions/workflows/" + WORKFLOW + "/dispatches"
TITLE = "5ul public report - every minute"
LEGACY_TITLE = "5ul public report - every 5 minutes"
SCHEDULE = {"timezone": "UTC", "expiresAt": 0, "hours": [-1], "mdays": [-1],
            "minutes": [-1], "months": [-1], "wdays": [-1]}
BODY = {"ref": "main"}
API_VERSION = "2022-11-28"


def validate_token(token):
    if (not token.startswith("github_pat_") or not 32 <= len(token) <= 1024
            or not token.isascii() or any(character.isspace() for character in token)):
        raise ValueError("Configure a dedicated fine-grained GitHub token")


class GitHubPublisher:
    def __init__(self, token, session=None):
        validate_token(token)
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token,
                                     "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": API_VERSION,
                                     "User-Agent": "FiveUlPublicReportScheduler/1.0"})

    def call(self, method, path, expected, body=None):
        response = self.session.request(method, API + path, json=body,
                                        timeout=(3, 8), allow_redirects=False)
        if response.status_code != expected:
            raise RuntimeError("GitHub publication request failed: HTTP " + str(response.status_code))
        return response.json() if expected == 200 else None

    def verify(self):
        user = self.call("GET", "/user", 200)
        if user.get("login", "").casefold() != OWNER:
            raise ValueError("Publication token belongs to another account")
        repo = self.call("GET", PREFIX, 200)
        if (repo.get("full_name") != REPOSITORY or repo.get("default_branch") != "main"
                or repo.get("archived") is not False or repo.get("disabled") is not False):
            raise ValueError("Unexpected publication repository")
        workflow = self.call("GET", PREFIX + "/actions/workflows/" + WORKFLOW, 200)
        if workflow.get("path") != ".github/workflows/" + WORKFLOW or workflow.get("state") != "active":
            raise ValueError("Public-report workflow is not active")

    def dispatch(self):
        self.call("POST", PREFIX + "/actions/workflows/" + WORKFLOW + "/dispatches", 204, BODY)


def own_job(client, identifier, allow_legacy=False):
    job = client.call("GET", "/jobs/" + job_id(identifier)).get("jobDetails")
    titles = {TITLE, LEGACY_TITLE} if allow_legacy else {TITLE}
    if not isinstance(job, dict) or job.get("url") != TARGET or job.get("title") not in titles:
        raise ValueError("Publication job identity mismatch")
    return job


def configure(store, client=None, github=None):
    if not store.acquire():
        return {"status": "busy", "enabled": False}
    try:
        state = store.load()
        token = os.environ["GH_REPORT_DISPATCH_TOKEN"]
        validate_token(token)
        client = client or CronClient(os.environ["CRONJOB_API_KEY"])
        github = github or GitHubPublisher(token)
        listing = client.call("GET", "/jobs")
        if listing.get("someFailed") is not False or not isinstance(listing.get("jobs"), list):
            raise ValueError("Incomplete scheduler listing; no publication task created")
        matches = [job for job in listing["jobs"] if job.get("url") == TARGET]
        if len(matches) > 1 or (matches and matches[0].get("title") not in {TITLE, LEGACY_TITLE}):
            raise ValueError("Existing publication task needs operator review")
        identifier = matches[0]["jobId"] if matches else None
        saved = state.get("publication_job_id")
        if saved is not None:
            job_id(saved)
            if saved != identifier:
                raise ValueError("Saved publication task is missing or different")
        if matches and matches[0].get("title") == LEGACY_TITLE and saved != identifier:
            raise ValueError("Legacy publication task must match the saved identity")
        if identifier is not None:
            own_job(client, identifier, allow_legacy=saved == identifier)
        github.verify()
        # Verify Actions write access before storing the dedicated token in cron-job.org.
        github.dispatch()
        payload = {"title": TITLE, "url": TARGET, "enabled": False, "saveResponses": False,
                   "requestMethod": 1, "requestTimeout": 30, "redirectSuccess": False,
                   "schedule": SCHEDULE, "auth": {"enable": False},
                   "extendedData": {"headers": {"Authorization": "Bearer " + token,
                                                "Accept": "application/vnd.github+json",
                                                "Content-Type": "application/json",
                                                "User-Agent": "FiveUlPublicReportScheduler/1.0",
                                                "X-GitHub-Api-Version": API_VERSION},
                                    "body": json.dumps(BODY, separators=(",", ":"))}}
        if identifier is None:
            identifier = client.call("PUT", "/jobs", {"job": {**payload, "notification": {
                "onFailure": True, "onFailureCount": 1, "onDisable": True,
                "mode": 2, "selectedChannels": [0]}}})["jobId"]
            own_job(client, identifier)
        client.call("PATCH", "/jobs/" + job_id(identifier), {"job": {**payload, "enabled": True}})
        job = own_job(client, identifier)
        headers = job.get("extendedData", {}).get("headers", {})
        if (job.get("enabled") is not True or job.get("requestMethod") != 1
                or job.get("schedule") != SCHEDULE or job.get("saveResponses") is not False
                or headers.get("Authorization") != "Bearer " + token
                or headers.get("Content-Type") != "application/json"
                or json.loads(job.get("extendedData", {}).get("body", "null")) != BODY):
            raise ValueError("Publication scheduler settings were not confirmed")
        state["publication_job_id"] = identifier
        store.save(state)
        return {"status": "publication_schedule_ready", "job_id": identifier,
                "enabled": True, "publication_seconds": 60}
    finally:
        store.release()


def status(store, client=None):
    identifier = store.load().get("publication_job_id")
    if identifier is None:
        return {"status": "publication_not_configured", "enabled": False}
    client = client or CronClient(os.environ["CRONJOB_API_KEY"])
    job = own_job(client, identifier, allow_legacy=True)
    rows = client.call("GET", "/jobs/" + job_id(identifier) + "/history").get("history")
    if not isinstance(rows, list):
        raise ValueError("Invalid publication scheduler history")
    safe_rows = [{key: row.get(key) if type(row.get(key)) is int else None
                  for key in ("date", "datePlanned", "httpStatus", "status", "duration")}
                 for row in rows[:3]]
    return {"status": "publication_schedule_status", "job_id": identifier,
            "enabled": job.get("enabled"), "last_execution": job.get("lastExecution"),
            "next_execution": job.get("nextExecution"), "history": safe_rows}
