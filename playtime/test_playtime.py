import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet

from playtime.model import apply_sample, interrupt, leaderboard, new_state, validate_state
from playtime.run import process
from playtime.storage import Store, retained_assets
from playtime.vrchat import ApiFailure, Client, retry_delay
from session_state import user_agent

WORLD = "wrld_test"
CONFIG = json.loads((Path(__file__).parent / "config.json").read_text())


def friend(uid="usr_one", location=WORLD + ":room", name="One"):
    return {"id": uid, "location": location, "displayName": name}


def response(body, status=200, headers=None):
    value = Mock(status_code=status, headers=headers or {}, history=[])
    value.json.return_value = body
    return value


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.empty = new_state("Bot", WORLD)
        self.first = apply_sample(self.empty, [friend()], 1000, 1, 600)

    def sample(self, friends=None, at=1300, run=2, source=None):
        return apply_sample(source or self.first, friends if friends is not None else [friend()], at, run, 600)

    def test_start_at_zero_no_import(self):
        self.assertEqual(self.empty["players"], {})
        self.assertEqual(self.first["players"]["usr_one"]["seconds"], 0)

    def test_two_samples_credit_actual_elapsed_not_fixed_five_minutes(self):
        result = self.sample(at=1324)
        self.assertEqual(result["players"]["usr_one"]["seconds"], 324)

    def test_duplicate_and_older_runs_are_idempotent(self):
        result = self.sample()
        self.assertEqual(self.sample(at=1600, source=result, run=2), result)
        self.assertEqual(self.sample(at=1600, source=result, run=1), result)

    def test_no_mutation_of_input(self):
        before = copy.deepcopy(self.first)
        self.sample()
        self.assertEqual(self.first, before)

    def test_clock_reversal_rejected_and_long_gap_does_not_credit(self):
        for timestamp in (999, 1000):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                self.sample(at=timestamp)
        for timestamp in (1601, 100000):
            with self.subTest(timestamp=timestamp):
                result = self.sample(at=timestamp)
                self.assertEqual(result["players"]["usr_one"]["seconds"], 0)

    def test_world_match_is_exact(self):
        result = self.sample([friend(location=WORLD + "-other:room")])
        self.assertEqual(result["players"]["usr_one"]["seconds"], 0)
        self.assertFalse(result["players"]["usr_one"]["present"])

    def test_missing_offline_private_and_traveling_do_not_credit(self):
        for location in (None, "", "offline", "private", "traveling", "traveling:traveling"):
            with self.subTest(location=location):
                result = self.sample([friend(location=location)])
                self.assertEqual(result["players"]["usr_one"]["seconds"], 0)
                self.assertFalse(result["players"]["usr_one"]["present"])

    def test_absence_breaks_continuity(self):
        absent = self.sample([])
        returned = self.sample(at=1600, run=3, source=absent)
        self.assertEqual(returned["players"]["usr_one"]["seconds"], 0)

    def test_renaming_retains_hours_and_same_names_stay_separate(self):
        result = self.sample([friend(name="Renamed"), friend("usr_two", name="Renamed")])
        self.assertEqual(result["players"]["usr_one"]["seconds"], 300)
        self.assertEqual(result["players"]["usr_two"]["seconds"], 0)

    def test_different_instance_of_same_world_is_eligible(self):
        self.assertEqual(self.sample([friend(location=WORLD + ":other")])["players"]["usr_one"]["seconds"], 300)

    def test_other_world_locations_not_stored(self):
        result = self.sample([friend("usr_other", "wrld_private:secret-room")])
        self.assertNotIn("usr_other", result["players"])
        self.assertNotIn("secret-room", json.dumps(result))

    def test_invalid_or_duplicate_snapshot_rejected(self):
        for rows in ([friend(), friend()], [{"id": "usr_one"}], [friend(location={})]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.sample(rows)

    def test_failure_preserves_hours_but_resets_continuity(self):
        counted = self.sample()
        failed = interrupt(counted, "network_error", 1400)
        returned = self.sample(at=1500, run=3, source=failed)
        self.assertEqual(returned["players"]["usr_one"]["seconds"], 300)

    def test_backoff_respects_server_and_grows_between_runs(self):
        failed = interrupt(self.first, "rate_limited", 1000, 4000)
        self.assertEqual(failed["retry_at"], 5000)
        failed = interrupt(failed, "rate_limited", 6000)
        self.assertEqual(failed["retry_at"], 13200)

    def test_report_matches_unity_and_hides_private_fields(self):
        state = self.sample([friend(name="<b>Someone</b>\n")])
        state["cookies"] = {"auth": "SECRET"}
        report = leaderboard(state)
        self.assertEqual(report["top"][0]["seconds"], 300)
        self.assertEqual(report["top"][0]["rank"], 1)
        for forbidden in ("SECRET", "usr_one", "<", "room", "cookies"):
            self.assertNotIn(forbidden, json.dumps(report))

    def test_rank_order_and_limit_are_deterministic(self):
        state = self.sample([friend("usr_z"), friend("usr_a")])
        state = self.sample([friend("usr_z"), friend("usr_a")], 1600, 3, state)
        self.assertEqual(len(leaderboard(state, 1)["top"]), 1)
        self.assertEqual(leaderboard(self.empty)["top"], [])

    def test_corrupt_and_mismatched_states_fail_closed(self):
        self.assertEqual(validate_state(self.first, "BOT", WORLD), self.first)
        for account, world in (("other", WORLD), ("Bot", "other")):
            with self.assertRaises(ValueError):
                validate_state(self.first, account, world)
        bad = copy.deepcopy(self.first)
        bad["players"]["usr_one"]["seconds"] = -1
        with self.assertRaises(ValueError):
            validate_state(bad, "Bot", WORLD)


class ApiTests(unittest.TestCase):
    def setUp(self):
        silence = patch("playtime.vrchat.print")
        silence.start()
        self.addCleanup(silence.stop)
        self.session = requests.Session()
        self.session.request = Mock()
        self.sleep = Mock()
        self.client = Client({"auth": "example"}, "test@example.com", session=self.session, sleep=self.sleep)

    def test_pages_are_complete_and_bounded(self):
        page = [friend("usr_" + str(i)) for i in range(100)]
        self.session.request.side_effect = [response(page), response([friend("usr_last")])]
        self.assertEqual(len(self.client.friends(2, 120)), 101)
        self.assertEqual(self.session.request.call_args.kwargs["params"]["offset"], 100)
        self.sleep.assert_called_once_with(2)

    def test_exact_page_size_requires_final_empty_page(self):
        page = [friend("usr_" + str(i)) for i in range(100)]
        self.session.request.side_effect = [response(page), response([])]
        self.assertEqual(len(self.client.friends(2, 120)), 100)

    def test_partial_pagination_never_counts(self):
        page = [friend("usr_" + str(i)) for i in range(100)]
        self.session.request.side_effect = [response(page), response({}, 503)]
        with self.assertRaises(ApiFailure):
            self.client.friends(2, 120)

    def test_duplicate_page_rejected(self):
        page = [friend("usr_" + str(i)) for i in range(100)]
        self.session.request.side_effect = [response(page), response([page[0]])]
        with self.assertRaises(ApiFailure):
            self.client.friends(2, 120)

    def test_page_limit_and_duration_limit_fail_closed(self):
        page = [friend("usr_" + str(i)) for i in range(100)]
        self.session.request.return_value = response(page)
        with self.assertRaises(ApiFailure):
            self.client.friends(1, 120)
        self.session.request.return_value = response([])
        with patch("playtime.vrchat.time.monotonic", side_effect=[0, 121]), self.assertRaises(ApiFailure):
            self.client.friends(1, 120)

    def test_auth_challenge_is_not_authenticated(self):
        self.session.request.return_value = response({"requiresTwoFactorAuth": ["emailOtp"]})
        with self.assertRaises(ApiFailure) as failure:
            self.client.verify()
        self.assertEqual(failure.exception.operation, "verify-session")
        self.assertEqual(failure.exception.reason, "two-factor-required")
        self.assertEqual(failure.exception.http_status, 200)

    def test_shared_login_client_identity(self):
        self.assertEqual(self.session.headers["User-Agent"], user_agent("test@example.com"))
        self.assertEqual(user_agent("test@example.com"), "FiveUlCalendarFeed/1.0 test@example.com")

    def test_restored_cookies_are_attached_to_actual_request(self):
        client = Client({"auth": "example", "twoFactorAuth": "second"}, "test@example.com")
        request = client.session.prepare_request(requests.Request("GET", "https://api.vrchat.cloud/api/1/auth/user"))
        self.assertEqual(request.headers["Cookie"], "auth=example; twoFactorAuth=second")
        self.assertNotIn("Authorization", request.headers)

    def test_logout_ends_only_current_session(self):
        self.session.request.return_value = response({"success": {"status_code": 200}})
        self.client.logout()
        self.assertEqual(self.session.request.call_args.args, ("PUT", "https://api.vrchat.cloud/api/1/logout"))
        self.assertEqual(list(self.session.cookies), [])

    def test_verify_requires_valid_user_id(self):
        for body in ({}, {"id": "other"}, {"id": None}, []):
            with self.subTest(body=body):
                self.session.request.return_value = response(body)
                with self.assertRaises(ApiFailure) as failure:
                    self.client.verify()
                self.assertEqual(failure.exception.reason, "missing-user-id")

    def test_auth_failure_reports_operation_without_response_contents(self):
        for operation, invoke in (
            ("verify-session", self.client.verify),
            ("list-friends", lambda: self.client.friends(2, 120)),
            ("list-requests", lambda: self.client.accept_requests(20)),
        ):
            with self.subTest(operation=operation):
                self.session.request.return_value = response({"error": {"message": "private-server-text"}}, 401)
                with self.assertRaises(ApiFailure) as failure:
                    invoke()
                self.assertEqual(failure.exception.operation, operation)
                self.assertEqual(failure.exception.http_status, 401)
                self.assertEqual(failure.exception.reason, "unclassified")
                self.assertNotIn("private-server-text", str(vars(failure.exception)))

    def test_known_auth_reason_is_classified(self):
        self.session.request.return_value = response({"error": {"message": '"Missing Credentials"'}}, 401)
        with self.assertRaises(ApiFailure) as failure:
            self.client.verify()
        self.assertEqual(failure.exception.reason, "missing-credentials")

    def test_no_password_login_or_automatic_retry(self):
        self.session.request.return_value = response({}, 429, {"Retry-After": "7200"})
        with self.assertRaises(ApiFailure) as result:
            self.client.verify()
        self.assertEqual(result.exception.retry_after, 7200)
        self.session.request.assert_called_once()
        self.assertNotIn("Authorization", self.session.headers)
        self.assertFalse(self.session.request.call_args.kwargs["allow_redirects"])

    def test_http_failures_are_sanitized(self):
        for status, expected in ((401, "login_required"), (403, "access_denied"), (500, "api_error")):
            self.session.request.return_value = response({"secret": "never print me"}, status)
            with self.assertRaises(ApiFailure) as failure:
                self.client.verify()
            self.assertEqual(str(failure.exception), expected)

    def test_network_error_does_not_leak_request(self):
        self.session.request.side_effect = requests.ConnectionError("secret cookie URL")
        with self.assertRaises(ApiFailure) as failure:
            self.client.verify()
        self.assertEqual(str(failure.exception), "network_error")

    def test_only_incoming_friend_requests_are_accepted(self):
        self.session.request.side_effect = [response([
            {"type": "invite", "id": "not_other"},
            {"type": "friendRequest", "id": "frq_example"},
        ]), response({"success": {}})]
        self.assertEqual(self.client.accept_requests(20), 1)
        self.assertEqual(self.session.request.call_args.args[0], "PUT")
        self.assertTrue(self.session.request.call_args.args[1].endswith("/frq_example/accept"))

    def test_notification_path_injection_rejected(self):
        self.session.request.return_value = response([{"type": "friendRequest", "id": "frq_a/../other"}])
        with self.assertRaises(ApiFailure):
            self.client.accept_requests(20)
        self.assertEqual(self.session.request.call_count, 1)

    def test_retry_after(self):
        self.assertEqual(retry_delay("3600"), 3600)
        self.assertEqual(retry_delay("invalid"), 0)
        self.assertEqual(retry_delay("Mon, 01 Jan 2001 00:00:00 GMT"), 0)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        silence = patch("playtime.run.print")
        silence.start()
        self.addCleanup(silence.stop)
        self.state = apply_sample(new_state("bot", WORLD), [friend()], 1000, 1, 600)
        self.client = Mock()
        self.client.friends.return_value = [friend()]
        self.client.accept_requests.return_value = 0
        self.client.cookies.return_value = {"auth": "example"}

    def test_no_calls_during_backoff_or_duplicate_run(self):
        limited = interrupt(self.state, "rate_limited", 1000)
        _, changed = process(limited, self.client, CONFIG, 2, now=1300)
        self.assertFalse(changed)
        process(self.state, self.client, CONFIG, 1, now=1300)
        self.client.verify.assert_not_called()

    def test_success_and_friend_request_failure_preserve_credited_time(self):
        self.client.accept_requests.side_effect = ApiFailure("rate_limited")
        with patch("playtime.run.time.time", return_value=1300):
            result, changed = process(self.state, self.client, CONFIG, 2)
        self.assertTrue(changed)
        self.assertEqual(result["players"]["usr_one"]["seconds"], 300)
        self.assertEqual(result["status"], "rate_limited")
        self.assertEqual(result["last_run"], 2)

    def test_error_does_not_publish_fabricated_time(self):
        self.client.friends.side_effect = ApiFailure("network_error")
        with patch("playtime.run.time.time", return_value=1300):
            result, _ = process(self.state, self.client, CONFIG, 2)
        self.assertEqual(result["players"]["usr_one"]["seconds"], 0)
        self.assertIsNone(result["last_sample"])
        self.client.accept_requests.assert_not_called()

    def test_auth_diagnostic_contains_only_status_and_cookie_presence(self):
        self.client.verify.side_effect = ApiFailure("login_required", operation="verify-session",
                                                   http_status=401, reason="unauthorized")
        self.client.cookies.return_value = {"auth": "SECRET_COOKIE"}
        with patch("playtime.run.print") as output, patch("playtime.run.time.time", return_value=1300):
            result, _ = process(self.state, self.client, CONFIG, 2)
        message = output.call_args.args[0]
        diagnostic = json.loads(message.removeprefix("[playtime] Sampling paused: "))
        self.assertEqual(diagnostic["operation"], "verify-session")
        self.assertTrue(diagnostic["auth_cookie_present"])
        self.assertFalse(diagnostic["two_factor_cookie_present"])
        self.assertNotIn("SECRET_COOKIE", message)
        self.assertEqual(result["status"], "login_required")
        self.client.friends.assert_not_called()


class StorageTests(unittest.TestCase):
    def test_retention_bounds_two_recent_plus_seven_days(self):
        assets = [{"id": day * 10 + run, "name": "state-%012d-202610%02d.enc" % (day * 10 + run, day)}
                  for day in range(1, 27) for run in range(3)]
        retained = retained_assets(assets)
        self.assertIn(262, retained)
        self.assertIn(261, retained)
        self.assertEqual(len(retained), 8)
        self.assertNotIn(12, retained)

    def test_encryption_hides_identity_and_session(self):
        key = Fernet.generate_key().decode()
        store = Store("owner/repo", "TOKEN", key, "bot", WORLD)
        state = new_state("bot", WORLD)
        state["cookies"] = {"auth": "SECRET"}
        encrypted = store.cipher.encrypt(json.dumps(state).encode())
        self.assertNotIn(b"SECRET", encrypted)
        self.assertNotIn(b'"account"', encrypted)
        self.assertEqual(json.loads(store.cipher.decrypt(encrypted)), state)

    def test_existing_empty_store_is_not_silently_reset(self):
        store = Store("owner/repo", "TOKEN", Fernet.generate_key().decode(), "bot", WORLD)
        store.request = Mock(return_value={"id": 1})
        store.assets = Mock(return_value=[])
        with self.assertRaises(RuntimeError):
            store.load()

    def test_missing_established_store_cannot_reset_totals(self):
        store = Store("owner/repo", "TOKEN", Fernet.generate_key().decode(), "bot", WORLD)
        store.request = Mock(return_value=None)
        with self.assertRaises(RuntimeError):
            store.load()
        store.request.assert_called_once()

    def test_new_counter_can_be_initialized_explicitly(self):
        store = Store("owner/repo", "TOKEN", Fernet.generate_key().decode(), "bot", WORLD, allow_create=True)
        store.request = Mock(side_effect=[None, {"id": 1}])
        self.assertEqual(store.load()["players"], {})

    def test_upload_failure_does_not_delete_previous(self):
        store = Store("owner/repo", "TOKEN", Fernet.generate_key().decode(), "bot", WORLD)
        store.release = {"id": 1, "upload_url": "https://uploads.github.com/repos/owner/repo/releases/1/assets{?name}"}
        store.session.post = Mock(return_value=response({}, 503))
        store.request = Mock()
        with self.assertRaises(RuntimeError):
            store.save(new_state("bot", WORLD))
        store.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
