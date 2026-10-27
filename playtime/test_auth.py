import copy
import unittest
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet

import fetch_events
from playtime.auth import login_client
from playtime.model import apply_sample, interrupt, new_state
from playtime.run import process_with_login
from playtime.test_playtime import CONFIG, WORLD, friend, response
from playtime.vrchat import ApiFailure


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.session = requests.Session()
        self.session.get = Mock()
        self.session.post = Mock()
        values = {"USERNAME": "test-bot", "PASSWORD": "test-password", "TOTP_SECRET": "JBSWY3DPEHPK3PXP",
                  "CONTACT": "test@example.com", "SESSION_KEY": Fernet.generate_key().decode(), "LOGIN_ACTION": "auto"}
        for name, value in values.items():
            item = patch.object(fetch_events, name, value)
            item.start()
            self.addCleanup(item.stop)
        for target in ("fetch_events.save_session", "fetch_events.log", "playtime.auth.print"):
            item = patch(target)
            item.start()
            self.addCleanup(item.stop)
        item = patch("playtime.auth.requests.Session", return_value=self.session)
        self.factory = item.start()
        self.addCleanup(item.stop)

    def test_one_password_login_one_totp_and_same_client(self):
        self.session.get.side_effect = [response({"requiresTwoFactorAuth": ["totp"]}), response({"id": "usr_bot"})]
        self.session.post.return_value = response({"verified": True})
        client = login_client(2)
        self.assertIs(client.session, self.session)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertIn("Authorization", self.session.get.call_args_list[0].kwargs["headers"])
        self.assertNotIn("headers", self.session.get.call_args_list[1].kwargs)
        self.session.post.assert_called_once()
        code = self.session.post.call_args.kwargs["json"]["code"]
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertNotIn("Authorization", client.session.headers)
        self.factory.assert_called_once()

    def test_bad_configuration_never_logs_in(self):
        for field, value in (("PASSWORD", ""), ("TOTP_SECRET", "bad-key!"), ("SESSION_KEY", "bad-key")):
            with self.subTest(field=field), patch.object(fetch_events, field, value):
                with self.assertRaises(ApiFailure) as failed:
                    login_client(2)
                self.assertEqual(failed.exception.status, "configuration_error")
        self.session.get.assert_not_called()

    def test_serverless_login_does_not_write_a_local_cookie_file(self):
        self.session.get.side_effect = [response({"requiresTwoFactorAuth": ["totp"]}), response({"id": "usr_bot"})]
        self.session.post.return_value = response({"verified": True})
        self.assertIs(login_client(2, persist=False).session, self.session)
        fetch_events.save_session.assert_not_called()

    def test_serverless_email_challenge_does_not_write_local_files(self):
        self.session.cookies.set("auth", "pending", domain="api.vrchat.cloud")
        self.session.get.return_value = response({"requiresTwoFactorAuth": ["emailOtp"]})
        with self.assertRaises(ApiFailure):
            login_client(2, persist=False)
        fetch_events.save_session.assert_not_called()

    def test_login_rate_limit_is_structured_without_retry(self):
        self.session.get.return_value = response({"error": {"message": "Too many requests"}}, 429,
                                                 {"Retry-After": "7200"})
        with self.assertRaises(ApiFailure) as failed:
            login_client(2)
        self.assertEqual(failed.exception.status, "rate_limited")
        self.assertEqual(failed.exception.retry_after, 7200)
        self.session.get.assert_called_once()
        self.session.post.assert_not_called()

    def test_new_location_confirmation_requires_manual_action(self):
        self.session.get.return_value = response({"error": {"message":
            "Logging in from too many places? Check your email for verification link"}}, 429)
        with self.assertRaises(ApiFailure) as failed:
            login_client(2)
        self.assertEqual(failed.exception.status, "login_required")
        self.assertEqual(failed.exception.reason, "email-location-verification")
        self.session.get.assert_called_once()

    def test_email_challenge_does_not_start_sampling(self):
        self.session.cookies.set("auth", "pending", domain="api.vrchat.cloud")
        self.session.get.return_value = response({"requiresTwoFactorAuth": ["emailOtp"]})
        with self.assertRaises(ApiFailure) as failed:
            login_client(2)
        self.assertEqual(failed.exception.status, "login_required")
        self.session.post.assert_not_called()

    def test_network_failure_is_sanitized(self):
        self.session.get.side_effect = requests.ConnectionError("PRIVATE_REQUEST")
        with self.assertRaises(ApiFailure) as failed:
            login_client(2)
        self.assertEqual(failed.exception.status, "network_error")
        self.assertNotIn("PRIVATE", str(vars(failed.exception)))


class FreshSessionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.state = apply_sample(new_state("bot", WORLD), [friend()], 1000, 1, 600)
        self.state["cookies"] = {"auth": "old-ip-bound-cookie"}
        self.client = Mock()
        self.client.friends.return_value = [friend()]
        self.client.accept_requests.return_value = 0
        self.client.cookies.return_value = {"auth": "new-cookie"}
        item = patch("playtime.run.login_client", return_value=self.client)
        self.login = item.start()
        self.addCleanup(item.stop)
        for target, options in (("playtime.run.print", {}), ("playtime.run.time.time", {"return_value": 1300})):
            item = patch(target, **options)
            item.start()
            self.addCleanup(item.stop)

    def test_fresh_login_samples_closes_session_and_retains_hours(self):
        before = copy.deepcopy(self.state)
        result, changed = process_with_login(self.state, CONFIG, 2)
        self.assertTrue(changed)
        self.login.assert_called_once_with(CONFIG["request_spacing_seconds"])
        self.client.verify.assert_called_once()
        self.client.logout.assert_called_once()
        self.assertEqual(result["players"]["usr_one"]["seconds"], 300)
        self.assertEqual(result["cookies"], {})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.state, before)

    def test_repeated_invocation_of_same_run_cannot_login_again(self):
        result, _ = process_with_login(self.state, CONFIG, 2)
        _, changed = process_with_login(result, CONFIG, 2, resume=True)
        self.assertFalse(changed)
        self.login.assert_called_once()

    def test_cooldown_precedes_login_even_for_manual_resume(self):
        limited = interrupt(self.state, "rate_limited", 1000, 7200)
        for resume in (False, True):
            _, changed = process_with_login(limited, CONFIG, 2, resume=resume)
            self.assertFalse(changed)
        self.login.assert_not_called()

    def test_auth_rejection_stops_scheduled_login_attempts(self):
        for status in ("login_required", "access_denied", "configuration_error", "logout_failed"):
            self.state["status"] = status
            _, changed = process_with_login(self.state, CONFIG, 2)
            self.assertFalse(changed)
        self.login.assert_not_called()
        result, _ = process_with_login(self.state, CONFIG, 2, resume=True)
        self.assertEqual(result["status"], "ok")

    def test_login_limit_is_persisted_and_does_not_credit_time(self):
        self.login.side_effect = ApiFailure("rate_limited", 7200)
        result, changed = process_with_login(self.state, CONFIG, 2)
        self.assertTrue(changed)
        self.assertEqual(result["retry_at"], 8500)
        self.assertEqual(result["players"]["usr_one"]["seconds"], 0)
        self.assertEqual(result["last_run"], 2)
        self.client.verify.assert_not_called()

    def test_sampling_limit_does_not_make_additional_logout_request(self):
        self.client.friends.side_effect = ApiFailure("rate_limited", 7200)
        result, _ = process_with_login(self.state, CONFIG, 2)
        self.assertEqual(result["status"], "rate_limited")
        self.client.logout.assert_not_called()

    def test_location_challenge_during_sampling_blocks_later_scheduled_login(self):
        self.client.friends.side_effect = ApiFailure("login_required", 7200,
                                                     reason="email-location-verification")
        result, _ = process_with_login(self.state, CONFIG, 2)
        self.assertEqual(result["status"], "login_required")
        self.assertEqual(result["retry_at"], 8500)
        self.client.logout.assert_not_called()
        self.client.accept_requests.assert_not_called()
        self.assertEqual(result["players"]["usr_one"]["seconds"], 0)
        process_with_login(result, CONFIG, 3, now=1400, resume=True)
        process_with_login(result, CONFIG, 4, now=9000)
        self.login.assert_called_once()

    def test_location_challenge_at_login_preserves_server_wait(self):
        self.login.side_effect = ApiFailure("login_required", 7200,
                                            reason="email-location-verification")
        result, _ = process_with_login(self.state, CONFIG, 2)
        self.assertEqual(result["retry_at"], 8500)
        process_with_login(result, CONFIG, 3, now=1400, resume=True)
        process_with_login(result, CONFIG, 4, now=9000)
        self.login.assert_called_once()
        self.client.verify.assert_not_called()

    def test_logout_failure_preserves_earned_time_but_blocks_more_logins(self):
        self.client.logout.side_effect = ApiFailure("network_error")
        result, _ = process_with_login(self.state, CONFIG, 2)
        self.assertEqual(result["players"]["usr_one"]["seconds"], 300)
        self.assertEqual(result["status"], "logout_failed")
        process_with_login(result, CONFIG, 3)
        self.login.assert_called_once()

    def test_logout_rate_limit_honors_server_delay(self):
        self.client.logout.side_effect = ApiFailure("rate_limited", 10000)
        result, _ = process_with_login(self.state, CONFIG, 2)
        self.assertEqual(result["retry_at"], 11300)
        self.assertEqual(result["status"], "rate_limited")

    def test_unexpected_sampling_exception_still_closes_own_session(self):
        self.client.verify.side_effect = RuntimeError("unexpected")
        with self.assertRaises(RuntimeError):
            process_with_login(self.state, CONFIG, 2)
        self.client.logout.assert_called_once()


if __name__ == "__main__":
    unittest.main()
