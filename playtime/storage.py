"""Encrypted, replaceable release snapshots; accumulated time is not a cache."""

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from cryptography.fernet import Fernet

from .model import new_state, validate_state

TAG = "playtime-state-v1"
ASSET = re.compile(r"^state-(\d{12})-(\d{8})\.enc$")
MAX_BYTES = 16 * 1024 * 1024


def retained_assets(assets):
    snapshots = [(int(ASSET.fullmatch(a["name"])[1]), ASSET.fullmatch(a["name"])[2], a["id"])
                 for a in assets if ASSET.fullmatch(a["name"])]
    snapshots.sort(reverse=True)
    keep = {entry[2] for entry in snapshots[:2]}
    days = set()
    for _, day, asset_id in snapshots:
        if day not in days and len(days) < 7:
            days.add(day)
            keep.add(asset_id)
    return keep


class Store:
    def __init__(self, repository, token, key, account, world_id, allow_create=False):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("Invalid repository")
        self.base = "https://api.github.com/repos/" + repository
        self.session = requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token,
                                     "Accept": "application/vnd.github+json",
                                     "User-Agent": "FiveUlPlaytime/1.0",
                                     "X-GitHub-Api-Version": "2022-11-28"})
        self.cipher = Fernet(key.encode("ascii"))
        self.account = account
        self.world_id = world_id
        self.allow_create = allow_create
        self.release = None

    def request(self, method, path, missing=False, **kwargs):
        response = self.session.request(method, self.base + path, timeout=(10, 40), **kwargs)
        if missing and response.status_code == 404:
            return None
        if not response.ok:
            raise RuntimeError("GitHub storage request failed: HTTP " + str(response.status_code))
        return response.json() if response.content else None

    def assets(self):
        assets = []
        for page in range(1, 11):
            batch = self.request("GET", "/releases/" + str(self.release["id"]) + "/assets",
                                 params={"per_page": 100, "page": page})
            assets.extend(batch)
            if len(batch) < 100:
                return assets
        raise RuntimeError("Too many state assets; refusing incomplete listing")

    def decode(self, asset):
        if asset["size"] > MAX_BYTES:
            raise ValueError("State exceeds storage safety limit")
        response = self.session.get(self.base + "/releases/assets/" + str(asset["id"]),
                                    headers={"Accept": "application/octet-stream"}, timeout=(10, 40))
        if not response.ok or len(response.content) > MAX_BYTES:
            raise RuntimeError("Cannot download saved state")
        state = json.loads(self.cipher.decrypt(response.content))
        validate_state(state, self.account, self.world_id)
        if state["revision"] != int(ASSET.fullmatch(asset["name"])[1]):
            raise ValueError("Snapshot revision mismatch")
        return state

    def load(self):
        self.release = self.request("GET", "/releases/tags/" + TAG, missing=True)
        if self.release is None:
            if not self.allow_create:
                raise RuntimeError("Saved release is missing; manual recovery required")
            self.release = self.request("POST", "/releases", json={
                "tag_name": TAG, "name": "Encrypted playtime checkpoints",
                "body": "Machine-managed encrypted state. Do not remove or replace the encryption key. No credentials or player locations are published in plaintext.",
                "prerelease": True, "make_latest": "false",
            })
            return new_state(self.account, self.world_id)
        snapshots = [a for a in self.assets() if ASSET.fullmatch(a["name"])]
        if not snapshots:
            # An established release losing all checkpoints must not reset everyone.
            raise RuntimeError("Existing state release is empty; manual recovery required")
        latest = max(snapshots, key=lambda a: int(ASSET.fullmatch(a["name"])[1]))
        return self.decode(latest)

    def save(self, state):
        validate_state(state, self.account, self.world_id)
        state["revision"] += 1
        payload = self.cipher.encrypt(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if len(payload) > MAX_BYTES:
            raise ValueError("State exceeds storage safety limit")
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        name = "state-" + format(state["revision"], "012d") + "-" + day + ".enc"
        upload = self.release["upload_url"].split("{", 1)[0]
        if urlsplit(upload).scheme != "https" or urlsplit(upload).netloc != "uploads.github.com":
            raise ValueError("Unexpected upload host")
        response = self.session.post(upload, params={"name": name}, data=payload,
                                     headers={"Content-Type": "application/octet-stream"}, timeout=(10, 60))
        if response.status_code != 201:
            raise RuntimeError("State upload failed: HTTP " + str(response.status_code))
        asset = response.json()
        digest = asset.get("digest")
        if digest and digest != "sha256:" + hashlib.sha256(payload).hexdigest():
            raise RuntimeError("State upload checksum mismatch")
        if self.decode(asset) != state:
            raise RuntimeError("State verification failed; previous snapshots retained")
        # Upload and verify first. A failed cleanup cannot destroy the new checkpoint.
        try:
            assets = self.assets()
            keep = retained_assets(assets)
            for old in assets:
                if ASSET.fullmatch(old["name"]) and old["id"] not in keep:
                    self.request("DELETE", "/releases/assets/" + str(old["id"]))
        except (RuntimeError, requests.RequestException):
            print("[playtime] Checkpoint saved; old snapshot cleanup postponed")
