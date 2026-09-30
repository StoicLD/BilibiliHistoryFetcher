import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud"))
import runner
from scripts.attention_storage import atomic_json, publish_reports, JobAlreadyRunning

DAY = "2026-09-29"


class CloudRunnerTests(unittest.TestCase):
    def setUp(self):
        self.recipient = patch.object(runner, "RECIPIENT", "recipient@example.test")
        self.recipient.start()
        self.addCleanup(self.recipient.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        runner.private_runtime_dirs(self.base)
        atomic_json(self.base / "state/run-ledger.json", runner.fresh_ledger())
        (self.base / "config").mkdir()
        shutil.copyfile(ROOT / "config/attention.deployment.example.json", self.base / "config/attention.deployment.json")
        self.report = {"date": DAY, "timezone": "Asia/Shanghai", "subject": "SYNTHETIC TEST",
                       "brief": "SYNTHETIC TEST\n主要主题：\nAI 1个", "markdown": "SYNTHETIC TEST",
                       "sync_status": "success", "generated_at": "2026-09-30T07:30:00+08:00"}
        self.result = {"status": "success", "ok": True, "result": {"dates": [DAY]},
                       "started_at": "2026-09-30T07:30:00+08:00", "finished_at": "2026-09-30T07:30:01+08:00"}

    def generate(self):
        publish_reports(self.base / "data", [self.report])
        with patch.object(runner, "credential_metadata"), patch.object(runner, "run_job", return_value=self.result):
            return runner.prepare(self.base, DAY, self.base / "DO-NOT-READ.env")

    def test_previous_day_uses_shanghai_calendar(self):
        self.assertEqual(runner.report_date(now=datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc)), DAY)
        self.assertEqual(runner.report_date(now=datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)), "2026-09-30")

    def test_recipient_is_required_and_plain(self):
        for recipient in ("", "Name <person@example.test>", "person@example.test OR in:anywhere", "two@example.test,other@example.test"):
            with self.subTest(recipient=recipient), patch.object(runner, "RECIPIENT", recipient):
                with self.assertRaises(runner.Refused):
                    runner.fresh_ledger()
                with self.assertRaises(runner.Refused):
                    runner.gmail_query(DAY)

    def test_ledger_initialization_never_overwrites_existing_history(self):
        before = (self.base / "state/run-ledger.json").read_bytes()
        with self.assertRaises(runner.Refused):
            runner.initialize_ledger(self.base)
        self.assertEqual((self.base / "state/run-ledger.json").read_bytes(), before)

    def test_new_deployment_initialization(self):
        fresh = self.base / "new-deployment"
        result = runner.initialize_ledger(fresh)
        self.assertEqual(result["status"], "initialized")
        self.assertEqual(runner.load_ledger(fresh / "state/run-ledger.json"), runner.fresh_ledger())

    def test_non_object_ledger_refused(self):
        atomic_json(self.base / "state/run-ledger.json", [])
        with self.assertRaises(runner.Refused):
            runner.load_ledger(self.base / "state/run-ledger.json")

    @unittest.skipIf(os.name == "nt", "POSIX directory permissions")
    def test_private_runtime_directories_created_and_unsafe_existing_refused(self):
        fresh = self.base / "separate-volume"
        runner.private_runtime_dirs(fresh)
        for name in ("data", "state"):
            self.assertEqual((fresh / name).stat().st_mode & 0o777, 0o700)
        (fresh / "data").chmod(0o755)
        with self.assertRaises(runner.Refused):
            runner.private_runtime_dirs(fresh)

    def test_non_object_payload_refused(self):
        self.generate()
        atomic_json(self.base / "data/outbox" / f"{DAY}.json", [])
        with self.assertRaises(runner.Refused):
            runner.read_payload(self.base, DAY)
        with self.assertRaises(runner.Refused):
            runner.prepare(self.base, DAY, self.base / "missing")

    def test_current_or_future_date_refused(self):
        with self.assertRaises(runner.Refused):
            runner.report_date("2026-09-30", now=datetime(2026, 9, 30, 0, tzinfo=timezone.utc))

    def test_missing_ledger_requires_restore(self):
        (self.base / "state/run-ledger.json").unlink()
        with self.assertRaises(runner.Refused):
            runner.load_ledger(self.base / "state/run-ledger.json")

    def test_missing_credentials_fail_before_job(self):
        with patch.object(runner, "run_job") as job, self.assertRaises(runner.Refused):
            runner.prepare(self.base, DAY, self.base / "MISSING.env")
        job.assert_not_called()

    def test_unrelated_recipient_ledger_refused(self):
        ledger = runner.fresh_ledger()
        ledger["recipient"] = "someone@example.test"
        atomic_json(self.base / "state/run-ledger.json", ledger)
        with self.assertRaises(runner.Refused):
            runner.load_ledger(self.base / "state/run-ledger.json")

    def test_success_creates_exact_dated_payload(self):
        result = self.generate()
        self.assertEqual(result["status"], "ready")
        payload = runner.read_payload(self.base, DAY)
        self.assertEqual(payload["to"], [runner.RECIPIENT])
        self.assertIn("BILI-ATTENTION-2026-09-29", payload["subject"])
        self.assertEqual(payload["body"], self.report["brief"])
        ledger = (self.base / "state/run-ledger.json").read_text()
        self.assertNotIn(self.report["brief"], ledger)
        self.assertNotIn("SYNTHETIC TEST", ledger)

    def test_ready_retry_reuses_payload_without_fetch(self):
        self.generate()
        with patch.object(runner, "run_job") as job:
            self.assertEqual(runner.prepare(self.base, DAY, self.base / "missing")["status"], "ready")
        job.assert_not_called()

    def test_job_overlap_cannot_send_old_success(self):
        publish_reports(self.base / "data", [self.report])
        with patch.object(runner, "credential_metadata"), patch.object(runner, "run_job", side_effect=JobAlreadyRunning), self.assertRaises(runner.Refused):
            runner.prepare(self.base, DAY, self.base / "DO-NOT-READ.env")
        self.assertFalse((self.base / "data/outbox" / (DAY + ".json")).exists())

    def test_failed_job_cannot_send_old_success(self):
        publish_reports(self.base / "data", [self.report])
        with patch.object(runner, "credential_metadata"), patch.object(runner, "run_job", return_value={"status": "failed", "ok": False}), self.assertRaises(runner.Refused):
            runner.prepare(self.base, DAY, self.base / "DO-NOT-READ.env")

    def test_success_must_contain_target_date(self):
        bad = copy.deepcopy(self.result)
        bad["result"]["dates"] = ["2026-09-28"]
        with self.assertRaises(runner.Refused):
            runner.prepare_payload(self.base / "data", DAY, bad)

    def test_stale_failed_or_naive_report_cannot_be_prepared(self):
        for field, value in [("sync_status", "failed"), ("generated_at", "2026-09-30T07:00:00+08:00"),
                             ("generated_at", "2026-09-30T07:31:00+08:00"),
                             ("generated_at", "2026-09-30T07:30:00")]:
            publish_reports(self.base / "data", [{**self.report, field: value}])
            with self.assertRaises(runner.Refused):
                runner.prepare_payload(self.base / "data", DAY, self.result)

    def test_unknown_existing_delivery_state_is_not_overwritten(self):
        ledger = runner.fresh_ledger()
        ledger["deliveries"][DAY] = {"status": "unknown-restored-state"}
        atomic_json(self.base / "state/run-ledger.json", ledger)
        with patch.object(runner, "run_job") as job, self.assertRaises(runner.Refused):
            runner.prepare(self.base, DAY, self.base / "DO-NOT-READ.env")
        job.assert_not_called()
        self.assertEqual(json.loads((self.base / "state/run-ledger.json").read_text())["deliveries"][DAY]["status"], "unknown-restored-state")

    def test_report_date_timezone_and_traversal_validated(self):
        for field, value in [("date", "2026-09-28"), ("timezone", "UTC")]:
            report = {**self.report, field: value}
            atomic_json(self.base / "data/reports/example.json", report)
            atomic_json(self.base / "data/reports/index.json", {"dates": {DAY: "example.json"}})
            with self.assertRaises(runner.Refused):
                runner.prepare_payload(self.base / "data", DAY, self.result)
        atomic_json(self.base / "data/reports/index.json", {"dates": {DAY: "../../config/attention.deployment.json"}})
        with self.assertRaises(runner.Refused):
            runner.prepare_payload(self.base / "data", DAY, self.result)

    def test_second_claim_refused_until_reconciled(self):
        self.generate()
        claim = runner.change_delivery(self.base, DAY, "claim")
        self.assertEqual(claim["status"], "sending")
        with self.assertRaises(runner.Refused):
            runner.change_delivery(self.base, DAY, "claim")
        with self.assertRaises(runner.Refused):
            runner.prepare(self.base, DAY, self.base / "missing")

    def test_success_requires_matching_attempt_and_gmail_id(self):
        self.generate()
        claim = runner.change_delivery(self.base, DAY, "claim")
        with self.assertRaises(runner.Refused):
            runner.change_delivery(self.base, DAY, "confirm-sent", attempt_id="wrong", message_id="fake-fixture-id")
        with self.assertRaises(runner.Refused):
            runner.change_delivery(self.base, DAY, "confirm-sent", attempt_id=claim["attempt_id"])
        result = runner.change_delivery(self.base, DAY, "confirm-sent", attempt_id=claim["attempt_id"], message_id="fake-fixture-id")
        self.assertEqual(result["status"], "sent")
        with patch.object(runner, "run_job") as job:
            self.assertEqual(runner.prepare(self.base, DAY, self.base / "missing")["status"], "sent")
        job.assert_not_called()

    def test_uncertain_must_not_retry_automatically(self):
        self.generate()
        claim = runner.change_delivery(self.base, DAY, "claim")
        runner.change_delivery(self.base, DAY, "mark-uncertain", attempt_id=claim["attempt_id"])
        with self.assertRaises(runner.Refused):
            runner.change_delivery(self.base, DAY, "claim")
        result = runner.change_delivery(self.base, DAY, "reconcile-sent", message_id="fake-fixture-id")
        self.assertEqual(result["status"], "sent")

    def test_definitive_failure_can_be_reclaimed_with_new_attempt(self):
        self.generate()
        claim = runner.change_delivery(self.base, DAY, "claim")
        runner.change_delivery(self.base, DAY, "mark-failed", attempt_id=claim["attempt_id"])
        newer = runner.change_delivery(self.base, DAY, "claim")
        self.assertNotEqual(claim["attempt_id"], newer["attempt_id"])

    def test_payload_tampering_refused(self):
        self.generate()
        path = self.base / "data/outbox" / (DAY + ".json")
        payload = json.loads(path.read_text())
        payload["body"] = "changed"
        atomic_json(path, payload)
        with self.assertRaises(runner.Refused):
            runner.read_payload(self.base, DAY)

    def test_reconciliation_can_restore_missing_receipt(self):
        result = runner.change_delivery(self.base, DAY, "reconcile-sent", message_id="fake-fixture-id")
        self.assertEqual(result["status"], "sent")
        with self.assertRaises(runner.Refused):
            runner.change_delivery(self.base, DAY, "claim")


if __name__ == "__main__":
    unittest.main()
