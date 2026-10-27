"""Operator-only management of this bot's one-minute cron-job.org task."""

import os
import time

import requests

API = "https://api.cron-job.org"
TARGET = "https://5ul-calendar-feed.vercel.app/api/playtime"
TITLE = "5ul playtime - every minute"
SCHEDULE = {"timezone": "UTC", "expiresAt": 0, "hours": [-1], "mdays": [-1],
            "minutes": [-1], "months": [-1], "wdays": [-1]}


class CronClient:
    def __init__(self, key, session=None, sleep=time.sleep):
        if not key:
            raise ValueError("CRONJOB_API_KEY is required")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        self.sleep, self.calls = sleep, 0

    def call(self, method, path, body=None):
        if self.calls:
            self.sleep(0.3)
        self.calls += 1
        response = self.session.request(method, API + path, json=body, timeout=(3, 8), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("Scheduler request failed: HTTP " + str(response.status_code))
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("Invalid scheduler response")
        return value


def job_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Invalid scheduler job identifier")
    return str(value)


def own_job(client, identifier):
    value = client.call("GET", "/jobs/" + job_id(identifier)).get("jobDetails")
    if not isinstance(value, dict) or value.get("url") != TARGET or value.get("title") != TITLE:
        raise ValueError("Job identity mismatch; refusing to edit another task")
    return value


def configure(store, client=None, now=None):
    now = int(time.time()) if now is None else now
    if not store.acquire():
        return {"status": "busy", "sampled": False}
    try:
        state = store.load()
        if (state["status"] != "ok" or state["last_run"] < 2 or state["last_success"] is None
                or not 0 <= now - state["last_success"] <= 180):
            raise ValueError("Verify two fresh samples before enabling the scheduler")
        client = client or CronClient(os.environ["CRONJOB_API_KEY"])
        token = os.environ["CRON_SECRET"]
        if len(token) < 32 or not token.isascii():
            raise ValueError("Invalid cron credential")
        listing = client.call("GET", "/jobs")
        if listing.get("someFailed") is not False or not isinstance(listing.get("jobs"), list):
            raise ValueError("Incomplete scheduler listing; refusing to create duplicates")
        matches = [row for row in listing["jobs"] if row.get("url") == TARGET]
        if len(matches) > 1 or (matches and matches[0].get("title") != TITLE):
            raise ValueError("An existing task needs operator review")
        payload = {"title": TITLE, "url": TARGET, "enabled": False, "saveResponses": False,
                   "requestMethod": 1, "requestTimeout": 30, "redirectSuccess": False,
                   "schedule": SCHEDULE, "auth": {"enable": False},
                   "extendedData": {"headers": {"Authorization": "Bearer " + token}, "body": ""}}
        identifier = matches[0]["jobId"] if matches else client.call("PUT", "/jobs", {"job": payload})["jobId"]
        path = "/jobs/" + job_id(identifier)
        if state.get("scheduler_job_id") not in (None, identifier):
            raise ValueError("Saved scheduler identity differs; operator review required")
        own_job(client, identifier)
        payload["notification"] = {"onFailure": True, "onFailureCount": 1, "onDisable": True,
                                   "mode": 2, "selectedChannels": [0]}
        client.call("PATCH", path, {"job": {**payload, "enabled": True}})
        saved = own_job(client, identifier)
        if (saved.get("enabled") is not True or saved.get("requestMethod") != 1
                or saved.get("schedule") != SCHEDULE
                or saved.get("extendedData", {}).get("headers", {}).get("Authorization") != "Bearer " + token):
            raise ValueError("Scheduler settings were not confirmed")
        state["scheduler_job_id"] = identifier
        store.save(state)
        return {"status": "schedule_ready", "job_id": identifier, "enabled": True, "poll_seconds": 60}
    finally:
        store.release()


def status(store, client=None):
    state = store.load()
    identifier = state.get("scheduler_job_id")
    if identifier is None:
        return {"status": "not_configured", "enabled": False}
    client = client or CronClient(os.environ["CRONJOB_API_KEY"])
    job = own_job(client, identifier)
    history = client.call("GET", "/jobs/" + job_id(identifier) + "/history")
    rows = history.get("history")
    if not isinstance(rows, list):
        raise ValueError("Invalid scheduler history")
    safe_rows = [{key: row.get(key) if type(row.get(key)) is int else None
                  for key in ("date", "datePlanned", "httpStatus", "status", "duration")}
                 for row in rows[:3]]
    return {"status": "schedule_status", "job_id": identifier, "enabled": job.get("enabled"),
            "last_execution": job.get("lastExecution"), "next_execution": job.get("nextExecution"),
            "history": safe_rows, "last_success": state["last_success"]}
