"""Build a daily attention report from the imported Bilibili history database."""

from __future__ import annotations

import os
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


INTERACTION_BADGE = "互动补充"
INTERACTION_REMARK_PREFIX = "互动补充："
VIDEO_BUSINESSES = {"archive", "pgc"}
DEVICE_LABELS = {
    0: "其他",
    1: "手机",
    2: "Web",
    3: "手机",
    4: "Pad",
    5: "手机",
    6: "Pad",
    7: "手机",
    33: "TV",
}


class AttentionReportError(RuntimeError):
    """Raised when a daily attention report cannot be built."""


def _parse_date(target_date: Optional[date | str], timezone: ZoneInfo) -> date:
    if target_date is None:
        return datetime.now(timezone).date()
    if isinstance(target_date, datetime):
        return target_date.date()
    if isinstance(target_date, date):
        return target_date
    try:
        return date.fromisoformat(target_date)
    except (TypeError, ValueError) as exc:
        raise AttentionReportError("日期必须使用 YYYY-MM-DD 格式") from exc


def _get_timezone(timezone_name: Optional[str]) -> ZoneInfo:
    name = timezone_name or os.environ.get("TZ") or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise AttentionReportError(f"未知时区: {name}") from exc


def _open_readonly_database(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if not path.is_file():
        raise AttentionReportError(f"历史数据库不存在: {path}")

    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _require_history_table(connection: sqlite3.Connection, year: int) -> str:
    table_name = f"bilibili_history_{year}"
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    if row is None:
        raise AttentionReportError(f"未找到 {year} 年的历史记录表")

    required_columns = {
        "title", "part", "bvid", "page", "cid", "epid", "oid",
        "business", "dt", "author_name", "author_mid", "view_at", "progress",
        "duration", "is_finish", "tag_name", "main_category", "badge", "remark",
    }
    columns = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    missing = sorted(required_columns - columns)
    if missing:
        raise AttentionReportError(f"历史表缺少必要字段: {', '.join(missing)}")
    return table_name


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalized_progress(row: sqlite3.Row) -> int:
    duration = max(0, _to_int(row["duration"]))
    progress = _to_int(row["progress"])
    if _to_int(row["is_finish"]) == 1 or progress == -1:
        return duration
    return min(max(progress, 0), duration)


def _canonical_key(row: sqlite3.Row) -> str:
    business = str(row["business"] or "other").strip().lower()
    bvid = str(row["bvid"] or "").strip()

    if business == "archive" and bvid:
        cid = _to_int(row["cid"])
        part_key = f"cid:{cid}" if cid else f"page:{max(1, _to_int(row['page']))}"
        return f"archive:{bvid}:{part_key}"
    if business == "pgc":
        identifier = _to_int(row["epid"]) or _to_int(row["cid"]) or _to_int(row["oid"])
        return f"pgc:{identifier or str(row['title'] or '').strip()}"

    raise AttentionReportError(f"不支持的视频业务类型: {business}")


def _category(row: sqlite3.Row) -> str:
    main_category = str(row["main_category"] or "").strip()
    if main_category and main_category != "待定":
        return main_category
    return str(row["tag_name"] or row["business"] or "其他").strip() or "其他"


def _display_title(row: sqlite3.Row) -> str:
    title = str(row["title"] or "未命名视频").strip()
    part = str(row["part"] or "").strip()
    if part and part != title:
        return f"{title} · {part}"
    return title


def _format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{remaining_seconds}s"


def _ranked_groups(
    items: list[dict[str, Any]],
    label_field: str,
    top_n: int,
    identity_field: Optional[str] = None,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in items:
        label = item[label_field] or "未知"
        identity = item.get(identity_field) if identity_field else None
        group_key = f"{identity_field}:{identity}" if identity else f"name:{label}"
        group = groups.setdefault(
            group_key,
            {
                "name": label,
                "estimated_watched_seconds": 0,
                "video_count": 0,
            },
        )
        if identity_field:
            group[identity_field] = identity or None
        group["estimated_watched_seconds"] += item["estimated_watched_seconds"]
        group["video_count"] += 1

    ranked = list(groups.values())
    ranked.sort(
        key=lambda item: (item["estimated_watched_seconds"], item["video_count"]),
        reverse=True,
    )
    return ranked[:top_n]


def _render_report(report: dict[str, Any]) -> str:
    overview = report["overview"]
    lines = [
        "Bilibili Attention Report",
        report["date"],
        "",
        "今日概览",
        "----------------",
        f"观看历史记录   {overview['history_record_count']}",
        f"不同视频/分P   {overview['distinct_video_count']}",
        f"估算内容消费   {_format_duration(overview['estimated_watched_seconds'])}",
        f"视频总长度     {_format_duration(overview['total_video_duration_seconds'])}",
    ]

    if report["top_videos"]:
        lines.extend(["", "主要视频", "----------------"])
        for index, video in enumerate(report["top_videos"], start=1):
            completion = (
                f"{video['completion_percent']}%"
                if video["completion_percent"] is not None
                else "--"
            )
            lines.extend([
                f"{index}. 《{video['title']}》",
                (
                    f"   {_format_duration(video['estimated_watched_seconds'])} / "
                    f"{_format_duration(video['duration_seconds'])} · {completion}"
                ),
                f"   UP: {video['author'] or '未知'}",
            ])

    if report["top_authors"]:
        lines.extend(["", "UP 主", "----------------"])
        for author in report["top_authors"]:
            lines.append(
                f"{author['name']}  {_format_duration(author['estimated_watched_seconds'])}"
                f" · {author['video_count']}个视频"
            )

    if report["top_categories"]:
        lines.extend(["", "分区", "----------------"])
        for category in report["top_categories"]:
            lines.append(
                f"{category['name']}  {_format_duration(category['estimated_watched_seconds'])}"
            )

    if report["devices"]:
        lines.extend(["", "设备（按历史记录）", "----------------"])
        for device in report["devices"]:
            lines.append(
                f"{device['name']}  {device['history_record_count']}条记录"
                f" · {device['distinct_video_count']}个视频"
            )

    lines.extend([
        "",
        "注：时长来自最终观看进度估算，不代表真实屏幕使用时间。",
    ])
    return "\n".join(lines)


def build_daily_attention_report(
    db_path: str | os.PathLike[str],
    target_date: Optional[date | str] = None,
    timezone_name: Optional[str] = None,
    top_n: int = 5,
) -> dict[str, Any]:
    """Return structured daily metrics plus a ready-to-send text report.

    Repeated rows for the same playable item use the maximum normalized progress,
    which avoids double-counting resumed playback. The database is opened read-only.
    """
    if not 1 <= top_n <= 20:
        raise AttentionReportError("top_n 必须在 1 到 20 之间")

    timezone = _get_timezone(timezone_name)
    day = _parse_date(target_date, timezone)
    start_at = datetime.combine(day, time.min, tzinfo=timezone)
    end_at = start_at + timedelta(days=1)
    start_timestamp = int(start_at.timestamp())
    end_timestamp = int(end_at.timestamp())

    with closing(_open_readonly_database(db_path)) as connection:
        table_name = _require_history_table(connection, day.year)
        rows = connection.execute(
            f"""
            SELECT
                title, part, bvid, page, cid, epid, oid, business, dt,
                author_name, author_mid, view_at, progress, duration, is_finish,
                tag_name, main_category
            FROM {table_name}
            WHERE view_at >= ? AND view_at < ?
              AND NOT (COALESCE(badge, '') = ? OR COALESCE(remark, '') LIKE ?)
            ORDER BY view_at ASC
            """,
            (
                start_timestamp,
                end_timestamp,
                INTERACTION_BADGE,
                f"{INTERACTION_REMARK_PREFIX}%",
            ),
        ).fetchall()

    type_counts: Counter[str] = Counter()
    device_events: Counter[str] = Counter()
    device_videos: dict[str, set[str]] = defaultdict(set)
    video_items: dict[str, dict[str, Any]] = {}

    for row in rows:
        business = str(row["business"] or "other").strip().lower() or "other"
        type_counts[business] += 1

        if business not in VIDEO_BUSINESSES:
            continue

        key = _canonical_key(row)
        device = DEVICE_LABELS.get(_to_int(row["dt"]), f"未知({_to_int(row['dt'])})")
        device_events[device] += 1
        device_videos[device].add(key)

        watched_seconds = _normalized_progress(row)
        duration_seconds = max(0, _to_int(row["duration"]))
        candidate = {
            "key": key,
            "title": _display_title(row),
            "bvid": str(row["bvid"] or ""),
            "author": str(row["author_name"] or "未知").strip() or "未知",
            "author_mid": _to_int(row["author_mid"]),
            "category": _category(row),
            "estimated_watched_seconds": watched_seconds,
            "duration_seconds": duration_seconds,
            "event_count": 1,
        }
        existing = video_items.get(key)
        if existing is None:
            video_items[key] = candidate
            continue

        existing["event_count"] += 1
        existing["duration_seconds"] = max(existing["duration_seconds"], duration_seconds)
        if watched_seconds >= existing["estimated_watched_seconds"]:
            candidate["event_count"] = existing["event_count"]
            candidate["duration_seconds"] = existing["duration_seconds"]
            video_items[key] = candidate

    videos = list(video_items.values())
    for video in videos:
        duration_seconds = video["duration_seconds"]
        video["completion_percent"] = (
            min(100, round(video["estimated_watched_seconds"] / duration_seconds * 100))
            if duration_seconds > 0
            else None
        )
    videos.sort(
        key=lambda item: (item["estimated_watched_seconds"], item["duration_seconds"]),
        reverse=True,
    )

    estimated_watched_seconds = sum(item["estimated_watched_seconds"] for item in videos)
    total_video_duration_seconds = sum(item["duration_seconds"] for item in videos)
    devices = [
        {
            "name": name,
            "history_record_count": count,
            "distinct_video_count": len(device_videos[name]),
        }
        for name, count in device_events.items()
    ]
    devices.sort(key=lambda item: item["history_record_count"], reverse=True)

    report = {
        "date": day.isoformat(),
        "timezone": timezone.key,
        "definitions": {
            "estimated_watched_content": (
                "同日同一播放条目取最大有效 progress；完成记录按 duration；不代表真实屏幕时间"
            ),
            "distinct_video": "普通视频按 bvid+cid/page，剧集按 epid/cid/oid",
            "device": "按真实观看历史记录中的 dt 统计",
        },
        "overview": {
            "history_record_count": len(rows),
            "distinct_video_count": len(videos),
            "estimated_watched_seconds": estimated_watched_seconds,
            "total_video_duration_seconds": total_video_duration_seconds,
        },
        "type_counts": dict(sorted(type_counts.items())),
        "top_videos": videos[:top_n],
        "top_authors": _ranked_groups(
            videos,
            "author",
            top_n,
            identity_field="author_mid",
        ),
        "top_categories": _ranked_groups(videos, "category", top_n),
        "devices": devices,
    }
    report["subject"] = (
        f"B站注意力日报：{_format_duration(estimated_watched_seconds)} / "
        f"{len(videos)}个视频"
    )
    report["text"] = _render_report(report)
    return report
