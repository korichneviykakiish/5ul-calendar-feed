"""Bounded API reads using an existing session; never automatic password login."""

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

from session_state import user_agent

API = "https://api.vrchat.cloud/api/1"


class ApiFailure(Exception):
    def __init__(self, status, retry_after=0, operation="api", http_status=0, reason="unspecified"):
        super().__init__(status)
        self.status = status
        self.retry_after = retry_after
        self.operation = operation
        self.http_status = http_status
        self.reason = reason


def auth_reason(response):
    try:
        body = response.json()
    except ValueError:
        return "non-json-response"
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return "unclassified"
    return {
        "missing credentials": "missing-credentials",
        "unauthorized": "unauthorized",
        "invalid credentials": "invalid-credentials",
        "logging in from too many places? check your email for verification link": "email-location-verification",
    }.get(message.strip().strip('"').rstrip(".").casefold(), "unclassified")


def retry_delay(value):
    try:
        if value.isascii() and value.isdigit():
            return int(value)
        return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return 0


class Client:
    def __init__(self, cookies, contact, spacing=2, session=None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent(contact)
        self.session.headers["Accept"] = "application/json"
        self.sleep = sleep
        self.spacing = spacing
        self.calls = 0
        for name in ("auth", "twoFactorAuth"):
            if cookies.get(name):
                self.session.cookies.set(name, cookies[name], domain="api.vrchat.cloud", secure=True)

    def call(self, method, path, params=None, operation="api"):
        if self.calls:
            self.sleep(self.spacing)
        self.calls += 1
        try:
            response = self.session.request(method, API + path, params=params,
                                            timeout=(10, 25), allow_redirects=False)
        except requests.RequestException:
            raise ApiFailure("network_error", operation=operation) from None
        if response.status_code == 429:
            raise ApiFailure("rate_limited", retry_delay(response.headers.get("Retry-After", "")),
                             operation, 429, auth_reason(response))
        if response.status_code == 401:
            raise ApiFailure("login_required", operation=operation, http_status=401, reason=auth_reason(response))
        if response.status_code == 403:
            raise ApiFailure("access_denied", operation=operation, http_status=403, reason=auth_reason(response))
        if response.status_code != 200:
            raise ApiFailure("api_error", operation=operation, http_status=response.status_code)
        try:
            return response.json()
        except ValueError:
            raise ApiFailure("invalid_response", operation=operation, http_status=200) from None

    def verify(self):
        user = self.call("GET", "/auth/user", operation="verify-session")
        if isinstance(user, dict) and "requiresTwoFactorAuth" in user:
            raise ApiFailure("login_required", operation="verify-session", http_status=200,
                             reason="two-factor-required")
        if not isinstance(user, dict) or not str(user.get("id", "")).startswith("usr_"):
            raise ApiFailure("login_required", operation="verify-session", http_status=200,
                             reason="missing-user-id")

    def friends(self, max_pages, max_duration):
        started = time.monotonic()
        found = []
        seen = set()
        for page in range(max_pages):
            rows = self.call("GET", "/auth/user/friends",
                             {"offline": "false", "n": 100, "offset": page * 100}, operation="list-friends")
            if not isinstance(rows, list) or len(rows) > 100:
                raise ApiFailure("invalid_response")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                    raise ApiFailure("invalid_response")
                if row["id"] in seen:
                    raise ApiFailure("unstable_snapshot")
                seen.add(row["id"])
                found.append(row)
            if time.monotonic() - started > max_duration:
                raise ApiFailure("snapshot_too_slow")
            if len(rows) < 100:
                return found
        raise ApiFailure("page_limit")

    def accept_requests(self, limit):
        rows = self.call("GET", "/auth/user/notifications",
                         {"type": "friendRequest", "n": min(100, limit), "offset": 0}, operation="list-requests")
        if not isinstance(rows, list):
            raise ApiFailure("invalid_response")
        accepted = 0
        for row in rows[:limit]:
            if not isinstance(row, dict) or row.get("type") != "friendRequest":
                continue
            notification = row.get("id", "")
            if not isinstance(notification, str) or not notification.startswith(("frq_", "not_")):
                raise ApiFailure("invalid_response")
            if not all(c.isascii() and (c.isalnum() or c in "_-") for c in notification):
                raise ApiFailure("invalid_response")
            self.call("PUT", "/auth/user/notifications/" + notification + "/accept", operation="accept-request")
            accepted += 1
        return accepted

    def cookies(self):
        return {c.name: c.value for c in self.session.cookies if c.name in ("auth", "twoFactorAuth")}
