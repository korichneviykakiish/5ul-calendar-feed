import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

import fetch_events
from session_state import load_cookies, save_cookies


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state" / "cookies.enc"
        self.key = Fernet.generate_key().decode("ascii")

    def test_encrypted_round_trip(self):
        cookies = {"auth": "test-session-not-a-real-token", "twoFactorAuth": "test-2fa"}
        save_cookies(self.path, self.key, "MyBot", cookies)
        self.assertEqual(load_cookies(self.path, self.key, "mybot"), cookies)
        self.assertNotIn(b"test-session", self.path.read_bytes())
        self.assertFalse((self.path.parent / "cookies.json").exists())

    def test_another_account_cannot_reuse_session(self):
        save_cookies(self.path, self.key, "old-bot", {"auth": "test-only"})
        self.assertEqual(load_cookies(self.path, self.key, "new-bot"), {})

    def test_key_rotation_and_tampering_require_new_login(self):
        save_cookies(self.path, self.key, "bot", {"auth": "test-only"})
        self.assertEqual(load_cookies(self.path, Fernet.generate_key().decode(), "bot"), {})
        self.path.write_bytes(b"invalid-cache")
        self.assertEqual(load_cookies(self.path, self.key, "bot"), {})

    def test_missing_key_fails_even_without_cache(self):
        with self.assertRaises(ValueError):
            load_cookies(self.path, "", "bot")

    def test_legacy_plaintext_is_not_read(self):
        self.path.parent.mkdir()
        (self.path.parent / "cookies.json").write_text('{"auth":"old-secret"}')
        self.assertEqual(load_cookies(self.path, self.key, "bot"), {})

    def test_missing_secrets_fail_before_network(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(fetch_events, "load_session") as network:
            with self.assertRaises(SystemExit):
                fetch_events.main()
            network.assert_not_called()


class FeedTests(unittest.TestCase):
    def test_group_config_and_existing_feed_remain_compatible(self):
        groups = json.loads(Path("groups.json").read_text(encoding="utf-8"))["groups"]
        feed = json.loads(Path("docs/events.json").read_text(encoding="utf-8"))
        ids = [group["id"] for group in groups]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(ids), {group["id"] for group in feed["groups"]})
        self.assertLessEqual(sum(len(group["events"]) for group in feed["groups"]), 96)
        for group in feed["groups"]:
            for event in group["events"]:
                self.assertIsInstance(event["s"], int)
                self.assertIsInstance(event["e"], int)
                self.assertIsInstance(event["t"], str)

    def test_text_and_timestamp_contract(self):
        self.assertEqual(fetch_events.clean_text("abc\nxyz", 80, False), "abc xyz")
        self.assertEqual(len(fetch_events.clean_text("x" * 400, 280, True)), 280)
        self.assertEqual(fetch_events.parse_iso("2026-10-26T10:00:00.000Z").utcoffset().total_seconds(), 0)


if __name__ == "__main__":
    unittest.main()
