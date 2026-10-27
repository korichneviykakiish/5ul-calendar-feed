import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from playtime.publish import MAX_BYTES, NoRedirects, fetch_report, validated_report


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
    def test_only_publisher_is_enabled_without_vrchat_credentials(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/publish-vercel-report.yml").read_text(encoding="utf-8")
        self.assertIn("python -m playtime.publish", workflow)
        self.assertIn("actions/deploy-pages@v5", workflow)
        self.assertIn("schedule:", workflow)
        for forbidden in ("secrets.", "VRC_", "playtime.run", "inputs:", "PLAYTIME_ENABLED"):
            self.assertNotIn(forbidden, workflow)
        self.assertFalse((root / ".github/workflows/playtime.yml").exists())


if __name__ == "__main__":
    unittest.main()
