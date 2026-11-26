import json
import sqlite3
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from requests import Response

from .d1_store import D1Store
from .model import apply_sample, leaderboard
from .serverless import REQUESTED_WORLD_ID, prepare_public_recovery, tick
from .vrchat import ApiFailure


class SQLiteSession:
    def __init__(self, connection):
        self.headers = {}
        self.connection = connection

    def post(self, _url, **kwargs):
        response = Response()
        query = kwargs["json"]
        try:
            cursor = self.connection.execute(query["sql"], query["params"])
            rows = [dict(row) for row in cursor.fetchall()]
        except sqlite3.OperationalError as error:
            response.status_code = 400
            body = {"success": False, "errors": [{"message": str(error)}]}
        else:
            response.status_code = 200
            body = {"success": True, "result": [{"success": True, "results": rows}]}
        response._content = json.dumps(body).encode()
        return response


class D1RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.key = Fernet.generate_key().decode()
        self.first = self.store()
        self.second = self.store()
        self.config = {"world_id": REQUESTED_WORLD_ID, "request_spacing_seconds": 2}

    def store(self):
        return D1Store("a" * 32, "00000000-0000-0000-0000-000000000001", "unused",
                       self.key, "bot", REQUESTED_WORLD_ID, SQLiteSession(self.db))

    def seed(self):
        playtime = {"estimated": True, "status": "ok", "updated": "2026-11-24 22:09 UTC",
                    "top": [{"rank": i, "name": f"Player {i}", "seconds": 200 - i} for i in range(1, 111)]}
        calendar = {"gen": 1, "groups": [{"id": "grp_00000000-0000-0000-0000-000000000001",
                                        "name": "Test", "events": []}]}
        with patch.dict("os.environ", {"PLAYTIME_D1_RESTORE_SNAPSHOT": "true"}):
            with patch("playtime.public_snapshot.fetch_json", side_effect=[playtime, calendar]):
                self.assertTrue(prepare_public_recovery(self.first, self.config, "sample"))
            with patch("playtime.public_snapshot.fetch_json") as fetch:
                self.assertTrue(prepare_public_recovery(self.first, self.config, "sample"))
                fetch.assert_not_called()
        return playtime

    def test_import_preserves_report_and_matches_exact_names_once(self):
        report = self.seed()
        state = self.first.load()
        self.assertEqual(leaderboard(state)["top"], report["top"])
        self.assertEqual(leaderboard(state)["updated"], report["updated"])
        friends = [{"id": "usr_1", "displayName": "Player 1", "location": REQUESTED_WORLD_ID},
                   {"id": "usr_2", "displayName": "Player 2", "location": REQUESTED_WORLD_ID},
                   {"id": "usr_3", "displayName": "Player 2", "location": None},
                   {"id": "usr_4", "displayName": "player 3", "location": REQUESTED_WORLD_ID}]
        sampled = state["last_success"] + 600
        state = apply_sample(state, friends, sampled, 1, 600)
        self.assertEqual(state["players"]["usr_1"]["seconds"], 199)
        self.assertNotIn("imported:001", state["players"])
        self.assertIn("imported:002", state["players"])
        self.assertIn("imported:003", state["players"])
        state = apply_sample(state, friends, sampled + 60, 2, 600)
        self.assertEqual(state["players"]["usr_1"]["seconds"], 259)

    def test_checkpoints_are_encrypted_and_expired_writer_cannot_overwrite(self):
        report = self.seed()
        with patch("playtime.d1_store.time.time", return_value=1000):
            self.assertTrue(self.first.acquire())
            original = self.first.load()
            self.assertFalse(self.second.acquire())
        with patch("playtime.d1_store.time.time", return_value=1331):
            self.assertTrue(self.second.acquire())
            current = self.second.load()
            current["status"] = "ready"
            self.second.save(current)
            ciphertext = self.db.execute("SELECT state, previous FROM playtime_checkpoints").fetchone()
            self.assertNotIn(report["top"][0]["name"], ciphertext["state"])
            self.assertIsNotNone(ciphertext["previous"])
            with self.assertRaises(RuntimeError):
                self.first.save(original)
            self.first.release()
            self.assertFalse(self.first.acquire())
            self.second.release()
            self.assertEqual(self.first.load()["status"], "ready")

    def test_recovery_login_attempt_is_consumed_on_failure(self):
        self.seed()
        now = self.first.load()["last_success"] + 86400
        with patch("playtime.serverless.login_client", side_effect=ApiFailure("login_required")) as login:
            result = tick(self.first, self.config, "test", now=now, allow_recovery_login=True)
            self.assertEqual(result["status"], "login_required")
            result = tick(self.first, self.config, "test", now=now + 61, allow_recovery_login=True)
            self.assertFalse(result["sampled"])
            self.assertEqual(login.call_count, 1)
        self.assertEqual(len(leaderboard(self.first.load())["top"]), 110)
