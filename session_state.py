"""Persist only authenticated, encrypted VRChat session data."""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def user_agent(contact):
    # Both jobs reuse one VRChat session and must identify as the same client.
    return "FiveUlCalendarFeed/1.0 " + contact


def session_cipher(key):
    if not key:
        raise ValueError("VRC_SESSION_KEY is required; store a Fernet key in GitHub Secrets")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise ValueError("VRC_SESSION_KEY must be a valid Fernet key") from None


def load_cookies(path, key, account):
    cipher = session_cipher(key)
    path = Path(path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(cipher.decrypt(path.read_bytes()))
    except (InvalidToken, ValueError, UnicodeError):
        print("[feed] Encrypted session invalid; signing in again")
        return {}
    if not isinstance(payload, dict) or payload.get("account") != account.strip().casefold():
        return {}
    cookies = payload.get("cookies")
    if not isinstance(cookies, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in cookies.items()
    ):
        return {}
    return cookies


def save_cookies(path, key, account, cookies):
    cipher = session_cipher(key)
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"account": account.strip().casefold(), "cookies": cookies}
    encrypted = cipher.encrypt(json.dumps(payload).encode("utf-8"))
    temporary = path.with_suffix(".tmp")
    with open(temporary, "wb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(encrypted)
    temporary.replace(path)
