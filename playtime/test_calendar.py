import copy
from datetime import datetime, timezone
import io
import json
import unittest
from unittest.mock import Mock, patch

from api.events import handler
from playtime.calendar import (advance, groups_config, month_queries, normalized_event,
                               status, validated_report, MAX_EVENTS)
from playtime.model import apply_sample, new_state
from playtime.serverless import handle, tick
from playtime.test_serverless import ADMIN, CRON, ENV, CONFIG, FRIEND, MemoryStore
from playtime.vrchat import ApiFailure

NOW = int(datetime(2026, 10, 28, 10, tzinfo=timezone.utc).timestamp())
GROUP = {"id": "grp_56fe79fc-ca06-4b67-858b-e4ec572afe5f", "name": "Public group"}
OTHER = {"id": "grp_528dc4da-8ccb-4c03-af92-cbd3c78eb74e", "name": "Second group"}
EVENT = {"id": "cal_one", "title": "Public <b>event</b>", "description": "Description",
         "startsAt": "2026-10-29T10:00:00Z", "endsAt": "2026-10-29T12:00:00Z",
         "accessType": "public", "isDraft": False, "interestedUserCount": 3, "platforms": ["android"]}
SAFE = normalized_event(EVENT, NOW)[1]
REPORT = {"gen": NOW - 3600, "groups": [{**GROUP, "events": [SAFE]}]}


class CalendarSchemaTests(unittest.TestCase):
    def test_current_groups_are_valid_unique_and_bounded(self):
        self.assertEqual(len(groups_config()), 9)

    def test_months_cover_the_entire_35_day_horizon_including_third_month(self):
        self.assertEqual(month_queries(NOW), ["2026-10-01T00:00:00.000Z", "2026-11-01T00:00:00.000Z",
                                             "2026-12-01T00:00:00.000Z"])
        boundary = int(datetime(2027, 1, 1, 1, tzinfo=timezone.utc).timestamp())
        self.assertEqual(month_queries(boundary)[0], "2026-12-01T00:00:00.000Z")

    def test_private_draft_deleted_or_implicit_access_is_never_published(self):
        for change in ({"accessType": "group"}, {"accessType": None}, {"isDraft": True},
                       {"deletedAt": "2026-10-27T10:00:00Z"}):
            with self.subTest(change=change):
                self.assertIsNone(normalized_event({**EVENT, **change}, NOW))

    def test_markup_and_private_fields_do_not_escape_the_public_schema(self):
        value = copy.deepcopy(REPORT)
        value["cookies"] = {"auth": "PRIVATE"}
        value["groups"][0]["members"] = ["usr_secret"]
        value["groups"][0]["events"][0]["secret"] = "PRIVATE"
        result = json.dumps(validated_report(value))
        for forbidden in ("PRIVATE", "usr_secret", "<b>"):
            self.assertNotIn(forbidden, result)

    def test_invalid_public_calendar_is_rejected(self):
        for change in ({"gen": 0}, {"gen": True}, {"groups": []}, {"groups": [REPORT["groups"][0]] * 25}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validated_report({**REPORT, **change})
        for change in ({"acc": "group"}, {"s": True}, {"e": 1}, {"cnt": -1}, {"q": 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validated_report({**REPORT, "groups": [{**GROUP, "events": [{**SAFE, **change}]}]})

    def test_naive_or_reversed_event_time_is_rejected(self):
        for change in ({"startsAt": "2026-10-29T10:00:00"}, {"endsAt": "2026-10-28T10:00:00Z"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalized_event({**EVENT, **change}, NOW)


class IncrementalCalendarTests(unittest.TestCase):
    def setUp(self):
        self.state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], NOW - 60, 1, 600)
        self.state["players"]["usr_one"]["seconds"] = 1234
        self.state["cookies"] = {"auth": "PRIVATE"}
        self.state["calendar"] = {"report": copy.deepcopy(REPORT)}
        self.client = Mock()
        self.client.call.return_value = {"results": [EVENT], "hasNext": False}

    def advance(self, now=NOW, groups=None):
        with patch("playtime.calendar.groups_config", return_value=groups or [GROUP]):
            self.state = advance(self.state, self.client, now)
        return self.state

    def test_two_reads_per_minute_same_session_and_only_complete_cycle_replaces_report(self):
        before = copy.deepcopy(self.state)
        self.advance()
        self.assertEqual(self.client.call.call_count, 2)
        self.assertEqual(self.state["calendar"]["report"], REPORT)
        self.assertEqual(status(self.state)["calendar_completed_queries"], 2)
        self.advance(NOW + 60)
        self.assertEqual(self.client.call.call_count, 3)
        self.assertEqual(self.state["calendar"]["status"], "ok")
        self.assertEqual(self.state["calendar"]["report"]["gen"], NOW)
        self.assertEqual(len(self.state["calendar"]["report"]["groups"][0]["events"]), 1)
        self.assertEqual({k:v for k,v in self.state.items() if k != "calendar"},
                         {k:v for k,v in before.items() if k != "calendar"})
        self.client.verify.assert_not_called()
        self.client.logout.assert_not_called()

    def test_next_cycle_waits_thirty_minutes_and_healthy_session_is_not_restarted(self):
        self.advance()
        self.advance(NOW + 60)
        self.client.reset_mock()
        self.advance(NOW + 1799)
        self.client.call.assert_not_called()
        self.advance(NOW + 1800)
        self.assertEqual(self.client.call.call_count, 2)

    def test_pages_resume_at_saved_offset_without_publishing_partial_data(self):
        self.client.call.side_effect = [{"results": [EVENT], "hasNext": True}, ApiFailure("time_budget")]
        self.advance()
        self.assertEqual(self.state["calendar"]["cycle"]["offset"], 1)
        self.assertEqual(self.state["calendar"]["report"], REPORT)
        self.client.call.side_effect = None
        self.advance(NOW + 60)
        self.assertEqual(self.client.call.call_args_list[2].args[2]["offset"], 1)

    def test_recurring_occurrences_are_not_deduplicated_by_id_alone(self):
        other = {**EVENT, "startsAt": "2026-10-30T10:00:00Z", "endsAt": "2026-10-30T12:00:00Z"}
        self.client.call.return_value = {"results": [EVENT, other], "hasNext": False}
        self.advance()
        self.advance(NOW + 60)
        self.assertEqual(len(self.state["calendar"]["report"]["groups"][0]["events"]), 2)

    def test_bad_page_never_replaces_previous_report_or_changes_hours(self):
        for body in ({}, {"results": [], "hasNext": True}, {"results": {}, "hasNext": False}):
            with self.subTest(body=body):
                self.state.pop("calendar", None)
                self.state["calendar"] = {"report": copy.deepcopy(REPORT)}
                self.client.call.return_value = body
                self.advance()
                self.assertEqual(self.state["calendar"]["status"], "invalid_response")
                self.assertEqual(self.state["calendar"]["report"], REPORT)
                self.assertEqual(self.state["players"]["usr_one"]["seconds"], 1234)
                self.assertEqual(self.state["status"], "ok")

    def test_transient_failure_preserves_hours_and_waits_before_retry(self):
        self.client.call.side_effect = ApiFailure("network_error")
        self.advance()
        self.assertEqual(self.state["status"], "ok")
        self.assertEqual(self.state["calendar"]["report"], REPORT)
        self.client.reset_mock()
        self.advance(NOW + 60)
        self.client.call.assert_not_called()

    def test_calendar_security_failures_honor_hard_stop_and_never_authorize_login(self):
        for failure in (ApiFailure("login_required", http_status=401), ApiFailure("access_denied", http_status=403),
                        ApiFailure("rate_limited", 7200, http_status=429),
                        ApiFailure("login_required", http_status=429, reason="email-location-verification")):
            with self.subTest(status=failure.status):
                self.state["calendar"] = {"report": copy.deepcopy(REPORT)}
                self.client.call.side_effect = failure
                self.client.reset_mock()
                self.advance()
                self.client.call.assert_called_once()
                self.assertTrue(self.state["auto_login_blocked"])
                self.assertFalse(self.state["session_recovery_pending"])
                self.assertIsNone(self.state["last_sample"])
                self.assertEqual(self.state["players"]["usr_one"]["seconds"], 1234)
                if failure.status == "rate_limited":
                    self.assertGreaterEqual(self.state["retry_at"], NOW + 7200)

    def test_missing_group_preserves_cached_events_and_marks_partial_without_fake_freshness(self):
        self.client.call.side_effect = [ApiFailure("api_error", http_status=404),
                                        {"results": [], "hasNext": False}]
        self.advance(groups=[GROUP, OTHER])
        self.client.call.side_effect = None
        self.client.call.return_value = {"results": [], "hasNext": False}
        self.advance(NOW + 60, groups=[GROUP, OTHER])
        calendar = self.state["calendar"]
        self.assertEqual(calendar["status"], "partial")
        self.assertEqual(calendar["report"]["groups"][0]["events"], [SAFE])
        self.assertEqual(calendar["report"]["gen"], REPORT["gen"])
        self.assertEqual(calendar["unavailable_groups"], [GROUP["id"]])

    def test_configuration_change_or_old_cycle_restarts_partial_collection(self):
        self.advance()
        self.advance(NOW + 60, groups=[OTHER])
        self.assertEqual(self.state["calendar"]["cycle"]["groups"], [OTHER])
        self.advance(NOW + 3700, groups=[OTHER])
        self.assertEqual(self.state["calendar"]["cycle"]["started"], NOW + 3700)

    def test_complete_empty_result_can_clear_expired_or_cancelled_events(self):
        self.client.call.return_value = {"results": [], "hasNext": False}
        self.advance()
        self.advance(NOW + 60)
        self.assertEqual(self.state["calendar"]["report"]["groups"][0]["events"], [])
        self.assertEqual(self.state["calendar"]["report"]["gen"], NOW)

    def test_global_event_cap_keeps_nearest_events(self):
        rows = [{**EVENT, "id": "cal_" + str(i)} for i in range(100)]
        self.client.call.return_value = {"results": rows, "hasNext": False}
        self.advance(groups=[GROUP, OTHER])
        self.advance(NOW + 60, groups=[GROUP, OTHER])
        self.advance(NOW + 120, groups=[GROUP, OTHER])
        self.assertEqual(sum(len(g["events"]) for g in self.state["calendar"]["report"]["groups"]), MAX_EVENTS)

    def test_lost_config_file_does_not_destroy_counter(self):
        with patch("playtime.calendar.groups_config", side_effect=OSError("unavailable")):
            result = advance(self.state, self.client, NOW)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["calendar"]["report"], REPORT)
        self.client.call.assert_not_called()


class CalendarIntegrationTests(unittest.TestCase):
    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_public_route_is_read_only_and_never_returns_checkpoint_fields(self, store):
        store.return_value.load.return_value = {"calendar": {"report": REPORT}, "cookies": {"auth": "PRIVATE"}}
        with patch("playtime.serverless.Client") as client:
            code, report = handle("GET", action="calendar")
        self.assertEqual(code, 200)
        self.assertEqual(report, REPORT)
        store.return_value.acquire.assert_not_called()
        store.return_value.save.assert_not_called()
        client.assert_not_called()

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_empty_calendar_does_not_publish_empty_success(self, store):
        store.return_value.load.return_value = {}
        self.assertEqual(handle("GET", action="calendar")[0], 503)

    @patch.dict("os.environ", ENV, clear=True)
    @patch("playtime.serverless.configured_store")
    def test_progress_diagnostics_are_admin_only(self, store):
        self.assertEqual(handle("POST", "Bearer " + CRON, "calendar-status")[0], 401)
        store.assert_not_called()
        store.return_value.load.return_value = {}
        self.assertEqual(handle("POST", "Bearer " + ADMIN, "calendar-status")[0], 200)

    def test_poll_samples_hours_before_calendar_and_persists_rotated_cookies(self):
        state = apply_sample(new_state("bot", CONFIG["world_id"]), [FRIEND], NOW - 60, 1, 600)
        state["cookies"] = {"auth": "before"}
        store = MemoryStore(state)
        client = Mock()
        client.friends.return_value = [FRIEND]
        client.accept_requests.return_value = 0
        client.cookies.side_effect = [{"auth": "before"}, {"auth": "after"}]
        client.call.return_value = {"results": [EVENT], "hasNext": False}
        with patch("playtime.serverless.Client", return_value=client), \
                patch("playtime.calendar.groups_config", return_value=[GROUP]), \
                patch("playtime.run.time.time", return_value=NOW), patch("playtime.serverless.login_client") as login:
            result = tick(store, {**CONFIG, "calendar_enabled": True}, "test@example.org", now=NOW)
        self.assertTrue(result["sampled"])
        self.assertEqual(store.state["players"]["usr_one"]["seconds"], 60)
        self.assertEqual(store.state["cookies"], {"auth": "after"})
        calls = [c[0] for c in client.mock_calls]
        self.assertLess(calls.index("friends"), calls.index("call"))
        self.assertEqual(len(store.saves), 2)
        login.assert_not_called()

    def test_failed_sample_does_not_start_calendar(self):
        state = new_state("bot", CONFIG["world_id"])
        state.update(status="ok", cookies={"auth": "cookie"})
        client = Mock()
        client.verify.side_effect = ApiFailure("network_error")
        client.cookies.return_value = {"auth": "cookie"}
        with patch("playtime.serverless.Client", return_value=client), patch("playtime.serverless.advance_calendar") as cal:
            tick(MemoryStore(state), {**CONFIG, "calendar_enabled": True}, "test@example.org", now=NOW)
        cal.assert_not_called()

    @patch("api.events.handle", return_value=(200, REPORT))
    def test_http_calendar_function_uses_only_the_read_only_action(self, execute):
        request = object.__new__(handler)
        request.wfile = io.BytesIO()
        request.send_response = Mock()
        request.send_header = Mock()
        request.end_headers = Mock()
        request.do_GET()
        execute.assert_called_once_with("GET", action="calendar")
        self.assertEqual(json.loads(request.wfile.getvalue()), REPORT)
        execute.reset_mock()
        request.do_POST()
        execute.assert_not_called()
        request.send_response.assert_called_with(405)


if __name__ == "__main__":
    unittest.main()
