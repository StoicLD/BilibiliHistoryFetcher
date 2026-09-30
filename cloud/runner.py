"""Generate a daily Bilibili mail payload; Gmail remains an agent-side action.

Mail delivery and scheduling are external integrations; this module never sends mail.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import stat
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from scripts.attention_config import load_job_config
from scripts.attention_job import run_job
from scripts.attention_storage import atomic_json, job_lock, JobAlreadyRunning, read_index

RECIPIENT = os.environ.get("BILI_ATTENTION_RECIPIENT", "")
TZ_NAME = "Asia/Shanghai"
TZ = ZoneInfo(TZ_NAME)
DELIVERY_STATES = {"ready", "sending", "sent", "failed", "uncertain"}


class Refused(RuntimeError):
    """A safe, non-secret diagnostic suitable for the orchestrating agent."""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def private_runtime_dirs(base):
    for name in ("data", "state"):
        path = base / name
        if path.is_symlink():
            raise Refused("Runtime directories must not be symbolic links")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise Refused("Runtime path must be a directory")
        if os.name != "nt" and (stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid()):
            raise Refused("Runtime directories must be owned by this user with owner-only permissions (0700)")


def report_date(value=None, now=None):
    yesterday = (now or datetime.now(TZ)).astimezone(TZ).date() - timedelta(days=1)
    selected = date.fromisoformat(value) if value else yesterday
    if selected > yesterday:
        raise Refused("Only a completed Shanghai calendar day may be reported")
    return selected.isoformat()


def marker(day):
    return "BILI-ATTENTION-" + day


def validate_recipient():
    # A plain mailbox only: no display names, spaces or Gmail query operators.
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", RECIPIENT):
        raise Refused("Set BILI_ATTENTION_RECIPIENT to one plain email address")


def gmail_query(day):
    validate_recipient()
    return f'in:sent to:{RECIPIENT} subject:"{marker(day)}"'


def fresh_ledger():
    validate_recipient()
    return {"schema_version": 1, "timezone": TZ_NAME, "recipient": RECIPIENT,
            "policy": "one-email-per-report-date; no automatic refresh/backfill resends",
            "deliveries": {}}


def load_ledger(path):
    validate_recipient()
    if not path.exists():
        raise Refused("Run ledger is missing: restore its latest durable backup before continuing")
    result = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(result, dict) or result.get("schema_version") != 1 or result.get("timezone") != TZ_NAME
            or result.get("recipient") != RECIPIENT or not isinstance(result.get("deliveries"), dict)):
        raise Refused("Run ledger identity or schema does not match this deployment")
    for key, entry in result["deliveries"].items():
        if not isinstance(entry, dict) or entry.get("status") not in DELIVERY_STATES:
            raise Refused("Run ledger contains an unknown delivery state; reconcile before continuing")
        if date.fromisoformat(key).isoformat() != key:
            raise Refused("Run ledger contains a non-canonical report date")
    return result


def credential_metadata(path):
    # Never print or return credential bytes. Refuse unsafe files before the job reads them.
    if path.is_symlink():
        raise Refused("Credential file must not be a symbolic link")
    try:
        info = path.stat()
    except FileNotFoundError:
        raise Refused("Credential file is missing; user must enter it through the cloud terminal") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise Refused("Credential file must be a nonempty regular file")
    if os.name != "nt" and (stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid()):
        raise Refused("Credential file must be owned by this user and accessible only to its owner")
    if os.name != "nt" and (stat.S_IMODE(path.parent.stat().st_mode) & 0o077):
        raise Refused("Credential directory must be accessible only to its owner")


def prepare_payload(output, day, result):
    if result.get("status") != "success" or result.get("ok") is not True:
        raise Refused("Generation failed; no email payload was prepared")
    if day not in result.get("result", {}).get("dates", []):
        raise Refused("Successful run did not generate the requested date")
    reports = (output / "reports").resolve()
    relative = read_index(output).get("dates", {}).get(day)
    if not relative:
        raise Refused("Requested report was not published")
    path = (reports / relative).resolve()
    if not path.is_relative_to(reports):
        raise Refused("Report index contains an out-of-bounds path")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise Refused("Published report must be a JSON object")
    if report.get("date") != day or report.get("timezone") != TZ_NAME:
        raise Refused("Published report date or timezone does not match the request")
    if report.get("sync_status") != "success":
        raise Refused("Published report does not certify a successful synchronization")
    timestamps = [datetime.fromisoformat(value) for value in
                  (result["started_at"], report["generated_at"], result["finished_at"])]
    if any(value.tzinfo is None or value.utcoffset() is None for value in timestamps):
        raise Refused("Report/run timestamps must be timezone-aware")
    if not timestamps[0] <= timestamps[1] <= timestamps[2]:
        raise Refused("Published report was not generated during this successful run")
    if not isinstance(report.get("subject"), str) or not isinstance(report.get("brief"), str):
        raise Refused("Published report is missing its subject or brief")
    content = {"to": [RECIPIENT], "subject": report["subject"] + f" [{marker(day)}]",
               "body": report["brief"], "report_date": day, "timezone": TZ_NAME,
               "marker": marker(day)}
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {**content, "content_sha256": digest}


def entry_summary(day, entry):
    result = {"report_date": day, "status": entry["status"],
              "gmail_preflight_query": gmail_query(day)}
    for key in ("content_sha256", "attempt_id", "gmail_message_id"):
        if key in entry:
            result[key] = entry[key]
    return result


def prepare(base, day, credentials, explicit_date=False):
    private_runtime_dirs(base)
    ledger_path = base / "state/run-ledger.json"
    with job_lock(base / "state/orchestrator-lock"):
        ledger = load_ledger(ledger_path)
        old = ledger["deliveries"].get(day)
        if old and old["status"] == "sent":
            return entry_summary(day, old)
        if old and old["status"] in {"sending", "uncertain"}:
            raise Refused("Delivery outcome needs Gmail reconciliation; do not send again")
        outbox = base / "data/outbox" / f"{day}.json"
        if old and old["status"] in {"ready", "failed"} and outbox.exists():
            payload = json.loads(outbox.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("content_sha256") != old.get("content_sha256"):
                raise Refused("Saved payload and ledger digest disagree")
            return {**entry_summary(day, old), "payload_path": str(outbox)}
        credential_metadata(credentials)
        config = load_job_config(base / "config/attention.deployment.json")
        if config["timezone"] != TZ_NAME:
            raise Refused("Deployment timezone must remain Asia/Shanghai")
        config["output_dir"] = str(base / "data")
        try:
            result = run_job(config, credentials, day if explicit_date else None)
        except JobAlreadyRunning:
            raise Refused("Generation is already running; no fresh payload was prepared") from None
        payload = prepare_payload(base / "data", day, result)
        atomic_json(outbox, payload)
        entry = {"status": "ready", "marker": marker(day), "content_sha256": payload["content_sha256"],
                 "prepared_at": now_iso(), "generation_started_at": result["started_at"],
                 "generation_finished_at": result["finished_at"]}
        ledger["deliveries"][day] = entry
        atomic_json(ledger_path, ledger)
        return {**entry_summary(day, entry), "payload_path": str(outbox)}


def change_delivery(base, day, operation, *, attempt_id=None, message_id=None):
    """CAS-like local state. External mail preflights and durable version-guarded claims remain mandatory."""
    private_runtime_dirs(base)
    path = base / "state/run-ledger.json"
    with job_lock(base / "state/orchestrator-lock"):
        ledger = load_ledger(path)
        entry = ledger["deliveries"].get(day)
        if operation == "reconcile-sent":
            if not message_id or not message_id.strip():
                raise Refused("Verified Gmail message ID is required")
            if entry is None:
                entry = {"marker": marker(day)}
                ledger["deliveries"][day] = entry
            entry.update(status="sent", gmail_message_id=message_id, reconciled_at=now_iso())
        elif operation == "claim":
            if entry is None or entry["status"] not in {"ready", "failed"}:
                raise Refused("Payload is not claimable; reconcile ambiguous or sent state first")
            entry.update(status="sending", attempt_id=uuid.uuid4().hex, attempted_at=now_iso())
        elif operation == "confirm-sent":
            if not entry or entry.get("attempt_id") != attempt_id or entry["status"] not in {"sending", "uncertain"}:
                raise Refused("Gmail result does not match an active delivery attempt")
            if not message_id or not message_id.strip():
                raise Refused("Successful Gmail message ID is required")
            entry.update(status="sent", gmail_message_id=message_id, sent_at=now_iso())
        elif operation in {"mark-failed", "mark-uncertain"}:
            if not entry or entry.get("attempt_id") != attempt_id or entry["status"] != "sending":
                raise Refused("Failure does not match an active delivery attempt")
            entry.update(status="failed" if operation == "mark-failed" else "uncertain", outcome_at=now_iso())
        else:
            raise Refused("Unknown ledger operation")
        atomic_json(path, ledger)
        return entry_summary(day, entry)


def read_payload(base, day):
    entry = load_ledger(base / "state/run-ledger.json")["deliveries"].get(day)
    if not entry or entry["status"] not in {"ready", "failed", "sending"}:
        raise Refused("No sendable payload exists for this date")
    payload = json.loads((base / "data/outbox" / f"{day}.json").read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise Refused("Saved mail payload must be a JSON object")
    digest = payload.pop("content_sha256")
    calculated = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if (digest != calculated or digest != entry.get("content_sha256") or payload.get("to") != [RECIPIENT]
            or payload.get("report_date") != day or payload.get("marker") != marker(day)):
        raise Refused("Saved mail payload failed its integrity check")
    return {**payload, "content_sha256": digest}


def initialize_ledger(base):
    """Only for a brand-new deployment; never replace a missing delivery history."""
    ledger = fresh_ledger()
    private_runtime_dirs(base)
    with job_lock(base / "state/orchestrator-lock"):
        path = base / "state/run-ledger.json"
        try:
            with path.open("x", encoding="utf-8") as stream:
                json.dump(ledger, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise Refused("Run ledger already exists; initialization never overwrites it") from None
    return {"status": "initialized", "recipient": RECIPIENT, "timezone": TZ_NAME}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["init-ledger", "status", "prepare", "payload", "claim", "confirm-sent",
                                      "reconcile-sent", "mark-failed", "mark-uncertain"])
    p.add_argument("--date", help="Manual report date; default yesterday in Shanghai")
    p.add_argument("--base", type=Path, default=BASE)
    p.add_argument("--credentials", type=Path)
    p.add_argument("--attempt-id")
    p.add_argument("--gmail-message-id")
    args = p.parse_args()
    base = args.base.resolve()
    try:
        day = report_date(args.date)
        if args.command == "init-ledger":
            result = initialize_ledger(base)
        elif args.command == "status":
            ledger = load_ledger(base / "state/run-ledger.json")
            result = {"report_date": day, "entry": ledger["deliveries"].get(day),
                      "gmail_preflight_query": gmail_query(day), "credentials_present": (base / "secrets/bilibili.env").is_file()}
        elif args.command == "prepare":
            result = prepare(base, day, args.credentials or base / "secrets/bilibili.env", args.date is not None)
        elif args.command == "payload":
            result = read_payload(base, day)
        else:
            result = change_delivery(base, day, args.command, attempt_id=args.attempt_id,
                                     message_id=args.gmail_message_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Refused, JobAlreadyRunning) as exc:
        print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False))
        return 2
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__,
                          "message": "Local configuration, ledger, or payload is invalid; no send authorized"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
