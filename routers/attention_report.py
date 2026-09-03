"""HTTP adapter for daily Bilibili attention reports."""

from datetime import date
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from scripts.apprise_notify import send_apprise_notification
from scripts.attention_report import AttentionReportError, build_daily_attention_report
from scripts.send_log_email import send_email
from scripts.utils import get_output_path, load_config


router = APIRouter()


class AttentionReportRequest(BaseModel):
    date: Optional[date] = None
    timezone: Optional[str] = None
    top_n: int = Field(5, ge=1, le=20)
    send_email_report: bool = True
    send_apprise_report: bool = False


def _build_report(target_date: Optional[date], timezone: Optional[str], top_n: int) -> dict:
    config = load_config()
    return build_daily_attention_report(
        db_path=get_output_path(config["db_file"]),
        target_date=target_date,
        timezone_name=timezone,
        top_n=top_n,
    )


@router.get("/daily-report", summary="预览B站注意力日报")
async def get_daily_attention_report(
    date: Optional[date] = None,
    timezone: Optional[str] = None,
    top_n: int = 5,
):
    try:
        report = _build_report(date, timezone, top_n)
        return {"status": "success", "data": report}
    except AttentionReportError as exc:
        return {"status": "error", "message": str(exc)}


@router.post("/daily-report/send", summary="生成并发送B站注意力日报")
async def send_daily_attention_report(request: AttentionReportRequest):
    if not request.send_email_report and not request.send_apprise_report:
        return {"status": "error", "message": "至少需要启用一个通知渠道"}

    try:
        report = _build_report(request.date, request.timezone, request.top_n)
    except AttentionReportError as exc:
        return {"status": "error", "message": str(exc)}

    notifications = {}
    successful_channels = 0

    if request.send_email_report:
        email_result = await send_email(report["subject"], report["text"])
        notifications["email"] = email_result
        if email_result.get("status") == "success":
            successful_channels += 1

    if request.send_apprise_report:
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
