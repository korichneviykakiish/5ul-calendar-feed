import copy
import json
import unittest
from unittest.mock import Mock, patch

from playtime.model import apply_sample, new_state
from playtime.scheduler import CronClient, configure, status, rearm, TARGET, TITLE, SCHEDULE
from playtime.test_serverless import ADMIN, CRON, ENV, CONFIG, FRIEND, MemoryStore
from playtime.serverless import handle


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], 1200, 1, 600)
        state = apply_sample(state, [FRIEND], 1260, 2, 600)
        self.store = MemoryStore(state)
        self.client = Mock()
        self.job = {"jobId": 123, "title": TITLE, "url": TARGET, "enabled": False,
                    "requestMethod": 1, "schedule": SCHEDULE,
                    "extendedData": {"headers": {"Authorization": "Bearer " + CRON}}}

    def configure(self, responses):
        self.client.call.side_effect = responses
        with patch.dict("os.environ", {"CRON_SECRET": CRON}):
            return configure(self.store, self.client, now=1320)

    def test_new_job_is_created_disabled_then_verified_and_enabled(self):
        result = self.configure([{"jobs": [], "someFailed": False}, {"jobId": 123},
                                 {"jobDetails": self.job}, {},
                                 {"jobDetails": {**self.job, "enabled": True}}])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["poll_seconds"], 60)
        calls = self.client.call.call_args_list
        self.assertEqual(calls[1].args[:2], ("PUT", "/jobs"))
        self.assertFalse(calls[1].args[2]["job"]["enabled"])
        job = calls[3].args[2]["job"]
        self.assertEqual(job["url"], TARGET)
        self.assertEqual(job["requestMethod"], 1)
        self.assertEqual(job["schedule"]["minutes"], [-1])
        self.assertEqual(job["requestTimeout"], 30)
        self.assertFalse(job["saveResponses"])
        self.assertNotIn(ADMIN, json.dumps(job))
        self.assertNotIn("X-Playtime-Action", job["extendedData"]["headers"])
        self.assertEqual(self.store.state["scheduler_job_id"], 123)

    def test_existing_managed_job_is_reused_not_duplicated(self):
        self.configure([{"jobs": [self.job], "someFailed": False}, {"jobDetails": self.job}, {},
                        {"jobDetails": {**self.job, "enabled": True}}])
        self.assertNotIn("PUT", [c.args[0] for c in self.client.call.call_args_list])

    def test_incomplete_listing_or_duplicates_never_create_or_edit_jobs(self):
        for listing in ({"jobs": [], "someFailed": True}, {"jobs": [self.job, self.job], "someFailed": False},
                        {"jobs": [{**self.job, "title": "User's own task"}], "someFailed": False}):
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                self.configure([listing])
            self.assertEqual(self.client.call.call_args_list[-1].args[0], "GET")
            self.client.reset_mock()

    def test_unrelated_job_is_not_modified(self):
        self.client.call.side_effect = [{"jobs": [self.job], "someFailed": False},
                                        {"jobDetails": {**self.job, "url": "https://unrelated.example"}}]
        with patch.dict("os.environ", {"CRON_SECRET": CRON}), self.assertRaises(ValueError):
            configure(self.store, self.client, now=1320)
        self.assertFalse(any(c.args[0] == "PATCH" for c in self.client.call.call_args_list))

    def test_stale_or_unconfirmed_login_cannot_enable_schedule(self):
        for changes in ({"status": "login_required"}, {"last_run": 1}, {"last_success": 1000}):
            with self.subTest(changes=changes):
                saved = copy.deepcopy(self.store.state)
                self.store.state.update(changes)
                with self.assertRaises(ValueError):
                    self.configure([])
                self.client.call.assert_not_called()
                self.store.state = saved

    def test_wrong_job_settings_are_not_recorded_as_configured(self):
        with self.assertRaises(ValueError):
            self.configure([{"jobs": [self.job], "someFailed": False}, {"jobDetails": self.job}, {},
                            {"jobDetails": {**self.job, "enabled": True, "requestMethod": 0}}])
        self.assertNotIn("scheduler_job_id", self.store.state)

    def test_busy_configuration_does_not_contact_provider(self):
        self.store.busy = True
        self.assertEqual(self.configure([])["status"], "busy")
        self.client.call.assert_not_called()

    def test_non_numeric_job_id_cannot_be_used_in_url(self):
        with self.assertRaises(ValueError):
            self.configure([{"jobs": [{**self.job, "jobId": "../other"}], "someFailed": False}])
        self.assertEqual(self.client.call.call_count, 1)

    def test_status_returns_only_safe_history_and_never_provider_credentials(self):
        self.store.state["scheduler_job_id"] = 123
        self.client.call.side_effect = [
            {"jobDetails": {**self.job, "enabled": True}},
            {"history": [{"date": 1320, "datePlanned": 1320, "httpStatus": 200, "status": 1,
                          "duration": 3000, "headers": CRON, "body": ADMIN}]}]
        result = status(self.store, self.client)
        self.assertEqual(result["history"][0]["httpStatus"], 200)
        self.assertNotIn(CRON, json.dumps(result))
        self.assertNotIn(ADMIN, json.dumps(result))

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_cron_token_cannot_manage_or_read_schedule(self, factory):
        for action in ("schedule", "schedule-status", "schedule-rearm"):
            self.assertEqual(handle("POST", "Bearer " + CRON, action)[0], 401)
        factory.assert_not_called()

    def test_provider_rejection_has_no_automatic_retry_and_no_secret_response_dump(self):
        session = Mock()
        session.request.return_value.status_code = 401
        session.request.return_value.text = "PRIVATE"
        client = CronClient("private-api-key", session=session)
        with self.assertRaises(RuntimeError) as error:
            client.call("GET", "/jobs")
        self.assertNotIn("PRIVATE", str(error.exception))
        session.request.assert_called_once()
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])


class SchedulerRearmTests(unittest.TestCase):
    def setUp(self):
        state = new_state("bot", CONFIG["world_id"])
        state.update(status="login_required", last_success=1200, scheduler_job_id=123,
                     session_recovery_pending=True, auto_login_blocked=False)
        self.store = MemoryStore(state)
        self.client = Mock()
        self.job = {"jobId": 123, "title": TITLE, "url": TARGET, "enabled": False,
                    "requestMethod": 1, "schedule": SCHEDULE, "saveResponses": False,
                    "auth": {"enable": False}, "redirectSuccess": False,
                    "extendedData": {"headers": {"Authorization": "Bearer " + CRON}, "body": ""}}

    def rearm(self, config=None):
        with patch.dict("os.environ", {"CRON_SECRET": CRON}):
            return rearm(self.store, CONFIG if config is None else config, self.client)

    def test_rearms_only_saved_task_without_touching_hours_or_credentials(self):
        before = copy.deepcopy(self.store.state)
        self.client.call.side_effect = [{"jobDetails": self.job}, {},
                                        {"jobDetails": {**self.job, "enabled": True}}]
        result = self.rearm()
        self.assertEqual(result["status"], "schedule_rearmed")
        self.assertEqual(self.client.call.call_args_list[1].args,
                         ("PATCH", "/jobs/123", {"job": {"enabled": True}}))
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.store.saves, [])

    def test_already_enabled_task_is_not_modified(self):
        self.client.call.return_value = {"jobDetails": {**self.job, "enabled": True}}
        self.assertTrue(self.rearm()["enabled"])
        self.client.call.assert_called_once_with("GET", "/jobs/123")

    def test_hard_stops_unknown_state_and_absent_task_cannot_rearm(self):
        original = copy.deepcopy(self.store.state)
        for changes in ({"auto_login_blocked": True}, {"session_recovery_pending": False},
                        {"status": "access_denied"}, {"status": "rate_limited"},
                        {"last_success": None}, {"scheduler_job_id": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.state = {**copy.deepcopy(original), **changes}
                self.rearm()
            self.client.call.assert_not_called()

    def test_disabled_policy_cannot_rearm(self):
        with self.assertRaises(ValueError):
            self.rearm({**CONFIG, "auto_recover_session": False})
        self.client.call.assert_not_called()

    def test_changed_task_is_not_taken_over(self):
        for changes in ({"title": "Other"}, {"url": "https://other.example"},
                        {"requestMethod": 0}, {"schedule": {}}, {"saveResponses": True},
                        {"auth": {"enable": True}}, {"redirectSuccess": True},
                        {"extendedData": {"headers": {"Authorization": "Bearer " + ADMIN}}},
                        {"extendedData": {"headers": {"Authorization": "Bearer " + CRON,
                                                       "X-Playtime-Action": "resume"}}}):
            self.client.reset_mock()
            self.client.call.return_value = {"jobDetails": {**self.job, **changes}}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.rearm()
            self.assertTrue(all(c.args[0] == "GET" for c in self.client.call.call_args_list))

    def test_unconfirmed_rearm_is_not_reported_as_enabled(self):
        self.client.call.side_effect = [{"jobDetails": self.job}, {}, {"jobDetails": self.job}]
        with self.assertRaises(ValueError):
            self.rearm()

    def test_busy_rearm_does_not_contact_provider(self):
        self.store.busy = True
        self.assertEqual(self.rearm()["status"], "busy")
        self.client.call.assert_not_called()

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configuration", return_value=CONFIG)
    @patch("playtime.serverless.configured_store")
    @patch("playtime.serverless.rearm_schedule")
    @patch("playtime.serverless.login_client")
    def test_operator_rearm_route_never_logs_in(self, login, rearm_mock, store, config):
        rearm_mock.return_value = {"status": "schedule_rearmed", "enabled": True}
        self.assertEqual(handle("POST", "Bearer " + ADMIN, "schedule-rearm")[0], 200)
        rearm_mock.assert_called_once_with(store.return_value, CONFIG)
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
