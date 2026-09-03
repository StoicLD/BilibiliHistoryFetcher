"""HTTP adapter for daily Bilibili attention reports."""

from datetime import date
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from scripts.attention_report import AttentionReportError, build_daily_attention_report
from scripts.attention_report_delivery import deliver_attention_report
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
    try:
        report = _build_report(request.date, request.timezone, request.top_n)
    except AttentionReportError as exc:
        return {"status": "error", "message": str(exc)}

    return await deliver_attention_report(
        report,
        send_email_report=request.send_email_report,
        send_apprise_report=request.send_apprise_report,
    )
