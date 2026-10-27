import copy
import io
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet

from api.playtime import handler
from playtime.model import apply_sample, new_state
from playtime.serverless import handle, initialize_empty, public_report, tick, configuration, configured_store
from playtime.serverless_store import RedisStore, LEASE_SECONDS, RELEASE, SAVE
from playtime.vrchat import ApiFailure, Client

CONFIG = {**configuration(), "world_id": "wrld_test"}
FRIEND = {"id": "usr_one", "displayName": "One", "location": "wrld_test:private-instance"}
CRON = "c" * 40
ADMIN = "a" * 40
ENV = {"CRON_SECRET": CRON, "PLAYTIME_ADMIN_SECRET": ADMIN, "PLAYTIME_ENABLED": "true",
       "VERCEL_ENV": "production", "VRC_CONTACT": "bot@example.org"}


class MemoryStore:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.busy = False
        self.saves = []

    def acquire(self):
        return not self.busy

    def release(self):
        pass

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        state["revision"] += 1
        self.state = copy.deepcopy(state)
        self.saves.append(copy.deepcopy(state))


class MinutePollingTests(unittest.TestCase):
    def setUp(self):
        self.state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], 1200, 77, 600)
        self.state["cookies"] = {"auth": "not-public"}
        self.store = MemoryStore(self.state)
        self.client = Mock()
        self.client.friends.return_value = [FRIEND]
        self.client.accept_requests.return_value = 1
        self.client.cookies.return_value = {"auth": "not-public"}

    def run_tick(self, now=1260, resume=False):
        with patch("playtime.serverless.Client", return_value=self.client), \
                patch("playtime.run.time.time", return_value=now):
            return tick(self.store, CONFIG, "bot@example.org", resume=resume, now=now)

    def test_minute_credits_actual_seconds_and_never_password_logs_in(self):
        with patch("playtime.serverless.login_client") as login:
            result = self.run_tick()
        login.assert_not_called()
        self.client.logout.assert_not_called()
        self.assertTrue(result["sampled"])
        self.assertEqual(result["poll_seconds"], 60)
        self.assertEqual(self.store.state["players"]["usr_one"]["seconds"], 60)
        self.assertEqual(self.store.state["last_run"], 78)
        self.assertEqual(self.store.state["cookies"], {"auth": "not-public"})

    def test_duplicate_minute_and_out_of_order_tick_do_not_send_requests(self):
        self.run_tick()
        self.client.reset_mock()
        self.assertEqual(self.run_tick(1290)["status"], "already_checked")
        self.assertEqual(self.run_tick(1259)["status"], "already_checked")
        self.client.verify.assert_not_called()

    def test_following_minute_is_processed(self):
        self.run_tick()
        self.assertTrue(self.run_tick(1320)["sampled"])
        self.assertEqual(self.store.state["players"]["usr_one"]["seconds"], 120)

    def test_busy_worker_prevents_all_api_and_storage_work(self):
        self.store.busy = True
        self.assertEqual(self.run_tick()["status"], "busy")
        self.client.verify.assert_not_called()
        self.assertFalse(self.store.saves)

    def test_unknown_login_and_blocked_state_never_auto_login(self):
        for status in ("login_required", "access_denied", "waiting_for_login", "configuration_error"):
            with self.subTest(status=status), patch("playtime.serverless.login_client") as login:
                self.store.state["status"] = status
                self.assertFalse(self.run_tick()["sampled"])
                login.assert_not_called()
        self.client.verify.assert_not_called()

    def test_missing_cookie_never_auto_login(self):
        self.store.state["cookies"] = {}
        with patch("playtime.serverless.login_client") as login:
            self.assertEqual(self.run_tick()["status"], "login_required")
        login.assert_not_called()

    def test_manual_resume_still_honors_rate_limit(self):
        self.store.state.update(status="rate_limited", retry_at=1800)
        with patch("playtime.serverless.login_client") as login:
            self.assertEqual(self.run_tick(resume=True)["retry_at"], 1800)
        login.assert_not_called()
        self.client.verify.assert_not_called()

    def test_manual_resume_uses_memory_only_login(self):
        self.store.state.update(status="login_required", cookies={})
        with patch("playtime.serverless.login_client", return_value=self.client) as login:
            result = self.run_tick(resume=True)
        self.assertEqual(result["status"], "ok")
        login.assert_called_once_with(2.0, persist=False)
        self.assertEqual(self.store.saves[0]["status"], "login_required")

    def test_failed_manual_login_blocks_future_automatic_calls(self):
        with patch("playtime.serverless.login_client", side_effect=ApiFailure("login_required", 3600)):
            self.assertEqual(self.run_tick(resume=True)["status"], "login_required")
        self.assertEqual(self.store.state["retry_at"], 4860)
        self.assertEqual(self.store.state["cookies"], {})
        self.assertFalse(self.run_tick(1320)["sampled"])

    def test_429_stops_notifications_and_future_polls(self):
        self.client.verify.side_effect = ApiFailure("rate_limited", 7200)
        self.assertEqual(self.run_tick()["status"], "rate_limited")
        self.client.accept_requests.assert_not_called()
        self.client.reset_mock()
        self.run_tick(1320)
        self.client.verify.assert_not_called()
        self.assertEqual(self.store.state["players"]["usr_one"]["seconds"], 0)

    def test_attempt_persisted_before_requests_and_crash_breaks_continuity(self):
        self.client.verify.side_effect = RuntimeError("process interrupted")
        with self.assertRaises(RuntimeError):
            self.run_tick()
        self.assertEqual(self.store.state["status"], "sampling")
        self.assertIsNone(self.store.state["last_sample"])
        self.assertEqual(self.run_tick(1280)["status"], "already_checked")
        self.client.verify.side_effect = None
        self.run_tick(1320)
        self.assertEqual(self.store.state["players"]["usr_one"]["seconds"], 0)

    def test_slow_incomplete_friends_never_become_empty_valid_snapshot(self):
        self.client.friends.side_effect = ApiFailure("time_budget")
        self.assertEqual(self.run_tick()["status"], "time_budget")
        self.assertEqual(self.store.state["last_success"], 1200)
        self.client.accept_requests.assert_not_called()

    def test_public_report_exposes_staleness_but_not_secrets(self):
        report = public_report(self.state, CONFIG, 1801)
        self.assertEqual(report["status"], "stale")
        for secret in ("not-public", "usr_one", "private-instance"):
            self.assertNotIn(secret, json.dumps(report))


class HttpTests(unittest.TestCase):
    def test_deployment_keeps_file_based_python_functions(self):
        config = json.loads((Path(__file__).parents[1] / "vercel.json").read_text())
        self.assertIn("framework", config)
        self.assertIsNone(config["framework"])
        self.assertEqual(config["buildCommand"], "")
        self.assertIn("api/playtime.py", config["functions"])

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_no_auth_never_opens_storage(self, store):
        for auth in ("", "Bearer wrong", "Bearer \u044f"):
            self.assertEqual(handle("POST", auth)[0], 401)
        store.assert_not_called()

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_cron_cannot_force_login(self, store):
        self.assertEqual(handle("POST", "Bearer " + CRON, "resume")[0], 401)
        store.assert_not_called()

    @patch.dict("os.environ", {**ENV, "VERCEL_ENV": "preview"}, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_preview_cannot_contact_vrchat(self, store):
        self.assertEqual(handle("POST", "Bearer " + CRON)[0], 503)
        store.assert_not_called()

    @patch.dict("os.environ", {**ENV, "PLAYTIME_ENABLED": "false"}, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_paused_production_does_not_sample(self, store):
        self.assertEqual(handle("POST", "Bearer " + CRON)[0], 503)
        store.assert_not_called()

    @patch.dict("os.environ", {**ENV, "PLAYTIME_ADMIN_SECRET": CRON}, clear=True)
    def test_admin_and_cron_keys_must_differ(self):
        self.assertEqual(handle("POST", "Bearer " + CRON, "resume")[0], 503)

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    @patch("playtime.serverless.tick")
    def test_failure_is_not_reported_as_green_http(self, check, store):
        check.return_value = {"status": "login_required", "sampled": False}
        self.assertEqual(handle("POST", "Bearer " + CRON)[0], 503)
        check.return_value = {"status": "busy", "sampled": False}
        self.assertEqual(handle("POST", "Bearer " + CRON)[0], 200)

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store", side_effect=ValueError("PRIVATE"))
    def test_exception_content_not_returned(self, store):
        self.assertNotIn("PRIVATE", json.dumps(handle("POST", "Bearer " + CRON)))

    def request_handler(self):
        request = object.__new__(handler)
        request.headers = {"Authorization": "Bearer " + CRON}
        request.wfile = io.BytesIO()
        request.send_response = Mock()
        request.send_header = Mock()
        request.end_headers = Mock()
        return request

    @patch("api.playtime.handle", return_value=(200, {"status": "ok", "top": []}))
    def test_public_route_is_read_only_and_cacheable(self, execute):
        request = self.request_handler()
        request.do_GET()
        execute.assert_called_once_with("GET", "Bearer " + CRON, "sample")
        self.assertEqual(json.loads(request.wfile.getvalue()), {"status": "ok", "top": []})
        request.send_header.assert_any_call("Cache-Control", "public, s-maxage=60, stale-while-revalidate=60")

    @patch("api.playtime.handle", return_value=(503, {"status": "login_required"}))
    def test_post_failure_is_not_cached(self, execute):
        request = self.request_handler()
        request.do_POST()
        execute.assert_called_once_with("POST", "Bearer " + CRON, "sample")
        request.send_response.assert_called_once_with(503)
        request.send_header.assert_any_call("Cache-Control", "no-store")


class StoreConfigurationTests(unittest.TestCase):
    @patch.dict("os.environ", {"VRC_USERNAME": "bot", "VRC_SESSION_KEY": "key"}, clear=True)
    @patch("playtime.serverless.RedisStore")
    def test_vercel_marketplace_names_work_without_copying_secrets(self, factory):
        with patch.dict("os.environ", {"KV_REST_API_URL": "https://test.upstash.io", "KV_REST_API_TOKEN": "write-token"}):
            configured_store(CONFIG)
        factory.assert_called_once_with("https://test.upstash.io", "write-token", "key", "bot", CONFIG["world_id"])

    @patch.dict("os.environ", {"VRC_USERNAME": "bot", "VRC_SESSION_KEY": "key"}, clear=True)
    @patch("playtime.serverless.RedisStore")
    def test_explicit_upstash_pair_takes_precedence(self, factory):
        with patch.dict("os.environ", {"UPSTASH_REDIS_REST_URL": "https://explicit.upstash.io",
                                      "UPSTASH_REDIS_REST_TOKEN": "explicit-token",
                                      "KV_REST_API_URL": "https://other.upstash.io", "KV_REST_API_TOKEN": "other-token"}):
            configured_store(CONFIG)
        self.assertEqual(factory.call_args.args[:2], ("https://explicit.upstash.io", "explicit-token"))

    @patch.dict("os.environ", {"VRC_USERNAME": "bot", "VRC_SESSION_KEY": "key"}, clear=True)
    @patch("playtime.serverless.RedisStore")
    def test_partial_explicit_pair_does_not_mix_two_databases(self, factory):
        with patch.dict("os.environ", {"UPSTASH_REDIS_REST_URL": "https://explicit.upstash.io",
                                      "KV_REST_API_URL": "https://other.upstash.io", "KV_REST_API_TOKEN": "other-token"}):
            with self.assertRaises(ValueError):
                configured_store(CONFIG)
        factory.assert_not_called()

    @patch.dict("os.environ", {"VRC_USERNAME": "bot", "VRC_SESSION_KEY": "key"}, clear=True)
    @patch("playtime.serverless.RedisStore")
    def test_read_only_token_is_never_used_for_counter_storage(self, factory):
        with patch.dict("os.environ", {"KV_REST_API_URL": "https://test.upstash.io", "KV_REST_API_READ_ONLY_TOKEN": "read-token"}):
            with self.assertRaises(ValueError):
                configured_store(CONFIG)
        factory.assert_not_called()


class RedisStoreTests(unittest.TestCase):
    def setUp(self):
        self.key = Fernet.generate_key().decode()
        self.store = RedisStore("https://test.upstash.io", "secret", self.key, "bot", "wrld_test")
        self.store.command = Mock()
        self.state = new_state("bot", "wrld_test")

    def test_encoded_checkpoint_is_encrypted_and_identity_checked(self):
        self.state["cookies"] = {"auth": "PRIVATE"}
        encoded = self.store.encode(self.state)
        self.assertNotIn("PRIVATE", encoded)
        self.store.command.return_value = encoded
        self.assertEqual(self.store.load(), self.state)
        self.state["account"] = "wrong"
        with self.assertRaises(ValueError):
            self.store.encode(self.state)

    def test_missing_or_corrupt_store_never_initializes_automatically(self):
        for value in (None, "corrupt"):
            self.store.command.return_value = value
            with self.assertRaises(Exception):
                self.store.load()
        self.assertTrue(all(call.args[0] == "GET" for call in self.store.command.call_args_list))

    def test_existing_checkpoint_cannot_be_overwritten_by_initialization(self):
        self.store.command.return_value = None
        with self.assertRaises(RuntimeError):
            self.store.initialize(self.state)
        self.assertEqual(self.store.command.call_args.args[-1], "NX")

    def test_lock_is_atomic_expiring_and_longer_than_hard_function_limit(self):
        self.store.command.return_value = "OK"
        self.assertTrue(self.store.acquire())
        args = self.store.command.call_args.args
        self.assertEqual(args[-3:], ("NX", "EX", LEASE_SECONDS))
        config = json.loads((Path(__file__).parents[1] / "vercel.json").read_text())
        self.assertGreater(LEASE_SECONDS, config["functions"]["api/playtime.py"]["maxDuration"])

    def test_save_checks_lease_and_loaded_checkpoint_in_one_atomic_script(self):
        self.store.lease, self.store.loaded = "owner", "old-ciphertext"
        self.store.command.return_value = 1
        self.store.save(self.state)
        args = self.store.command.call_args.args
        self.assertEqual(args[:3], ("EVAL", SAVE, 3))
        self.assertEqual(args[6:8], ("owner", "old-ciphertext"))
        self.assertEqual(self.state["revision"], 1)

    def test_expired_lease_does_not_advance_local_revision(self):
        self.store.lease, self.store.loaded = "old-owner", "old-ciphertext"
        self.store.command.return_value = 0
        with self.assertRaises(RuntimeError):
            self.store.save(self.state)
        self.assertEqual(self.state["revision"], 0)

    def test_release_is_conditional_on_owner(self):
        self.store.lease = "owner"
        self.store.release()
        self.store.command.assert_called_once_with("EVAL", RELEASE, 1, self.store.prefix + ":lock", "owner")

    def test_redirects_or_error_bodies_never_leak_credentials(self):
        session = Mock(headers={})
        session.post.return_value.status_code = 302
        store = RedisStore("https://test.upstash.io", "PRIVATE", self.key, "bot", "wrld_test", session)
        with self.assertRaisesRegex(RuntimeError, "State service unavailable"):
            store.acquire()
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])


class RequestBudgetTests(unittest.TestCase):
    def test_expired_deadline_sends_no_request(self):
        session = Mock(headers={})
        client = Client({}, "contact", session=session, deadline=100)
        with patch("playtime.vrchat.time.monotonic", return_value=101), self.assertRaises(ApiFailure) as error:
            client.verify()
        self.assertEqual(error.exception.status, "time_budget")
        session.request.assert_not_called()

    def test_serverless_timeout_is_short_but_existing_client_is_unchanged(self):
        session = Mock(headers={})
        session.request.return_value.status_code = 200
        session.request.return_value.json.return_value = {"id": "usr_one"}
        client = Client({}, "contact", session=session, deadline=108)
        with patch("playtime.vrchat.time.monotonic", return_value=100):
            client.verify()
        self.assertEqual(session.request.call_args.kwargs["timeout"], (3, 4))
        Client({}, "contact", session=session).verify()
        self.assertEqual(session.request.call_args.kwargs["timeout"], (10, 25))


class InitializationTests(unittest.TestCase):
    @patch.dict("os.environ", {"VRC_USERNAME": "bot"}, clear=True)
    @patch("playtime.serverless.configured_store")
    @patch("playtime.storage.Store")
    def test_new_counter_starts_empty_without_reading_github(self, old_store, destination):
        initialize_empty()
        state = destination.return_value.initialize.call_args.args[0]
        self.assertEqual(state["players"], {})
        self.assertIsNone(state["last_success"])
        self.assertEqual(state["revision"], 0)
        self.assertEqual(state["last_run"], 0)
        self.assertEqual(state["cookies"], {})
        self.assertEqual(state["status"], "waiting_for_login")
        old_store.assert_not_called()


if __name__ == "__main__":
    unittest.main()
