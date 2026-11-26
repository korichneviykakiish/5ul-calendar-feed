import copy
import unittest
from unittest.mock import Mock, patch

from playtime.calendar import advance
from playtime.model import apply_sample, new_state
from playtime.serverless import tick
from playtime.test_auto_recovery import rejected_session
from playtime.test_calendar import GROUP, NOW, REPORT
from playtime.test_serverless import CONFIG, FRIEND, MemoryStore
from playtime.vrchat import ApiFailure


def optional_rejection(source, reason="missing-credentials", **changes):
    return ApiFailure(**{
        "status": "login_required", "http_status": 401, "reason": reason,
        "operation": "accept-request" if source == "friends" else "calendar-events", **changes,
    })


class OptionalSessionVerificationTests(unittest.TestCase):
    def fixture(self):
        state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], NOW - 120, 1, 600)
        state = apply_sample(state, [FRIEND], NOW - 60, 2, 600)
        state["cookies"] = {"auth": "SYNTHETIC", "twoFactorAuth": "SYNTHETIC_2FA"}
        state["calendar"] = {"report": copy.deepcopy(REPORT)}
        client = Mock()
        client.friends.return_value = [FRIEND]
        client.accept_requests.return_value = 0
        client.cookies.return_value = copy.deepcopy(state["cookies"])
        client.call.return_value = {"results": [], "hasNext": False}
        return MemoryStore(state), client

    def run_tick(self, store, client, now, login):
        with patch("playtime.serverless.Client", return_value=client), \
                patch("playtime.serverless.login_client", login), \
                patch("playtime.calendar.groups_config", return_value=[GROUP]), \
                patch("playtime.run.time.time", return_value=now):
            return tick(store, {**CONFIG, "calendar_enabled": True, "auto_recover_session": True},
                        "test@example.org", now=now)

    def test_friend_queue_rejection_preserves_sample_and_stops_before_calendar(self):
        for reason in ("missing-credentials", "unauthorized"):
            with self.subTest(reason=reason):
                store, client = self.fixture()
                client.accept_requests.side_effect = optional_rejection("friends", reason)
                login = Mock(return_value=client)
                with patch("playtime.serverless.advance_calendar") as calendar:
                    result = self.run_tick(store, client, NOW, login)
                self.assertEqual(result["status"], "ok")
                self.assertTrue(result["sampled"])
                self.assertEqual(store.state["players"]["usr_one"]["seconds"], 120)
                self.assertTrue(store.state["players"]["usr_one"]["present"])
                self.assertEqual(store.state["last_sample"], NOW)
                self.assertEqual(store.state["last_success"], NOW)
                self.assertEqual(store.state["friend_request_status"], "session_verification_pending")
                self.assertFalse(store.state.get("session_recovery_pending", False))
                self.assertFalse(store.state.get("auto_login_blocked", False))
                calendar.assert_not_called()
                login.assert_not_called()

    def test_optional_rejection_then_healthy_verify_keeps_counting_without_login(self):
        for source in ("friends", "calendar"):
            with self.subTest(source=source):
                store, client = self.fixture()
                target = client.accept_requests if source == "friends" else client.call
                target.side_effect = optional_rejection(source)
                login = Mock(return_value=client)
                self.run_tick(store, client, NOW, login)
                self.assertEqual(store.state["status"], "ok")
                self.assertEqual(store.state["players"]["usr_one"]["seconds"], 120)
                self.assertEqual(store.state["calendar"]["report"], REPORT)
                target.side_effect = None
                client.reset_mock()
                result = self.run_tick(store, client, NOW + 60, login)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(store.state["players"]["usr_one"]["seconds"], 180)
                self.assertEqual(client.mock_calls[0][0], "verify")
                client.verify.assert_called_once()
                login.assert_not_called()

    def test_only_next_verified_session_rejection_can_authorize_one_recovery(self):
        for source in ("friends", "calendar"):
            with self.subTest(source=source):
                store, client = self.fixture()
                target = client.accept_requests if source == "friends" else client.call
                target.side_effect = optional_rejection(source)
                login = Mock(return_value=client)
                self.run_tick(store, client, NOW, login)
                login.assert_not_called()
                self.assertFalse(store.state.get("session_recovery_pending", False))
                target.side_effect = None
                client.reset_mock()
                client.verify.side_effect = rejected_session()
                result = self.run_tick(store, client, NOW + 60, login)
                self.assertEqual(result["status"], "login_required")
                self.assertTrue(store.state["session_recovery_pending"])
                self.assertFalse(store.state.get("auto_login_blocked", False))
                self.assertEqual(store.state["players"]["usr_one"]["seconds"], 120)
                client.verify.assert_called_once()
                client.friends.assert_not_called()
                client.accept_requests.assert_not_called()
                client.call.assert_not_called()
                login.assert_not_called()
                client.verify.side_effect = None
                result = self.run_tick(store, client, NOW + 120, login)
                self.assertEqual(result["status"], "ok")
                login.assert_called_once_with(CONFIG["request_spacing_seconds"], persist=False)
                self.assertEqual(store.state["auto_login_attempts"], 1)
                self.assertEqual(store.state["players"]["usr_one"]["seconds"], 120)

    def test_untrusted_verify_after_optional_rejection_still_requires_manual_recovery(self):
        for source in ("friends", "calendar"):
            with self.subTest(source=source):
                store, client = self.fixture()
                target = client.accept_requests if source == "friends" else client.call
                target.side_effect = optional_rejection(source)
                login = Mock(return_value=client)
                self.run_tick(store, client, NOW, login)
                target.side_effect = None
                client.verify.side_effect = rejected_session(session_rejected=False)
                self.run_tick(store, client, NOW + 60, login)
                self.assertTrue(store.state["auto_login_blocked"])
                self.assertFalse(store.state["session_recovery_pending"])
                client.reset_mock()
                self.run_tick(store, client, NOW + 120, login)
                client.verify.assert_not_called()
                login.assert_not_called()


class CalendarSessionVerificationTests(unittest.TestCase):
    def fixture(self):
        state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], NOW - 60, 1, 600)
        state = apply_sample(state, [FRIEND], NOW, 2, 600)
        state.update(cookies={"auth": "SYNTHETIC"}, session_recovery_pending=False,
                     auto_login_blocked=False)
        state["calendar"] = {"report": copy.deepcopy(REPORT)}
        client = Mock()
        return state, client

    def test_known_calendar_401_keeps_primary_accounting_and_cached_feed_unchanged(self):
        for reason in ("missing-credentials", "unauthorized"):
            with self.subTest(reason=reason):
                state, client = self.fixture()
                before = copy.deepcopy(state)
                client.call.side_effect = optional_rejection("calendar", reason)
                with patch("playtime.calendar.groups_config", return_value=[GROUP]):
                    result = advance(state, client, NOW)
                self.assertEqual({k: v for k, v in result.items() if k != "calendar"},
                                 {k: v for k, v in before.items() if k != "calendar"})
                self.assertEqual(result["calendar"]["report"], REPORT)
                self.assertEqual(result["calendar"]["status"], "session_verification_pending")
                client.call.assert_called_once()
                client.verify.assert_not_called()
                client.logout.assert_not_called()

    def test_security_challenges_and_server_backoff_keep_the_original_hard_stop(self):
        cases = [optional_rejection("calendar", "unclassified"),
                 optional_rejection("calendar", retry_after=120),
                 optional_rejection("calendar", "two-factor-required", http_status=200),
                 optional_rejection("calendar", "email-location-verification", http_status=429),
                 ApiFailure("access_denied", http_status=403, operation="calendar-events"),
                 ApiFailure("rate_limited", 7200, http_status=429, operation="calendar-events")]
        for failure in cases:
            with self.subTest(status=failure.status, reason=failure.reason, retry=failure.retry_after):
                state, client = self.fixture()
                client.call.side_effect = failure
                with patch("playtime.calendar.groups_config", return_value=[GROUP]):
                    result = advance(state, client, NOW)
                self.assertEqual(result["status"], failure.status)
                self.assertTrue(result["auto_login_blocked"])
                self.assertFalse(result["session_recovery_pending"])
                self.assertEqual(result["players"]["usr_one"]["seconds"], 60)
                self.assertIsNone(result["last_sample"])
                self.assertEqual(result["calendar"]["report"], REPORT)
                if failure.retry_after:
                    self.assertGreaterEqual(result["retry_at"], NOW + failure.retry_after)


if __name__ == "__main__":
    unittest.main()
