import copy
import json
import unittest
from unittest.mock import Mock, patch

from playtime.model import apply_sample, new_state
from playtime.scheduler import CronClient, configure, status, TARGET, TITLE, SCHEDULE
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
        for action in ("schedule", "schedule-status"):
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


if __name__ == "__main__":
    unittest.main()
