import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet

import fetch_events
from session_state import load_cookies, save_cookies, request_cookie_flags


class SessionTests(unittest.TestCase):
    def test_request_cookie_diagnostics_never_include_values(self):
        response = Mock(request=Mock(headers={"Cookie": "auth=SECRET; twoFactorAuth=OTHER_SECRET",
                                               "Authorization": "Basic PRIVATE"}))
        flags = request_cookie_flags(response)
        self.assertEqual(flags["auth_cookie_count"], 1)
        self.assertEqual(flags["two_factor_cookie_count"], 1)
        self.assertTrue(flags["basic_header_sent"])
        self.assertNotIn("SECRET", json.dumps(flags))
        self.assertNotIn("PRIVATE", json.dumps(flags))

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


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        for name, value in {"USERNAME": "test-bot", "PASSWORD": "test-password",
                            "TOTP_SECRET": "", "EMAIL_CODE": "123456"}.items():
            item = patch.object(fetch_events, name, value)
            item.start()
            self.addCleanup(item.stop)
        self.session = requests.Session()
        self.session.cookies.set("auth", "pending-test-cookie", domain="api.vrchat.cloud")
        self.session.get = Mock()
        self.session.post = Mock()
        item = patch.object(fetch_events, "save_session")
        self.save = item.start()
        self.addCleanup(item.stop)

    @staticmethod
    def response(body, status=200):
        response = Mock(status_code=status, headers={}, history=[])
        response.json.return_value = body
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError("test HTTP error")
        return response

    def test_request_email_preserves_challenge_without_using_code(self):
        def respond(url, **kwargs):
            if "headers" not in kwargs:
                return self.response({}, 401)
            self.session.cookies.set("auth", "new-pending-cookie", domain="api.vrchat.cloud")
            return self.response({"requiresTwoFactorAuth": ["emailOtp"]})
        self.session.get.side_effect = respond
        self.assertFalse(fetch_events.authenticate(self.session, "request-email"))
        self.save.assert_called_once_with(self.session)
        self.session.post.assert_not_called()
        self.assertIn("Authorization", self.session.get.call_args.kwargs["headers"])

    def test_verify_uses_cached_challenge_before_get_without_password_login(self):
        self.session.post.return_value = self.response({"verified": True})
        self.session.get.return_value = self.response({"id": "usr_test-bot"})
        self.assertTrue(fetch_events.authenticate(self.session, "verify-email"))
        self.session.post.assert_called_once_with(
            fetch_events.API + "/auth/twofactorauth/emailotp/verify", json={"code": "123456"}, timeout=30)
        self.session.get.assert_called_once_with(fetch_events.API + "/auth/user", timeout=30)
        self.save.assert_called_once()

    def test_invalid_email_code_never_logs_code_or_server_body(self):
        self.session.post.return_value = self.response({"verified": False})
        self.session.post.return_value.text = "private-response-content"
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            fetch_events.authenticate(self.session, "verify-email")
        self.assertNotIn("123456", output.getvalue())
        self.assertNotIn("private-response-content", output.getvalue())
        self.session.get.assert_not_called()
        self.save.assert_not_called()

    def test_verify_requires_cached_session(self):
        self.session.cookies.clear()
        with self.assertRaises(SystemExit):
            fetch_events.authenticate(self.session, "verify-email")
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_verify_rejects_malformed_codes_without_network(self):
        for code in ("", "12345", "1234567", "ABC123", "１２３４５６"):
            with self.subTest(code=code), patch.object(fetch_events, "EMAIL_CODE", code):
                with self.assertRaises(SystemExit):
                    fetch_events.authenticate(self.session, "verify-email")
        self.session.post.assert_not_called()

    def test_schedule_does_not_send_email_or_reuse_old_code(self):
        for response in (self.response({}, 401), self.response({"requiresTwoFactorAuth": ["emailOtp"]})):
            self.session.get.reset_mock()
            self.session.get.return_value = response
            with self.assertRaises(SystemExit):
                fetch_events.authenticate(self.session, "auto")
            self.session.get.assert_called_once_with(fetch_events.API + "/auth/user", timeout=30)
            self.session.post.assert_not_called()

    def test_active_session_does_not_need_totp_or_email_code(self):
        self.session.get.return_value = self.response({"id": "usr_test-bot"})
        self.assertTrue(fetch_events.authenticate(self.session, "auto"))
        self.session.post.assert_not_called()
        self.save.assert_called_once()

    def test_no_session_skips_unauthenticated_probe(self):
        self.session.cookies.clear()
        self.session.get.return_value = self.response({"id": "usr_test-bot"})
        with patch.object(fetch_events, "TOTP_SECRET", "JBSWY3DPEHPK3PXP"):
            self.assertTrue(fetch_events.authenticate(self.session, "check-login"))
        self.assertEqual(self.session.get.call_count, 2)
        self.assertIn("Authorization", self.session.get.call_args_list[0].kwargs["headers"])
        self.assertNotIn("headers", self.session.get.call_args_list[1].kwargs)
        self.save.assert_called_once()

    def test_check_login_reuses_active_session(self):
        self.session.get.return_value = self.response({"id": "usr_test-bot"})
        self.assertTrue(fetch_events.authenticate(self.session, "check-login"))
        self.session.get.assert_called_once_with(fetch_events.API + "/auth/user", timeout=30)
        self.session.post.assert_not_called()

    def test_cached_session_limit_never_starts_password_login(self):
        self.session.get.return_value = self.response(
            {"error": {"message": "Created too many sessions for that user"}}, 429)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            fetch_events.authenticate(self.session, "check-login")
        self.session.get.assert_called_once_with(fetch_events.API + "/auth/user", timeout=30)
        self.session.post.assert_not_called()
        self.save.assert_not_called()
        self.assertIn('"stage": "cached-session"', output.getvalue())
        self.assertIn('"reason": "session-limit"', output.getvalue())
        self.assertNotIn("pending-test-cookie", output.getvalue())

    def test_api_outage_does_not_start_another_login(self):
        self.session.get.return_value = self.response({}, 503)
        with self.assertRaises(requests.HTTPError):
            fetch_events.authenticate(self.session, "request-email")
        self.session.get.assert_called_once()
        self.session.post.assert_not_called()

    def test_rate_limited_login_reports_server_delay_without_retry(self):
        response = self.response({"error": {"message": "private-server-response"}}, 429)
        response.headers = {"Retry-After": "900"}
        self.session.get.return_value = response
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            fetch_events.login(self.session, "request-email")
        self.assertIn("HTTP 429", output.getvalue())
        self.assertIn("900", output.getvalue())
        self.assertNotIn("private-server-response", output.getvalue())
        self.session.get.assert_called_once()
        self.session.post.assert_not_called()

    def test_rate_limit_without_numeric_delay_does_not_invent_expiry(self):
        response = self.response({}, 429)
        response.headers = {"Retry-After": "untrusted-server-text"}
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            fetch_events.stop_if_rate_limited(response)
        self.assertIn("Срок снятия ограничения сервер не указал", output.getvalue())
        self.assertNotIn("untrusted-server-text", output.getvalue())

    def test_rate_limit_classifies_only_known_messages(self):
        cases = [
            ("Created too many sessions for that user", "session-limit"),
            ('"Created too many sessions for that user"', "session-limit"),
            ("Too many login attempts", "rate-limited"),
            ("Too many requests", "rate-limited"),
            ("private-token test-password", "unclassified-auth-rejection"),
        ]
        for message, reason in cases:
            with self.subTest(reason=reason):
                response = self.response({"error": {"message": message}}, 429)
                response.headers = {"Set-Cookie": "auth=private-token", "x-vrc-api-version": "test"}
                self.session.get.reset_mock()
                self.session.get.return_value = response
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit):
                    fetch_events.login(self.session, "request-email")
                self.assertIn('"reason": "' + reason + '"', output.getvalue())
                self.assertIn('"stage": "password-login"', output.getvalue())
                for secret in ("private-token", "test-password", "pending-test-cookie"):
                    self.assertNotIn(secret, output.getvalue())
                self.session.get.assert_called_once()
                self.session.post.assert_not_called()
                self.save.assert_not_called()

    def test_non_json_limit_is_not_assumed_to_be_account_or_ip_limit(self):
        response = self.response(None, 429)
        response.json.side_effect = ValueError("private-body")
        response.headers = {"cf-mitigated": "challenge", "Retry-After": "untrusted"}
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            fetch_events.stop_if_rate_limited(response, "password-login")
        self.assertIn('"reason": "non-json-response"', output.getvalue())
        self.assertIn('"cloudflare_challenge": true', output.getvalue())
        self.assertIn("не установлена", output.getvalue())
        self.assertNotIn("private-body", output.getvalue())
        self.assertNotIn("untrusted", output.getvalue())

    def test_login_failure_classification_never_echoes_response(self):
        self.assertEqual(fetch_events.login_failure_reason(self.response(
            {"error": {"message": "Invalid Username/Email or Password"}})), "invalid-credentials")
        self.assertEqual(fetch_events.login_failure_reason(self.response(
            {"error": {"message": '"Invalid Username/Email or Password"'}})), "invalid-credentials")
        self.assertEqual(fetch_events.login_failure_reason(self.response(
            {"error": {"message": '"Missing Credentials"'}})), "missing-credentials")
        for body in ({"error": {"message": "sensitive-response"}}, ["sensitive-response"], None):
            self.assertEqual(fetch_events.login_failure_reason(self.response(body)),
                             "unclassified-auth-rejection")
        response = self.response(None)
        response.json.side_effect = ValueError("not JSON")
        self.assertEqual(fetch_events.login_failure_reason(response), "non-json-response")

    def test_error_message_accepts_string_error_and_top_level_message(self):
        for body in ({"error": "Too many requests"}, {"message": "Too many requests"},
                     {"error": {"message": '"Too many requests"'}}):
            self.assertEqual(fetch_events.login_failure_reason(self.response(body)), "rate-limited")

    def test_location_verification_is_not_reported_as_rate_or_session_limit(self):
        message = "Logging in from too many places? Check your email for verification link"
        for encoded in (message, json.dumps(message)):
            response = self.response({"error": {"message": encoded}}, 429)
            self.assertEqual(fetch_events.login_failure_reason(response), "email-location-verification")
            self.session.get.reset_mock()
            self.session.get.return_value = response
            output = io.StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit):
                fetch_events.login(self.session, "request-email")
            self.assertIn("ссылкой из письма", output.getvalue())
            self.assertNotIn("Точная причина не распознана", output.getvalue())
            self.session.get.assert_called_once()
            self.session.post.assert_not_called()
            self.save.assert_not_called()

    def test_manual_error_detail_is_sanitized_and_opt_in(self):
        message = ("Please wait before logging in again. test-bot test-password 123456 "
                   "someone@example.org 203.0.113.1 usr_12345678-1234-1234-1234-123456789abc "
                   "private-cookie new-private-cookie Bearer private-authorization "
                   "https://example.org/private\nnext line")
        response = self.response({"error": {"message": message}, "private": "private-body"}, 429)
        response.request = Mock(headers={"Cookie": "auth=private-cookie",
                                         "Authorization": "Bearer private-authorization"})
        response.cookies = requests.cookies.RequestsCookieJar()
        response.cookies.set("auth", "new-private-cookie")
        for action in ("auto", "check-login"):
            output = io.StringIO()
            with patch.object(fetch_events, "LOGIN_ACTION", action), redirect_stdout(output):
                fetch_events.log_auth_diagnostic(response, "password-login")
            data = output.getvalue()
            for private in ("test-bot", "test-password", "123456", "someone@example.org",
                            "203.0.113.1", "usr_", "private-cookie", "new-private-cookie",
                            "private-authorization", "https://example.org", "private-body"):
                self.assertNotIn(private, data)
            self.assertEqual("error_detail" in data, action == "check-login")
            if action == "check-login":
                self.assertIn("Please wait before logging in again.", data)

    def test_manual_error_detail_redacts_secret_variants_before_truncation(self):
        password = "sensitive+password:%/@"
        totp = "JBSWY3DPEHPK3PXP"
        encoded = fetch_events.quote(password, safe="")
        basic = fetch_events.base64.b64encode(("test-bot:" + encoded).encode()).decode()
        response = self.response({"error": {"message":
            "Please wait. " + password + " " + encoded + " " + totp + " " + basic}})
        with patch.object(fetch_events, "PASSWORD", password), patch.object(fetch_events, "TOTP_SECRET", totp):
            detail = fetch_events.sanitized_auth_error(response)
        for private in (password, encoded, totp, basic):
            self.assertNotIn(private, detail)
        self.assertTrue(detail.startswith("Please wait."))

    def test_manual_error_detail_rejects_large_or_non_json_payloads(self):
        response = self.response({"error": {"message": "private-message" * 200}})
        self.assertEqual(fetch_events.sanitized_auth_error(response), "message-unavailable-or-too-long")
        response.json.side_effect = ValueError("private-body")
        self.assertEqual(fetch_events.sanitized_auth_error(response), "non-json-response")

    def test_diagnostic_does_not_expose_auth_header_or_arbitrary_response_text(self):
        response = self.response({"error": {"message": '"Missing Credentials"'}}, 401)
        response.request = Mock(headers={"Authorization": "Basic private-token"})
        response.url = fetch_events.API + "/auth/user"
        response.history = []
        response.headers = {"x-vrc-api-version": "test"}
        self.session.get.return_value = response
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit):
            fetch_events.login(self.session, "request-email")
        for secret in ("private-token", "test-password", "test-bot"):
            self.assertNotIn(secret, output.getvalue())
        self.assertIn('"expected_auth_header_sent": false', output.getvalue())
        self.assertIn("missing-credentials", output.getvalue())

    def test_pasted_newlines_are_rejected_without_password_request(self):
        for name, value in (("USERNAME", "test-bot\n"), ("USERNAME", " test-bot "),
                            ("PASSWORD", "test-password\r\n")):
            with self.subTest(field=name), patch.object(fetch_events, name, value):
                with self.assertRaises(SystemExit):
                    fetch_events.login(self.session, "request-email")
        self.session.get.assert_not_called()

    def test_special_characters_are_encoded_once_and_password_spaces_preserved(self):
        self.session.get.return_value = self.response({"id": "usr_test-bot"})
        with patch.object(fetch_events, "USERNAME", "bot@example.com"), \
                patch.object(fetch_events, "PASSWORD", " a+b:c%/@ "):
            self.assertTrue(fetch_events.login(self.session, "request-email"))
        header = self.session.get.call_args_list[0].kwargs["headers"]["Authorization"]
        decoded = fetch_events.base64.b64decode(header.removeprefix("Basic ")).decode("ascii")
        self.assertEqual(decoded, "bot%40example.com:%20a%2Bb%3Ac%25%2F%40%20")

    def test_arbitrary_success_response_is_not_an_authenticated_user(self):
        self.session.get.return_value = self.response({"success": True})
        self.assertFalse(fetch_events.session_alive(self.session))

    def test_login_cannot_save_success_without_a_reusable_session(self):
        self.session.get.side_effect = [self.response({"id": "usr_test-bot"}), self.response({}, 401)]
        with self.assertRaises(SystemExit):
            fetch_events.login(self.session, "request-email")
        self.save.assert_not_called()

    def test_login_cannot_save_an_unexpected_success_body(self):
        for body in ({"success": True}, [], {"error": {"message": "private-body"}}):
            self.session.get.return_value = self.response(body)
            with self.subTest(body=body), self.assertRaises(SystemExit):
                fetch_events.login(self.session, "request-email")
        self.save.assert_not_called()

    def test_totp_still_supported(self):
        self.session.get.side_effect = [self.response({"requiresTwoFactorAuth": ["totp"]}),
                                       self.response({"id": "usr_test-bot"})]
        self.session.post.return_value = self.response({"verified": True})
        with patch.object(fetch_events, "TOTP_SECRET", "JBSWY3DPEHPK3PXP"):
            self.assertTrue(fetch_events.login(self.session))
        self.assertTrue(self.session.post.call_args.args[0].endswith("/totp/verify"))

    def test_totp_rejection_cannot_save_an_authenticated_session(self):
        self.session.get.return_value = self.response({"requiresTwoFactorAuth": ["totp"]})
        self.session.post.return_value = self.response({"verified": False})
        with patch.object(fetch_events, "TOTP_SECRET", "JBSWY3DPEHPK3PXP"), self.assertRaises(SystemExit):
            fetch_events.login(self.session)
        self.session.get.assert_called_once()
        self.save.assert_not_called()

    def test_authentication_check_never_collects_or_publishes_calendar(self):
        env = {name: "test-only" for name in (
            "VRC_USERNAME", "VRC_PASSWORD", "VRC_CONTACT", "VRC_SESSION_KEY")}
        before = Path("docs/events.json").read_bytes()
        with patch.dict("os.environ", env, clear=True), \
                patch.object(fetch_events, "LOGIN_ACTION", "check-login"), \
                patch.object(fetch_events, "session_cipher"), \
                patch.object(fetch_events, "load_session", return_value=self.session), \
                patch.object(fetch_events, "authenticate", return_value=True) as authenticate, \
                patch.object(fetch_events, "session_alive", return_value=True) as probe, \
                patch.object(fetch_events, "collect") as collect, \
                patch.object(fetch_events, "workflow_output") as output:
            fetch_events.main()
            authenticate.assert_called_once_with(self.session, "check-login")
            probe.assert_called_once_with(self.session)
            collect.assert_not_called()
            output.assert_called_once_with("publish", "false")
        self.assertEqual(Path("docs/events.json").read_bytes(), before)

    def test_authentication_check_rejects_broken_persisted_session_without_login_retry(self):
        env = {name: "test-only" for name in (
            "VRC_USERNAME", "VRC_PASSWORD", "VRC_CONTACT", "VRC_SESSION_KEY")}
        with patch.dict("os.environ", env, clear=True), \
                patch.object(fetch_events, "LOGIN_ACTION", "check-login"), \
                patch.object(fetch_events, "session_cipher"), \
                patch.object(fetch_events, "load_session", return_value=self.session), \
                patch.object(fetch_events, "authenticate", return_value=True) as authenticate, \
                patch.object(fetch_events, "session_alive", return_value=False), \
                patch.object(fetch_events, "collect") as collect, \
                redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            fetch_events.main()
        authenticate.assert_called_once()
        collect.assert_not_called()

    def test_pending_email_does_not_collect_or_overwrite_feed(self):
        env = {name: "test-only" for name in (
            "VRC_USERNAME", "VRC_PASSWORD", "VRC_CONTACT", "VRC_SESSION_KEY")}
        before = Path("docs/events.json").read_bytes()
        with patch.dict("os.environ", env, clear=True), \
                patch.object(fetch_events, "session_cipher"), \
                patch.object(fetch_events, "load_session", return_value=self.session), \
                patch.object(fetch_events, "authenticate", return_value=False), \
                patch.object(fetch_events, "collect") as collect, \
                patch.object(fetch_events, "workflow_output") as output:
            fetch_events.main()
            collect.assert_not_called()
            output.assert_called_once_with("publish", "false")
        self.assertEqual(Path("docs/events.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
