"""Encrypted checkpoint storage on Cloudflare D1."""

import hashlib
import json
import re
import secrets
import time
import zlib

import requests
from cryptography.fernet import Fernet

from .model import validate_state
from .serverless_store import LEASE_SECONDS, MAX_BYTES

TABLE = "playtime_checkpoints"
# D1 limits the entire row to 2 MB. Leave room for both encrypted revisions.
MAX_CHECKPOINT = 950_000


class MissingCheckpoint(RuntimeError):
    """The selected D1 database has no checkpoint for this world."""


class D1Store:
    def __init__(self, account_id, database_id, token, key, account, world_id, session=None):
        if (not re.fullmatch(r"[0-9a-f]{32}", account_id)
                or not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", database_id)
                or not token or not account.strip()):
            raise ValueError("Invalid Cloudflare D1 configuration")
        self.url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token})
        self.cipher = Fernet(key.encode("ascii"))
        self.account, self.world_id = account, world_id
        self.checkpoint_id = self.identity(world_id)
        self.lease = None
        self.loaded_revision = None

    def identity(self, world_id):
        return hashlib.sha256((self.account.strip().casefold() + "\n" + world_id).encode()).hexdigest()[:24]

    def query(self, sql, *params):
        response = self.session.post(self.url, json={"sql": sql, "params": [str(p) for p in params]},
                                     timeout=(3, 8), allow_redirects=False)
        if response.is_redirect:
            raise RuntimeError("Unexpected D1 redirect")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Invalid D1 response")
        if not response.ok or body.get("success") is not True:
            errors = body.get("errors", [])
            if (response.status_code in (200, 400) and isinstance(errors, list)
                    and any(isinstance(error, dict) and f"no such table: {TABLE}" in str(error.get("message"))
                            for error in errors)):
                raise MissingCheckpoint("D1 checkpoint table missing")
            raise RuntimeError("D1 query failed")
        results = body.get("result")
        if (not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict)
                or results[0].get("success") is not True):
            raise RuntimeError("Invalid D1 query result")
        rows = results[0].get("results")
        if not isinstance(rows, list):
            raise RuntimeError("Invalid D1 result rows")
        return rows

    def encode(self, state):
        validate_state(state, self.account, self.world_id)
        data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()
        if len(data) > MAX_BYTES:
            raise ValueError("Checkpoint exceeds raw size limit")
        payload = self.cipher.encrypt(zlib.compress(data))
        if len(payload) > MAX_CHECKPOINT:
            raise ValueError("Checkpoint exceeds D1 size limit")
        return payload.decode("ascii")

    def load(self):
        rows = self.query(f"SELECT state, revision FROM {TABLE} WHERE id = ?", self.checkpoint_id)
        if not rows:
            raise MissingCheckpoint("D1 checkpoint missing")
        payload = rows[0].get("state")
        if not isinstance(payload, str) or len(payload) > MAX_CHECKPOINT:
            raise ValueError("Invalid D1 checkpoint")
        decompressor = zlib.decompressobj()
        data = decompressor.decompress(self.cipher.decrypt(payload.encode("ascii")), MAX_BYTES + 1)
        if len(data) > MAX_BYTES or not decompressor.eof:
            raise ValueError("Invalid D1 checkpoint size")
        state = json.loads(data)
        validate_state(state, self.account, self.world_id)
        if rows[0].get("revision") != state["revision"]:
            raise ValueError("D1 checkpoint revision mismatch")
        for name in ("last_tick", "last_attempt"):
            if name in state and (type(state[name]) is not int or state[name] < 0):
                raise ValueError("Invalid D1 checkpoint time")
        self.loaded_revision = state["revision"]
        return state

    def initialize(self, state, *, if_missing=False):
        payload = self.encode(state)
        self.query(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, previous TEXT, revision INTEGER NOT NULL,
            lease TEXT, lease_until INTEGER NOT NULL DEFAULT 0
        )""")
        rows = self.query(f"""INSERT INTO {TABLE} (id, state, revision) VALUES (?, ?, ?)
            ON CONFLICT(id) DO NOTHING RETURNING id""", self.checkpoint_id, payload, state["revision"])
        if not rows and not if_missing:
            raise RuntimeError("Checkpoint already exists; refusing to overwrite hours")
        return bool(rows)

    def acquire(self):
        token = secrets.token_hex(24)
        now = int(time.time())
        rows = self.query(f"""UPDATE {TABLE} SET lease = ?, lease_until = ?
            WHERE id = ? AND lease_until <= ? RETURNING id""",
                          token, now + LEASE_SECONDS, self.checkpoint_id, now)
        if not rows:
            return False
        self.lease = token
        self.loaded_revision = None
        return True

    def release(self):
        if self.lease:
            self.query(f"UPDATE {TABLE} SET lease = NULL, lease_until = 0 WHERE id = ? AND lease = ?",
                       self.checkpoint_id, self.lease)
            self.lease = None
            self.loaded_revision = None

    def save(self, state):
        if self.lease is None or self.loaded_revision is None or state["revision"] != self.loaded_revision:
            raise RuntimeError("Saving requires a lease and the loaded checkpoint")
        updated = dict(state, revision=self.loaded_revision + 1)
        rows = self.query(f"""UPDATE {TABLE} SET previous = state, state = ?, revision = ?
            WHERE id = ? AND revision = ? AND lease = ? AND lease_until > ? RETURNING revision""",
                          self.encode(updated), updated["revision"], self.checkpoint_id,
                          self.loaded_revision, self.lease, int(time.time()))
        if not rows:
            raise RuntimeError("Lease or checkpoint changed; stale worker cannot write")
        self.loaded_revision = updated["revision"]
        state["revision"] = updated["revision"]

    def forget_world(self, world_id):
        if world_id == self.world_id or not world_id.startswith("wrld_"):
            raise ValueError("Cannot remove the active world's counter")
        identity = self.identity(world_id)
        rows = self.query(f"DELETE FROM {TABLE} WHERE id = ? AND lease_until <= ? RETURNING id",
                          identity, int(time.time()))
        if not rows and self.query(f"SELECT id FROM {TABLE} WHERE id = ?", identity):
            raise RuntimeError("Old counter is busy; no data removed")
        return len(rows)

    def native_release_supported(self):
        return self.query("SELECT 1 AS available") == [{"available": 1}]
