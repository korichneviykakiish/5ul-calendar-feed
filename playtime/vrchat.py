"""Bounded API reads using an existing session; never automatic password login."""

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

API = "https://api.vrchat.cloud/api/1"


class ApiFailure(Exception):
    def __init__(self, status, retry_after=0):
        super().__init__(status)
        self.status = status
        self.retry_after = retry_after


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
        self.session.headers["User-Agent"] = "FiveUlPlaytime/1.0 (+" + contact + ")"
        self.session.headers["Accept"] = "application/json"
        self.sleep = sleep
        self.spacing = spacing
        self.calls = 0
        for name in ("auth", "twoFactorAuth"):
            if cookies.get(name):
                self.session.cookies.set(name, cookies[name], domain="api.vrchat.cloud", secure=True)

    def call(self, method, path, params=None):
        if self.calls:
            self.sleep(self.spacing)
        self.calls += 1
        try:
            response = self.session.request(method, API + path, params=params,
                                            timeout=(10, 25), allow_redirects=False)
        except requests.RequestException:
            raise ApiFailure("network_error") from None
        if response.status_code == 429:
            raise ApiFailure("rate_limited", retry_delay(response.headers.get("Retry-After", "")))
        if response.status_code == 401:
            raise ApiFailure("login_required")
        if response.status_code == 403:
            raise ApiFailure("access_denied")
        if response.status_code != 200:
            raise ApiFailure("api_error")
        try:
            return response.json()
        except ValueError:
            raise ApiFailure("invalid_response") from None

    def verify(self):
        user = self.call("GET", "/auth/user")
        if not isinstance(user, dict) or "requiresTwoFactorAuth" in user or not user.get("id"):
            raise ApiFailure("login_required")

    def friends(self, max_pages, max_duration):
        started = time.monotonic()
        found = []
        seen = set()
        for page in range(max_pages):
            rows = self.call("GET", "/auth/user/friends",
                             {"offline": "false", "n": 100, "offset": page * 100})
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
                         {"type": "friendRequest", "n": min(100, limit), "offset": 0})
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
            self.call("PUT", "/auth/user/notifications/" + notification + "/accept")
            accepted += 1
        return accepted

    def cookies(self):
        return {c.name: c.value for c in self.session.cookies if c.name in ("auth", "twoFactorAuth")}
