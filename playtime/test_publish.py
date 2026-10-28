import copy
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import Mock, patch

from playtime.publish import MAX_BYTES, NoRedirects, fetch_report, validated_report, publish_calendar, CALENDAR_SOURCE


class PublicReportTests(unittest.TestCase):
    def setUp(self):
        self.report = {"updated": "2026-10-27 12:00 UTC", "status": "ok", "estimated": True,
                       "top": [{"rank": 1, "name": "Player", "seconds": 60}]}

    def test_only_public_fields_are_forwarded(self):
        report = copy.deepcopy(self.report)
        report["cookies"] = {"auth": "PRIVATE"}
        report["top"][0]["id"] = "usr_private"
        safe = validated_report(report)
        self.assertNotIn("PRIVATE", json.dumps(safe))
        self.assertNotIn("usr_private", json.dumps(safe))
        self.assertEqual(safe, self.report)

    def test_empty_new_counter_is_valid_without_old_hours(self):
        report = {"updated": "", "status": "waiting_for_login", "estimated": True, "top": []}
        self.assertEqual(validated_report(report), report)

    def test_failures_are_published_as_failures_not_relabelled_ok(self):
        report = {**self.report, "status": "login_required"}
        self.assertEqual(validated_report(report)["status"], "login_required")

    def test_invalid_shape_timestamp_or_status_is_rejected(self):
        for changes in ({"updated": "PRIVATE"}, {"status": "<script>"}, {"estimated": False},
                        {"top": {}}, {"top": self.report["top"] * 121}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validated_report({**self.report, **changes})

    def test_invalid_or_markup_rows_are_rejected(self):
        for changes in ({"seconds": -1}, {"seconds": True}, {"rank": 2}, {"name": "<b>Player</b>"},
                        {"name": "\nPRIVATE"}, {"name": ""}, {"name": "x" * 65}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validated_report({**self.report, "top": [{**self.report["top"][0], **changes}]})

    def test_cross_domain_redirects_are_never_followed(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, 302, "", {}, "https://unrelated.example"))

    @patch("playtime.publish.build_opener")
    def test_fetch_is_bounded_and_has_no_vrchat_credentials(self, factory):
        response = Mock(status=200)
        response.read.return_value = json.dumps(self.report).encode()
        factory.return_value.open.return_value.__enter__ = Mock(return_value=response)
        factory.return_value.open.return_value.__exit__ = Mock(return_value=False)
        self.assertEqual(fetch_report(), self.report)
        request = factory.return_value.open.call_args.args[0]
        self.assertFalse(request.has_header("Authorization"))
        response.read.assert_called_once_with(MAX_BYTES + 1)

    @patch("playtime.publish.build_opener")
    def test_oversized_report_is_rejected_before_publication(self, factory):
        response = Mock(status=200)
        response.read.return_value = b"x" * (MAX_BYTES + 1)
        factory.return_value.open.return_value.__enter__ = Mock(return_value=response)
        factory.return_value.open.return_value.__exit__ = Mock(return_value=False)
        with self.assertRaises(ValueError):
            fetch_report()


class PublicationWorkflowTests(unittest.TestCase):
    def workflow(self):
        root = Path(__file__).resolve().parents[1]
        return (root / ".github/workflows/publish-vercel-report.yml").read_text(encoding="utf-8")

    def test_only_publisher_is_enabled_without_vrchat_credentials(self):
        root = Path(__file__).resolve().parents[1]
        workflow = self.workflow()
        self.assertIn("python -m playtime.publish", workflow)
        self.assertIn("actions/deploy-pages@v5", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("schedule:", workflow)
        for forbidden in ("secrets.", "VRC_", "playtime.run", "inputs:", "PLAYTIME_ENABLED"):
            self.assertNotIn(forbidden, workflow)
        self.assertFalse((root / ".github/workflows/playtime.yml").exists())
        self.assertIn("git add docs/playtime.json docs/events.json", workflow)

    def test_pages_retries_only_failed_deployment_once_with_bounded_waits(self):
        workflow = self.workflow()
        self.assertEqual(workflow.count("uses: actions/deploy-pages@v5"), 2)
        self.assertEqual(workflow.count("timeout: 90000"), 2)
        self.assertEqual(workflow.count("continue-on-error: true"), 1)
        self.assertIn("id: deployment\n        continue-on-error: true", workflow)
        self.assertIn("name: Wait before one Pages publication retry\n"
                      "        if: ${{ steps.deployment.outcome == 'failure' }}\n"
                      "        run: sleep 20", workflow)
        self.assertIn("id: deployment_retry\n"
                      "        if: ${{ steps.deployment.outcome == 'failure' }}\n"
                      "        uses: actions/deploy-pages@v5", workflow)
        self.assertIn("group: calendar-feed\n  cancel-in-progress: false", workflow)

    def test_publication_is_not_green_when_both_attempts_fail(self):
        workflow = self.workflow()
        self.assertIn("name: Verify Pages publication\n"
                      "        if: ${{ always() && !cancelled() }}", workflow)
        self.assertIn('run: test "$PRIMARY_OUTCOME" = success || test "$RETRY_OUTCOME" = success', workflow)
        self.assertIn("PRIMARY_OUTCOME: ${{ steps.deployment.outcome }}", workflow)
        self.assertIn("RETRY_OUTCOME: ${{ steps.deployment_retry.outcome }}", workflow)
        self.assertIn("url: ${{ steps.deployment_retry.outputs.page_url || steps.deployment.outputs.page_url }}", workflow)


class PublicCalendarPublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "events.json"
        self.report = {"gen": 1793178000, "groups": [{"id": "grp_56fe79fc-ca06-4b67-858b-e4ec572afe5f",
                                                    "name": "Public", "events": []}]}
        self.path.write_text(json.dumps(self.report), encoding="utf-8")

    @patch("playtime.publish.fetch_json")
    def test_valid_calendar_uses_public_url_and_strips_extra_fields(self, fetch):
        fetch.return_value = {**self.report, "gen": self.report["gen"] + 60, "cookies": "PRIVATE"}
        self.assertTrue(publish_calendar(self.path))
        fetch.assert_called_once_with(CALENDAR_SOURCE)
        self.assertNotIn("PRIVATE", self.path.read_text())

    @patch("playtime.publish.fetch_json")
    def test_network_error_or_incomplete_cycle_preserves_previous_file(self, fetch):
        original = self.path.read_bytes()
        for result in (None, {"status": "calendar_not_ready"}):
            fetch.return_value = result
            self.assertFalse(publish_calendar(self.path))
            self.assertEqual(self.path.read_bytes(), original)
        fetch.side_effect = OSError("network")
        self.assertFalse(publish_calendar(self.path))
        self.assertEqual(self.path.read_bytes(), original)

    @patch("playtime.publish.fetch_json")
    def test_stale_backend_cannot_replace_newer_published_calendar(self, fetch):
        original = self.path.read_bytes()
        fetch.return_value = {**self.report, "gen": self.report["gen"] - 1}
        self.assertFalse(publish_calendar(self.path))
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
