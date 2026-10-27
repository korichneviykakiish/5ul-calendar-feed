import copy
import json
import unittest
from unittest.mock import Mock, patch

from playtime.model import new_state
from playtime.publication_scheduler import (
    API, BODY, OWNER, PREFIX, REPOSITORY, SCHEDULE, TARGET, TITLE, WORKFLOW,
    GitHubPublisher, configure, status,
)
from playtime.serverless import handle
from playtime.test_serverless import ADMIN, CRON, ENV, MemoryStore

TOKEN = "github_pat_" + "g" * 64


class PublicationSchedulerTests(unittest.TestCase):
    def setUp(self):
        state = new_state("bot", "wrld_test")
        state["players"] = {"usr_test": {"name": "Player", "seconds": 900, "present": True}}
        state["cookies"] = {"auth": "private-session"}
        state["scheduler_job_id"] = 999
        self.store = MemoryStore(state)
        self.client, self.github = Mock(), Mock()
        self.job = {"jobId": 123, "title": TITLE, "url": TARGET, "enabled": False,
                    "requestMethod": 1, "schedule": SCHEDULE, "saveResponses": False,
                    "extendedData": {"headers": {"Authorization": "Bearer " + TOKEN,
                                                 "Content-Type": "application/json"},
                                     "body": json.dumps(BODY)}}

    def configure(self, responses):
        self.client.call.side_effect = responses
        with patch.dict("os.environ", {"GH_REPORT_DISPATCH_TOKEN": TOKEN}, clear=True):
            return configure(self.store, self.client, self.github)

    def new_job(self):
        return [{"jobs": [], "someFailed": False}, {"jobId": 123}, {"jobDetails": self.job}, {},
                {"jobDetails": {**self.job, "enabled": True}}]

    def existing_job(self):
        return [{"jobs": [self.job], "someFailed": False}, {"jobDetails": self.job}, {},
                {"jobDetails": {**self.job, "enabled": True}}]

    def test_new_job_verifies_access_then_creates_disabled_and_enables(self):
        result = self.configure(self.new_job())
        self.assertEqual(result, {"status": "publication_schedule_ready", "job_id": 123,
                                  "enabled": True, "publication_seconds": 300})
        self.github.verify.assert_called_once()
        self.github.dispatch.assert_called_once()
        calls = self.client.call.call_args_list
        self.assertFalse(calls[1].args[2]["job"]["enabled"])
        job = calls[3].args[2]["job"]
        self.assertEqual(job["url"], TARGET)
        self.assertEqual(job["requestMethod"], 1)
        self.assertEqual(json.loads(job["extendedData"]["body"]), {"ref": "main"})
        self.assertEqual(job["schedule"]["minutes"], list(range(1, 60, 5)))
        self.assertFalse(job["saveResponses"])
        self.assertFalse(job["redirectSuccess"])
        for private in (ADMIN, CRON, "private-session"):
            self.assertNotIn(private, json.dumps(job))
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_existing_job_is_reused_and_sampler_is_not_modified(self):
        before = copy.deepcopy(self.store.state)
        self.configure(self.existing_job())
        self.assertEqual(self.store.state["publication_job_id"], 123)
        for field in ("players", "cookies", "scheduler_job_id", "last_run", "status"):
            self.assertEqual(self.store.state[field], before[field])
        self.assertNotIn(TOKEN, json.dumps(self.store.state))
        self.assertFalse(any(call.args[0] == "PUT" for call in self.client.call.call_args_list))

    def test_missing_or_ambiguous_task_refuses_all_effects(self):
        for listing in ({"jobs": [], "someFailed": True},
                        {"jobs": [self.job, self.job], "someFailed": False},
                        {"jobs": [{**self.job, "title": "Another task"}], "someFailed": False}):
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                self.configure([listing])
            self.github.dispatch.assert_not_called()
            self.assertEqual(self.client.call.call_count, 1)
            self.client.reset_mock()

    def test_recorded_task_cannot_be_silently_replaced(self):
        self.store.state["publication_job_id"] = 456
        with self.assertRaises(ValueError):
            self.configure([{"jobs": [self.job], "someFailed": False}])
        self.github.dispatch.assert_not_called()
        self.assertEqual(self.client.call.call_count, 1)

    def test_non_numeric_task_never_forms_provider_url(self):
        with self.assertRaises(ValueError):
            self.configure([{"jobs": [{**self.job, "jobId": "../other"}], "someFailed": False}])
        self.assertEqual(self.client.call.call_count, 1)

    def test_unrelated_task_details_cannot_be_edited(self):
        with self.assertRaises(ValueError):
            self.configure([{"jobs": [self.job], "someFailed": False},
                            {"jobDetails": {**self.job, "url": "https://unrelated.example"}}])
        self.github.dispatch.assert_not_called()
        self.assertFalse(any(call.args[0] == "PATCH" for call in self.client.call.call_args_list))

    def test_token_rejection_cannot_create_or_enable_a_job(self):
        self.github.dispatch.side_effect = RuntimeError("HTTP 403")
        with self.assertRaises(RuntimeError):
            self.configure([{"jobs": [], "someFailed": False}])
        self.assertEqual(self.client.call.call_count, 1)
        self.assertFalse(self.store.saves)

    def test_mismatched_provider_settings_are_not_saved_as_success(self):
        for changes in ({"requestMethod": 0}, {"saveResponses": True}, {"enabled": False},
                        {"extendedData": {"headers": {}, "body": "{}"}}):
            responses = self.new_job()
            responses[-1] = {"jobDetails": {**self.job, "enabled": True, **changes}}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.configure(responses)
            self.assertNotIn("publication_job_id", self.store.state)

    def test_busy_setup_does_not_contact_any_provider(self):
        self.store.busy = True
        self.assertEqual(self.configure([])["status"], "busy")
        self.client.call.assert_not_called()
        self.github.verify.assert_not_called()

    def test_publication_can_report_a_paused_counter_without_logging_in(self):
        self.store.state["status"] = "login_required"
        with patch("playtime.serverless.login_client") as login:
            self.assertTrue(self.configure(self.new_job())["enabled"])
        login.assert_not_called()
        self.assertEqual(self.store.state["status"], "login_required")

    def test_status_returns_safe_numeric_history_only(self):
        self.store.state["publication_job_id"] = 123
        self.client.call.side_effect = [{"jobDetails": {**self.job, "enabled": True}},
                                        {"history": [{"date": 1200, "httpStatus": 204, "status": 1,
                                                      "headers": TOKEN, "body": "PRIVATE"}]}]
        result = status(self.store, self.client)
        self.assertEqual(result["history"][0]["httpStatus"], 204)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_status_without_configured_job_never_contacts_provider(self):
        self.assertFalse(status(self.store, self.client)["enabled"])
        self.client.call.assert_not_called()


class GitHubPublisherTests(unittest.TestCase):
    def test_requires_a_dedicated_fine_grained_key(self):
        for token in ("", "ghp_" + "x" * 64, "github_pat_x", TOKEN + "\n", TOKEN + "\u044f"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                GitHubPublisher(token)

    def test_verification_is_limited_to_the_expected_account_and_workflow(self):
        session = Mock(headers={})
        publisher = GitHubPublisher(TOKEN, session)
        responses = [
            {"login": OWNER},
            {"full_name": REPOSITORY, "default_branch": "main", "archived": False, "disabled": False},
            {"path": ".github/workflows/" + WORKFLOW, "state": "active"}]
        session.request.side_effect = [Mock(status_code=200, json=Mock(return_value=value)) for value in responses]
        publisher.verify()
        self.assertEqual(session.request.call_args_list[0].args[:2], ("GET", API + "/user"))
        self.assertEqual(session.request.call_args_list[-1].args[1], API + PREFIX + "/actions/workflows/" + WORKFLOW)

    def test_wrong_account_cannot_dispatch(self):
        publisher = GitHubPublisher(TOKEN, Mock(headers={}))
        publisher.call = Mock(return_value={"login": "someone_else"})
        with self.assertRaises(ValueError):
            publisher.verify()
        publisher.call.assert_called_once()

    def test_archived_or_changed_repository_is_refused(self):
        publisher = GitHubPublisher(TOKEN, Mock(headers={}))
        for changes in ({"full_name": "other/repo"}, {"default_branch": "other"}, {"archived": True}):
            publisher.call = Mock(side_effect=[{"login": OWNER},
                {"full_name": REPOSITORY, "default_branch": "main", "archived": False,
                 "disabled": False, **changes}])
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                publisher.verify()

    def test_disabled_or_different_workflow_is_refused(self):
        publisher = GitHubPublisher(TOKEN, Mock(headers={}))
        publisher.call = Mock(side_effect=[{"login": OWNER},
            {"full_name": REPOSITORY, "default_branch": "main", "archived": False, "disabled": False},
            {"path": ".github/workflows/" + WORKFLOW, "state": "disabled_manually"}])
        with self.assertRaises(ValueError):
            publisher.verify()

    def test_dispatch_is_one_bounded_post_without_redirects_or_private_body(self):
        session = Mock(headers={})
        session.request.return_value.status_code = 204
        publisher = GitHubPublisher(TOKEN, session)
        publisher.dispatch()
        session.request.assert_called_once_with("POST", TARGET, json=BODY, timeout=(3, 8), allow_redirects=False)
        session.request.return_value.json.assert_not_called()

    def test_github_errors_have_no_retries_or_secret_response_dump(self):
        for code in (301, 401, 403, 429, 500):
            session = Mock(headers={})
            session.request.return_value.status_code = code
            session.request.return_value.text = TOKEN
            with self.subTest(code=code), self.assertRaises(RuntimeError) as error:
                GitHubPublisher(TOKEN, session).dispatch()
            self.assertNotIn(TOKEN, str(error.exception))
            session.request.assert_called_once()


class PublicationManagementHttpTests(unittest.TestCase):
    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_sampling_token_cannot_manage_publication(self, store):
        for action in ("publication-schedule", "publication-status"):
            self.assertEqual(handle("POST", "Bearer " + CRON, action)[0], 401)
        store.assert_not_called()

    @patch.dict("os.environ", {**ENV, "VERCEL_ENV": "preview"}, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_preview_cannot_manage_publication(self, store):
        self.assertEqual(handle("POST", "Bearer " + ADMIN, "publication-schedule")[0], 503)
        store.assert_not_called()

    @patch.dict("os.environ", {**ENV, "PLAYTIME_ENABLED": "false"}, clear=True)
    @patch("playtime.serverless.configured_store")
    @patch("playtime.serverless.configure_publication", return_value={"status": "publication_schedule_ready"})
    @patch("playtime.serverless.tick")
    def test_operator_can_manage_publication_without_enabling_sampling(self, tick, configure_job, store):
        self.assertEqual(handle("POST", "Bearer " + ADMIN, "publication-schedule")[0], 200)
        configure_job.assert_called_once_with(store.return_value)
        tick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
