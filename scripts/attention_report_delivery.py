"""Deliver a generated attention report through existing notification providers."""

from typing import Any

from scripts.apprise_notify import send_apprise_notification
from scripts.send_log_email import send_email


async def deliver_attention_report(
    report: dict[str, Any],
    *,
    send_email_report: bool,
    send_apprise_report: bool,
) -> dict[str, Any]:
    """Send a report and return a scheduler-compatible status payload."""
    if not send_email_report and not send_apprise_report:
        return {"status": "error", "message": "至少需要启用一个通知渠道"}

    notifications = {}
    successful_channels = 0

    if send_email_report:
        email_result = await send_email(report["subject"], report["text"])
        notifications["email"] = email_result
        if email_result.get("status") == "success":
            successful_channels += 1

    if send_apprise_report:
        apprise_result = await send_apprise_notification(
            title=report["subject"],
            body=report["text"],
        )
        notifications["apprise"] = apprise_result
        if apprise_result.get("status") in {"success", "partial"}:
            successful_channels += 1

    if successful_channels == 0:
        return {
            "status": "error",
            "message": "注意力日报已生成，但通知发送失败",
            "data": report,
            "notifications": notifications,
        }

    return {
        "status": "success",
        "message": "注意力日报发送成功",
        "data": report,
        "notifications": notifications,
    }
