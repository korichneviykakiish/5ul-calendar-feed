import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import requests

from playtime.model import apply_sample, new_state, validate_state
from playtime.serverless import AUTO_LOGIN_COOLDOWN, handle, public_report, recovery_status, tick
from playtime.test_serverless import ADMIN, CRON, ENV, CONFIG, FRIEND, MemoryStore
from playtime.vrchat import ApiFailure, Client


def rejected_session(**changes):
    return ApiFailure(**{"status": "login_required", "operation": "verify-session", "http_status": 401,
                         "reason": "missing-credentials", "session_rejected": True, **changes})


class RejectionEvidenceTests(unittest.TestCase):
    def verify_failure(self, status=401, message="Missing Credentials", cookies=None, headers=None,
                       operation="verify-session", body=None):
        session = requests.Session()
        session.headers.update(headers or {})
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body if body is not None else {"error": {"message": message}}).encode()

        def respond(request, **kwargs):
            response.request = request
            return response

        session.send = Mock(side_effect=respond)
        client = Client({"auth": "PRIVATE_COOKIE"} if cookies is None else cookies,
                        "test@example.org", session=session, sleep=Mock())
        with self.assertRaises(ApiFailure) as failure, redirect_stdout(io.StringIO()):
            if operation == "verify-session":
                client.verify()
            else:
                client.call("GET", "/auth/user/friends", operation=operation)
        session.send.assert_called_once()
        return failure.exception

    def test_only_known_401_with_a_single_sent_auth_cookie_is_eligible(self):
        for message in ("Missing Credentials", "Unauthorized"):
            with self.subTest(message=message):
                self.assertTrue(self.verify_failure(message=message).session_rejected)

    def test_missing_or_duplicate_cookies_and_basic_login_are_not_eligible(self):
        for settings in ({"cookies": {}}, {"headers": {"Cookie": "auth=one; auth=two"}},
                         {"headers": {"Authorization": "Basic PRIVATE"}}):
            with self.subTest(settings=settings):
                self.assertFalse(self.verify_failure(**settings).session_rejected)

    def test_security_challenges_invalid_credentials_and_unknown_messages_are_not_eligible(self):
        for message in ("Invalid Credentials", "Other rejection",
                        "Logging in from too many places? Check your email for verification link"):
            with self.subTest(message=message):
                self.assertFalse(self.verify_failure(message=message).session_rejected)

    def test_forbidden_rate_limit_and_later_api_401_never_enable_recovery(self):
        for settings in ({"status": 403}, {"status": 429}, {"operation": "list-friends"}):
            with self.subTest(settings=settings):
                self.assertFalse(self.verify_failure(**settings).session_rejected)

    def test_partial_two_factor_session_is_not_treated_as_an_expired_session(self):
        self.assertFalse(self.verify_failure(status=200, body={"requiresTwoFactorAuth": ["totp"]}).session_rejected)


class AutomaticRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.config = {**CONFIG, "auto_recover_session": True}
        state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], 1200, 1, 600)
        state["players"]["usr_one"]["seconds"] = 600
        state["cookies"] = {"auth": "PRIVATE_COOKIE"}
        self.store = MemoryStore(state)
        self.client = Mock()
        self.client.friends.return_value = [FRIEND]
        self.client.accept_requests.return_value = 0
        self.client.cookies.return_value = {"auth": "PRIVATE_COOKIE"}
        self.login = Mock(return_value=self.client)
        silence = patch("builtins.print")
        silence.start()
        self.addCleanup(silence.stop)

    def tick(self, now, resume=False):
        with patch("playtime.serverless.Client", return_value=self.client), \
                patch("playtime.serverless.login_client", self.login), \
                patch("playtime.run.time.time", return_value=now):
            return tick(self.store, self.config, "test@example.org", resume=resume, now=now)

    def reject(self, now=1260, error=None):
        self.client.verify.side_effect = error or rejected_session()
        self.assertEqual(self.tick(now)["status"], "login_required")
        self.client.verify.side_effect = None

    def test_recovery_is_deferred_to_next_tick_and_uses_one_login_then_reuses_cookie(self):
        self.reject()
        self.login.assert_not_called()
        self.assertTrue(self.store.state["session_recovery_pending"])
        self.assertTrue(self.tick(1320)["sampled"])
        self.login.assert_called_once_with(2.0, persist=False)
        self.assertFalse(self.store.state["auto_login_blocked"])
        self.assertEqual(self.store.state["auto_login_attempts"], 1)
        self.assertEqual(self.store.state["last_login_method"], "automatic")
        self.assertEqual(self.tick(1380)["status"], "ok")
        self.login.assert_called_once()
        self.assertEqual(self.store.state["players"]["usr_one"]["seconds"], 660)

    def test_automatic_attempt_is_persisted_before_credentials_are_used(self):
        self.reject()

        def login(*args, **kwargs):
            self.assertTrue(self.store.state["auto_login_blocked"])
            self.assertFalse(self.store.state["session_recovery_pending"])
            self.assertEqual(self.store.state["last_login_attempt_at"], 1320)
            self.assertEqual(self.store.state["auto_login_attempts"], 1)
            return self.client

        self.login.side_effect = login
        self.tick(1320)

    def test_crash_cannot_start_another_login_even_after_cooldown(self):
        self.reject()
        self.login.side_effect = RuntimeError("process stopped")
        with self.assertRaises(RuntimeError):
            self.tick(1320)
        self.login.reset_mock()
        self.tick(1320 + AUTO_LOGIN_COOLDOWN + 60)
        self.login.assert_not_called()
        self.assertTrue(self.store.state["auto_login_blocked"])
        self.assertEqual(self.store.state["auto_login_attempts"], 1)

    def test_failed_auto_login_stops_without_later_automatic_retries_or_lost_hours(self):
        pending = copy.deepcopy(self.store.state)
        for status in ("login_required", "access_denied", "rate_limited", "network_error", "configuration_error"):
            with self.subTest(status=status):
                self.store.state = copy.deepcopy(pending)
                self.reject()
                self.login.reset_mock()
                self.login.side_effect = ApiFailure(status)
                self.tick(1320)
                self.assertEqual(self.login.call_count, 1)
                self.assertEqual(self.store.state["cookies"], {})
                self.assertEqual(self.store.state["players"]["usr_one"]["seconds"], 600)
                self.login.reset_mock()
                self.tick(100000)
                self.login.assert_not_called()

    def test_rejected_new_session_does_not_start_a_login_loop(self):
        self.reject()
        self.client.verify.side_effect = rejected_session()
        self.tick(1320)
        self.assertTrue(self.store.state["auto_login_blocked"])
        self.assertFalse(self.store.state["session_recovery_pending"])
        self.login.reset_mock()
        self.tick(100000)
        self.login.assert_not_called()

    def test_successful_auto_login_has_a_persistent_six_hour_limit(self):
        self.reject()
        self.tick(1320)
        self.reject(1380)
        self.login.reset_mock()
        self.assertEqual(self.tick(1320 + AUTO_LOGIN_COOLDOWN - 1)["retry_at"], 1320 + AUTO_LOGIN_COOLDOWN)
        self.login.assert_not_called()
        self.tick(1320 + AUTO_LOGIN_COOLDOWN)
        self.login.assert_called_once()
        self.assertEqual(self.store.state["auto_login_attempts"], 2)

    def test_manual_login_counts_toward_auto_cooldown(self):
        self.tick(1260, resume=True)
        self.reject(1320)
        self.login.reset_mock()
        self.tick(1380)
        self.login.assert_not_called()
        self.assertEqual(self.store.state["last_login_method"], "manual")

    def test_email_challenge_unknown_401_and_other_endpoints_require_manual_action(self):
        initial = copy.deepcopy(self.store.state)
        for changes in ({"reason": "email-location-verification", "session_rejected": False},
                        {"reason": "unclassified", "session_rejected": False},
                        {"operation": "list-friends"}, {"http_status": 429},
                        {"http_status": 200, "reason": "two-factor-required"}):
            with self.subTest(changes=changes):
                self.store.state = copy.deepcopy(initial)
                self.reject(error=rejected_session(**changes))
                self.tick(1320)
                self.assertFalse(self.store.state["session_recovery_pending"])
                self.login.assert_not_called()

    def test_rate_limit_is_never_followed_by_a_login_and_honors_longer_retry_after(self):
        self.client.verify.side_effect = ApiFailure("rate_limited", 7200, http_status=429)
        self.assertEqual(self.tick(1260)["status"], "rate_limited")
        self.assertEqual(self.store.state["retry_at"], 8460)
        self.tick(1320)
        self.login.assert_not_called()
        self.assertTrue(self.store.state["auto_login_blocked"])

    def test_hard_stop_latch_survives_a_later_successful_sample(self):
        self.store.state["auto_login_blocked"] = True
        self.tick(1260)
        self.reject(1320)
        self.tick(1380)
        self.login.assert_not_called()

    def test_operator_success_can_clear_a_hard_stop_without_bypassing_retry_after(self):
        self.store.state.update(auto_login_blocked=True, status="login_required", retry_at=1400)
        self.tick(1320, resume=True)
        self.login.assert_not_called()
        self.tick(1440, resume=True)
        self.login.assert_called_once()
        self.assertFalse(self.store.state["auto_login_blocked"])

    def test_disabled_feature_unknown_legacy_state_and_new_counter_never_auto_login(self):
        initial = copy.deepcopy(self.store.state)
        cases = [({"auto_recover_session": False}, {"status": "login_required", "session_recovery_pending": True}),
                 ({}, {"status": "login_required"}),
                 ({}, {"status": "login_required", "last_success": None, "session_recovery_pending": True})]
        for config, changes in cases:
            with self.subTest(config=config, changes=changes):
                self.store.state = {**copy.deepcopy(initial), **changes}
                self.config = {**CONFIG, "auto_recover_session": True, **config}
                self.tick(1260)
                self.login.assert_not_called()

    def test_duplicate_tick_and_busy_worker_cannot_send_credentials(self):
        self.reject()
        self.tick(1280)
        self.store.busy = True
        self.assertEqual(self.tick(1320)["status"], "busy")
        self.login.assert_not_called()

    def test_recovery_metadata_never_appears_in_public_report(self):
        self.reject()
        self.tick(1320)
        report = public_report(self.store.state, self.config, 1320)
        for key in ("last_login_attempt_at", "auto_login_blocked", "auto_login_attempts", "PRIVATE_COOKIE"):
            self.assertNotIn(key, json.dumps(report))


class RecoveryPolicyTests(unittest.TestCase):
    def test_invalid_recovery_state_is_rejected(self):
        original = new_state("bot", "wrld_test")
        for key, value in (("last_login_attempt_at", True), ("auto_login_attempts", -1),
                           ("auto_login_blocked", "false"), ("session_recovery_pending", 1),
                           ("last_login_method", {})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_state({**original, key: value}, "bot", "wrld_test")

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_cron_cannot_force_resume_or_read_operator_policy(self, store):
        for action in ("resume", "recovery-status"):
            self.assertEqual(handle("POST", "Bearer " + CRON, action)[0], 401)
        store.assert_not_called()

    @patch.dict("os.environ", {**ENV, "PLAYTIME_ENABLED": "false"}, clear=True)
    @patch("playtime.serverless.configured_store")
    @patch("playtime.serverless.tick")
    def test_operator_policy_read_does_not_log_in_even_while_paused(self, tick_mock, store):
        store.return_value.load.return_value = new_state("bot", "wrld_test")
        code, result = handle("POST", "Bearer " + ADMIN, "recovery-status")
        self.assertEqual(code, 200)
        self.assertEqual(result["cooldown_seconds"], 21600)
        self.assertTrue(result["manual_required"])
        tick_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
