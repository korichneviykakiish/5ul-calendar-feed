"""Encrypted durable state with a fenced, expiring lease for Vercel invocations."""

import copy
import hashlib
import json
import secrets
from urllib.parse import urlsplit

import requests
from cryptography.fernet import Fernet

from .model import validate_state

LEASE_SECONDS = 330  # Longer than Vercel's configured 300-second hard limit.
MAX_BYTES = 4 * 1024 * 1024
SAVE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[3], ARGV[2])
redis.call('SET', KEYS[2], ARGV[3])
return 1
"""
RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""


class RedisStore:
    def __init__(self, url, token, key, account, world_id, session=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".upstash.io")
                or parsed.username or parsed.password or parsed.port
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("Use the HTTPS Upstash REST endpoint")
        if not token or not account.strip():
            raise ValueError("Missing storage configuration")
        self.url = url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": "Bearer " + token})
        self.cipher = Fernet(key.encode("ascii"))
        self.account, self.world_id = account, world_id
        identity = hashlib.sha256((account.strip().casefold() + "\n" + world_id).encode()).hexdigest()[:24]
        self.prefix = "5ul:playtime:" + identity
        self.lease = None
        self.loaded = None

    def command(self, *parts):
        response = self.session.post(self.url, json=list(parts), timeout=(3, 5), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("State service unavailable")
        body = response.json()
        if not isinstance(body, dict) or "error" in body or "result" not in body:
            raise RuntimeError("State service rejected command")
        return body["result"]

    def acquire(self):
        token = secrets.token_hex(24)
        if self.command("SET", self.prefix + ":lock", token, "NX", "EX", LEASE_SECONDS) != "OK":
            return False
        self.lease = token
        return True

    def release(self):
        if self.lease:
            self.command("EVAL", RELEASE, 1, self.prefix + ":lock", self.lease)
            self.lease = None

    def encode(self, state):
        validate_state(state, self.account, self.world_id)
        payload = self.cipher.encrypt(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode())
        if len(payload) > MAX_BYTES:
            raise ValueError("State exceeds serverless storage limit")
        return payload.decode("ascii")

    def load(self):
        payload = self.command("GET", self.prefix + ":state")
        if not isinstance(payload, str) or len(payload) > MAX_BYTES:
            raise RuntimeError("Checkpoint missing; explicitly initialize the new counter before enabling")
        state = json.loads(self.cipher.decrypt(payload.encode("ascii")))
        validate_state(state, self.account, self.world_id)
        for name in ("last_tick", "last_attempt"):
            if name in state and (type(state[name]) is not int or state[name] < 0):
                raise ValueError("Invalid serverless checkpoint")
        self.loaded = payload
        return state

    def initialize(self, state):
        if self.command("SET", self.prefix + ":state", self.encode(state), "NX") != "OK":
            raise RuntimeError("Checkpoint already exists; refusing to overwrite hours")

    def save(self, state):
        if not self.lease or not self.loaded:
            raise RuntimeError("Saving requires a lease and an existing checkpoint")
        updated = copy.deepcopy(state)
        updated["revision"] += 1
        payload = self.encode(updated)
        saved = self.command("EVAL", SAVE, 3, self.prefix + ":lock", self.prefix + ":state",
                             self.prefix + ":previous", self.lease, self.loaded, payload)
        if saved != 1:
            raise RuntimeError("Lease or checkpoint changed; stale worker cannot write")
        self.loaded = payload
        state["revision"] = updated["revision"]
