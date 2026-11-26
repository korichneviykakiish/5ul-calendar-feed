import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from cryptography.fernet import Fernet

from playtime.model import apply_sample, interrupt, leaderboard, new_state, validate_state
from playtime.run import process, process_with_login
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

    def test_401_preserves_server_retry_after_without_inventing_a_default(self):
        for headers, expected in (({}, 0), ({"Retry-After": "invalid"}, 0),
                                  ({"Retry-After": "0"}, 0), ({"Retry-After": "120"}, 120)):
            with self.subTest(headers=headers):
                self.session.request.reset_mock()
                self.session.request.return_value = response(
                    {"error": {"message": "Unauthorized"}}, 401, headers)
                with self.assertRaises(ApiFailure) as failed:
                    self.client.call("GET", "/auth/user/notifications", operation="list-requests")
                self.assertEqual(failed.exception.status, "login_required")
                self.assertEqual(failed.exception.reason, "unauthorized")
                self.assertEqual(failed.exception.retry_after, expected)
                self.session.request.assert_called_once()

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

    def test_location_challenge_during_session_requires_manual_recovery(self):
        self.session.request.return_value = response({"error": {"message":
            "Logging in from too many places? Check your email for verification link"}}, 429,
            {"Retry-After": "7200"})
        with self.assertRaises(ApiFailure) as failure:
            self.client.verify()
        self.assertEqual(failure.exception.status, "login_required")
        self.assertEqual(failure.exception.reason, "email-location-verification")
        self.assertEqual(failure.exception.retry_after, 7200)
        self.assertEqual(failure.exception.http_status, 429)
        self.session.request.assert_called_once()

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

    def test_request_after_twenty_other_notifications_is_accepted(self):
        rows = [{"type": "invite", "id": "not_other_" + str(i)} for i in range(25)]
        rows.append({"type": "friendRequest", "id": "frq_later"})
        self.session.request.side_effect = [response(rows), response({"success": {}})]
        self.assertEqual(self.client.accept_requests(20), 1)
        params = self.session.request.call_args_list[0].kwargs["params"]
        self.assertEqual(params, {"hidden": "false", "n": 100, "offset": 0})

    def test_requests_collected_across_pages_before_acceptance(self):
        rows = [{"type": "invite", "id": "not_other_" + str(i)} for i in range(99)]
        rows.append({"type": "friendRequest", "id": "frq_first"})
        self.session.request.side_effect = [response(rows), response([
            {"type": "friendRequest", "id": "frq_second"},
        ]), response({"success": {}}), response({"success": {}})]
        self.assertEqual(self.client.accept_requests(20), 2)
        calls = self.session.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["GET", "GET", "PUT", "PUT"])
        self.assertEqual(calls[1].kwargs["params"]["offset"], 100)
        self.assertEqual(self.sleep.call_count, 3)

    def test_friend_acceptance_limit_is_not_notification_limit(self):
        rows = [{"type": "friendRequest", "id": "frq_" + str(i)} for i in range(100)]
        self.session.request.side_effect = [response(rows)] + [response({"success": {}})] * 20
        self.assertEqual(self.client.accept_requests(20), 20)
        self.assertEqual(self.session.request.call_count, 21)

    def test_duplicate_and_hidden_requests_are_not_accepted_twice(self):
        request = {"type": "friendRequest", "id": "frq_example"}
        self.session.request.side_effect = [response([
            request, request, {"type": "friendRequest", "id": "frq_hidden", "hidden": True},
        ]), response({"success": {}})]
        self.assertEqual(self.client.accept_requests(20), 1)
        self.assertEqual(self.session.request.call_count, 2)

    def test_notification_scan_is_bounded_and_logged_without_contents(self):
        rows = [{"type": "invite", "id": "not_private", "message": "PRIVATE_TEXT"}] * 100
        self.session.request.return_value = response(rows)
        with patch("playtime.vrchat.print") as output:
            self.assertEqual(self.client.accept_requests(20), 0)
        self.assertEqual(self.session.request.call_count, 5)
        self.assertEqual(self.session.request.call_args.kwargs["params"]["offset"], 400)
        message = output.call_args.args[0]
        diagnostic = json.loads(message.removeprefix("[playtime] Friend request scan: "))
        self.assertEqual(diagnostic["scanned"], 500)
        self.assertTrue(diagnostic["scan_capped"])
        self.assertNotIn("PRIVATE_TEXT", message)
        self.assertNotIn("not_private", message)

    def test_expired_request_does_not_block_remaining_requests(self):
        self.session.request.side_effect = [response([
            {"type": "friendRequest", "id": "frq_expired"},
            {"type": "friendRequest", "id": "frq_valid"},
        ]), response({}, 404), response({"success": {}})]
        with patch("playtime.vrchat.print") as output:
            self.assertEqual(self.client.accept_requests(20), 1)
        diagnostic = json.loads(output.call_args.args[0].removeprefix("[playtime] Friend request scan: "))
        self.assertEqual(diagnostic["expired"], 1)
        self.assertEqual(diagnostic["accepted"], 1)

    def test_accept_auth_and_rate_failures_stop_without_retry(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.session.request.reset_mock()
                self.session.request.side_effect = [response([
                    {"type": "friendRequest", "id": "frq_first"},
                    {"type": "friendRequest", "id": "frq_second"},
                ]), response({}, status)]
                with self.assertRaises(ApiFailure) as failure:
                    self.client.accept_requests(20)
                self.assertEqual(failure.exception.http_status, status)
                self.assertEqual(self.session.request.call_count, 2)

    def test_invalid_or_failed_later_page_does_not_accept_partial_queue(self):
        rows = [{"type": "invite"}] * 99 + [{"type": "friendRequest", "id": "frq_first"}]
        for second in (response({}, 503), response({}), response([None]), response([{}] * 101)):
            with self.subTest(response=second):
                self.session.request.reset_mock()
                self.session.request.side_effect = [response(rows), second]
                with self.assertRaises(ApiFailure):
                    self.client.accept_requests(20)
                self.assertTrue(all(call.args[0] == "GET" for call in self.session.request.call_args_list))

    def test_deadline_defers_remaining_requests_and_next_tick_accepts_them(self):
        clock = [0.0]
        pending = ["frq_" + str(i) for i in range(10)]
        accepted_ids = []

        def respond(method, url, **kwargs):
            if url.endswith("/auth/user"):
                return response({"id": "usr_bot"})
            if url.endswith("/friends"):
                return response([])
            if url.endswith("/notifications"):
                return response([{"type": "friendRequest", "id": uid} for uid in pending])
            self.assertEqual(method, "PUT")
            uid = url.split("/")[-2]
            pending.remove(uid)
            accepted_ids.append(uid)
            return response({"success": {}})

        def sleep(seconds):
            clock[0] += seconds

        self.session.request.side_effect = respond
        with patch("playtime.vrchat.time.monotonic", side_effect=lambda: clock[0]):
            client = Client({}, "test", session=self.session, sleep=sleep, deadline=18)
            client.verify()
            client.friends(100, 120)
            with patch("playtime.vrchat.print") as output:
                self.assertEqual(client.accept_requests(20), 6)
            diagnostic = json.loads(output.call_args.args[0].removeprefix("[playtime] Friend request scan: "))
            self.assertTrue(diagnostic["budget_exhausted"])
            self.assertEqual(diagnostic["deferred_known"], 4)
            self.assertEqual(len(pending), 4)
            clock[0] = 60
            client = Client({}, "test", session=self.session, sleep=sleep, deadline=78)
            client.verify()
            client.friends(100, 120)
            self.assertEqual(client.accept_requests(20), 4)
        self.assertEqual(pending, [])
        self.assertEqual(len(set(accepted_ids)), 10)
        self.assertTrue(all(call.args[0] in {"GET", "PUT"} for call in self.session.request.call_args_list))

    def test_scan_budget_exhaustion_leaves_partial_queue_untouched(self):
        rows = [{"type": "invite"}] * 99 + [{"type": "friendRequest", "id": "frq_waiting"}]
        self.client.call = Mock(side_effect=[rows, ApiFailure("time_budget", operation="list-requests")])
        with patch("playtime.vrchat.print") as output:
            self.assertEqual(self.client.accept_requests(20), 0)
        self.assertTrue(all(call.args[0] == "GET" for call in self.client.call.call_args_list))
        diagnostic = json.loads(output.call_args.args[0].removeprefix("[playtime] Friend request scan: "))
        self.assertTrue(diagnostic["budget_exhausted"])
        self.assertEqual(diagnostic["deferred_known"], 1)

    def test_zero_request_limit_makes_no_calls(self):
        self.assertEqual(self.client.accept_requests(0), 0)
        self.session.request.assert_not_called()

    def test_invalid_request_limit_makes_no_calls(self):
        for limit in (-1, 101, True, "20"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.client.accept_requests(limit)
        self.session.request.assert_not_called()

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

    def test_optional_queue_failures_do_not_erase_complete_sample_continuity(self):
        errors = [ApiFailure(status, operation="accept-request") for status in
                  ("time_budget", "network_error", "invalid_response")]
        errors += [ApiFailure("api_error", operation="list-requests", http_status=code) for code in (404, 500)]
        errors += [ValueError("private response contents")]
        for error in errors:
            with self.subTest(error=str(error)):
                self.client.accept_requests.side_effect = error
                state = copy.deepcopy(self.state)
                for run, now in ((2, 1060), (3, 1120), (4, 1180)):
                    with patch("playtime.run.time.time", return_value=now):
                        state, changed = process(state, self.client, CONFIG, run, now=now)
                    self.assertTrue(changed)
                    self.assertEqual(state["status"], "ok")
                    self.assertEqual(state["last_sample"], now)
                    self.assertEqual(state["last_success"], now)
                    self.assertTrue(state["players"]["usr_one"]["present"])
                    self.assertEqual(state["players"]["usr_one"]["seconds"], now - 1000)
                self.assertEqual(leaderboard(state)["top"][0]["seconds"], 180)

    def test_known_optional_401_defers_only_to_next_ticks_verified_session_check(self):
        for reason in ("missing-credentials", "unauthorized"):
            with self.subTest(reason=reason):
                self.client.reset_mock()
                self.client.verify.side_effect = None
                self.client.accept_requests.side_effect = ApiFailure(
                    "login_required", operation="accept-request", http_status=401, reason=reason)
                with patch("playtime.run.time.time", return_value=1060):
                    state, _ = process(self.state, self.client, CONFIG, 2, now=1060)
                self.assertEqual(state["status"], "ok")
                self.assertEqual(state["friend_request_status"], "session_verification_pending")
                self.assertEqual(state["last_sample"], 1060)
                self.assertTrue(state["players"]["usr_one"]["present"])
                self.assertEqual(state["players"]["usr_one"]["seconds"], 60)
                self.assertFalse(state.get("session_recovery_pending", False))
                self.assertFalse(state.get("auto_login_blocked", False))
                self.client.verify.assert_called_once()
                self.client.accept_requests.assert_called_once()
                self.client.verify.side_effect = ApiFailure(
                    "login_required", operation="verify-session", http_status=401,
                    reason=reason, session_rejected=True)
                self.client.accept_requests.reset_mock()
                with patch("playtime.run.time.time", return_value=1120):
                    rejected, _ = process(state, self.client, CONFIG, 3, now=1120)
                self.assertEqual(rejected["status"], "login_required")
                self.assertTrue(rejected["session_recovery_pending"])
                self.assertFalse(rejected.get("auto_login_blocked", False))
                self.assertEqual(rejected["players"]["usr_one"]["seconds"], 60)
                self.client.accept_requests.assert_not_called()

    def test_per_run_login_does_not_logout_or_retry_after_optional_session_401(self):
        self.client.accept_requests.side_effect = ApiFailure(
            "login_required", operation="accept-request", http_status=401, reason="unauthorized")
        with patch("playtime.run.login_client", return_value=self.client) as login, \
                patch("playtime.run.time.time", return_value=1060):
            state, _ = process_with_login(self.state, CONFIG, 2, resume=True, now=1060)
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["friend_request_status"], "session_verification_pending")
        self.assertEqual(state["players"]["usr_one"]["seconds"], 60)
        login.assert_called_once()
        self.client.verify.assert_called_once()
        self.client.accept_requests.assert_called_once()
        self.client.logout.assert_not_called()

    def test_optional_auth_and_rate_guards_still_hard_stop_without_retry(self):
        failures = [
            ApiFailure("login_required", operation="accept-request", http_status=401, reason="unclassified"),
            ApiFailure("login_required", operation="accept-request", http_status=200, reason="two-factor-required"),
            ApiFailure("login_required", operation="accept-request", http_status=429,
                       reason="email-location-verification", retry_after=7200),
            ApiFailure("login_required", operation="accept-request", http_status=401,
                       reason="unauthorized", retry_after=7200),
            ApiFailure("access_denied", operation="accept-request", http_status=403),
            ApiFailure("rate_limited", operation="accept-request", http_status=429, retry_after=7200),
        ]
        for error in failures:
            with self.subTest(status=error.status, reason=error.reason, code=error.http_status):
                self.client.reset_mock()
                self.client.accept_requests.side_effect = error
                with patch("playtime.run.time.time", return_value=1060):
                    state, _ = process(self.state, self.client, CONFIG, 2, now=1060)
                self.assertEqual(state["status"], error.status)
                self.assertEqual(state["players"]["usr_one"]["seconds"], 60)
                self.assertIsNone(state["last_sample"])
                self.assertFalse(state["players"]["usr_one"]["present"])
                self.assertTrue(state["auto_login_blocked"])
                self.assertFalse(state["session_recovery_pending"])
                if error.retry_after:
                    self.assertGreaterEqual(state["retry_at"], 1060 + error.retry_after)
                self.client.verify.assert_called_once()
                self.client.accept_requests.assert_called_once()

    def test_optional_http_401_with_retry_after_keeps_credit_and_hard_stops(self):
        session = requests.Session()
        session.request = Mock(side_effect=[
            response({"id": "usr_bot"}), response([friend()]),
            response({"error": {"message": "Unauthorized"}}, 401, {"Retry-After": "120"}),
        ])
        client = Client({"auth": "SYNTHETIC"}, "test@example.org", session=session, sleep=Mock())
        with patch("playtime.run.time.time", return_value=1060), patch("playtime.vrchat.print"):
            state, changed = process(self.state, client, CONFIG, 2, now=1060)
        self.assertTrue(changed)
        self.assertEqual(state["status"], "login_required")
        self.assertEqual(state["players"]["usr_one"]["seconds"], 60)
        self.assertIsNone(state["last_sample"])
        self.assertFalse(state["players"]["usr_one"]["present"])
        self.assertTrue(state["auto_login_blocked"])
        self.assertFalse(state["session_recovery_pending"])
        self.assertNotEqual(state["friend_request_status"], "session_verification_pending")
        self.assertEqual(state["retry_at"], 1180)
        self.assertEqual(session.request.call_count, 3)
        waiting, changed = process(state, client, CONFIG, 3, now=1120)
        self.assertFalse(changed)
        self.assertEqual(waiting, state)
        self.assertEqual(session.request.call_count, 3)

    def test_incomplete_presence_samples_still_fail_closed_before_optional_work(self):
        for status in ("time_budget", "network_error", "invalid_response", "unstable_snapshot", "page_limit"):
            with self.subTest(status=status):
                self.client.reset_mock()
                self.client.friends.side_effect = ApiFailure(status, operation="list-friends")
                with patch("playtime.run.time.time", return_value=1060):
                    state, _ = process(self.state, self.client, CONFIG, 2, now=1060)
                self.assertEqual(state["status"], status)
                self.assertEqual(state["players"]["usr_one"]["seconds"], 0)
                self.assertFalse(state["players"]["usr_one"]["present"])
                self.assertIsNone(state["last_sample"])
                self.client.accept_requests.assert_not_called()

    def test_deferred_request_telemetry_excludes_private_response_and_cookie(self):
        self.client.accept_requests.side_effect = ApiFailure(
            "api_error", operation="list-requests", http_status=500, reason="PRIVATE_RESPONSE")
        self.client.cookies.return_value = {"auth": "PRIVATE_COOKIE"}
        with patch("playtime.run.print") as output, patch("playtime.run.time.time", return_value=1060):
            process(self.state, self.client, CONFIG, 2, now=1060)
        logs = "\n".join(call.args[0] for call in output.call_args_list)
        self.assertIn("complete sample retained", logs)
        for private in ("PRIVATE_RESPONSE", "PRIVATE_COOKIE", "usr_one", "wrld_test"):
            self.assertNotIn(private, logs)

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
