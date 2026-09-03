import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import routers.attention_report as route_mod
from scripts.attention_report import AttentionReportError, build_daily_attention_report


SCHEMA = """
CREATE TABLE bilibili_history_2026 (
    title TEXT, part TEXT, bvid TEXT, page INTEGER, cid INTEGER,
    epid INTEGER, oid INTEGER, kid INTEGER, business TEXT, dt INTEGER,
    author_name TEXT, author_mid INTEGER, view_at INTEGER, progress INTEGER,
    duration INTEGER, is_finish INTEGER, tag_name TEXT, main_category TEXT,
    badge TEXT, remark TEXT
)
"""


class AttentionReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.db"
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute(SCHEMA)
        self.timezone = ZoneInfo("Asia/Singapore")
        self.day = date(2026, 9, 3)

    def tearDown(self):
        self.connection.close()
        self.temp_dir.cleanup()

    def add_row(self, **overrides):
        row = {
            "title": "视频",
            "part": "",
            "bvid": "BV-default",
            "page": 1,
            "cid": 1,
            "epid": 0,
            "oid": 1,
            "kid": 0,
            "business": "archive",
            "dt": 2,
            "author_name": "UP主",
            "author_mid": 1,
            "view_at": int(datetime(2026, 9, 3, 12, tzinfo=self.timezone).timestamp()),
            "progress": 0,
            "duration": 300,
            "is_finish": 0,
            "tag_name": "科技",
            "main_category": "知识",
            "badge": "",
            "remark": "",
        }
        row.update(overrides)
        columns = list(row)
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO bilibili_history_2026 ({', '.join(columns)}) VALUES ({placeholders})",
            [row[column] for column in columns],
        )
        self.connection.commit()

    def build(self):
        return build_daily_attention_report(
            self.db_path,
            target_date=self.day,
            timezone_name="Asia/Singapore",
        )

    def test_aggregates_daily_attention_without_double_counting(self):
        self.add_row(bvid="BV-A", cid=11, progress=120, duration=300, dt=1)
        self.add_row(bvid="BV-A", cid=11, progress=240, duration=300, dt=3)
        self.add_row(bvid="BV-B", cid=22, progress=-1, duration=600, dt=2)
        self.add_row(
            title="番剧",
            bvid="",
            cid=33,
            epid=333,
            business="pgc",
            progress=999,
            duration=300,
            dt=4,
        )
        self.add_row(bvid="BV-D", cid=44, progress=0, duration=400, is_finish=1, dt=33)
        self.add_row(business="live", bvid="", cid=55, progress=100, duration=0)
        self.add_row(bvid="BV-X", cid=66, progress=200, badge="互动补充")

        report = self.build()

        self.assertEqual(report["overview"]["history_record_count"], 6)
        self.assertEqual(report["overview"]["distinct_video_count"], 4)
        self.assertEqual(report["overview"]["estimated_watched_seconds"], 1540)
        self.assertEqual(report["overview"]["total_video_duration_seconds"], 1600)
        self.assertEqual(report["type_counts"], {"archive": 4, "live": 1, "pgc": 1})
        self.assertEqual(report["top_videos"][0]["bvid"], "BV-B")
        self.assertEqual(report["top_videos"][-1]["estimated_watched_seconds"], 240)
        self.assertEqual(
            {device["name"]: device["history_record_count"] for device in report["devices"]},
            {"手机": 2, "Web": 1, "Pad": 1, "TV": 1},
        )
        self.assertIn("时长来自最终观看进度估算", report["text"])

    def test_treats_different_parts_as_distinct_items(self):
        self.add_row(bvid="BV-MULTI", cid=101, page=1, part="上", progress=10)
        self.add_row(bvid="BV-MULTI", cid=102, page=2, part="下", progress=20)

        report = self.build()

        self.assertEqual(report["overview"]["distinct_video_count"], 2)
        self.assertEqual(report["overview"]["estimated_watched_seconds"], 30)

    def test_uses_local_day_boundaries(self):
        before = int(datetime(2026, 9, 2, 23, 59, 59, tzinfo=self.timezone).timestamp())
        start = int(datetime(2026, 9, 3, 0, 0, 0, tzinfo=self.timezone).timestamp())
        end = int(datetime(2026, 9, 4, 0, 0, 0, tzinfo=self.timezone).timestamp())
        self.add_row(bvid="BEFORE", cid=1, view_at=before, progress=10)
        self.add_row(bvid="START", cid=2, view_at=start, progress=20)
        self.add_row(bvid="END", cid=3, view_at=end, progress=30)

        report = self.build()

        self.assertEqual(report["overview"]["history_record_count"], 1)
        self.assertEqual(report["top_videos"][0]["bvid"], "START")

    def test_rejects_missing_database(self):
        with self.assertRaises(AttentionReportError):
            build_daily_attention_report(Path(self.temp_dir.name) / "missing.db", self.day)


class AttentionReportRouteTests(unittest.TestCase):
    def test_send_route_propagates_notification_failure(self):
        report = {"subject": "日报", "text": "正文"}
        request = route_mod.AttentionReportRequest()
        with patch.object(route_mod, "_build_report", return_value=report), patch.object(
            route_mod, "send_email", AsyncMock(return_value={"status": "error"})
        ):
            result = asyncio.run(route_mod.send_daily_attention_report(request))

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["data"], report)

    def test_send_route_accepts_successful_email(self):
        report = {"subject": "日报", "text": "正文"}
        request = route_mod.AttentionReportRequest()
        with patch.object(route_mod, "_build_report", return_value=report), patch.object(
            route_mod, "send_email", AsyncMock(return_value={"status": "success"})
        ) as send_email_mock:
            result = asyncio.run(route_mod.send_daily_attention_report(request))

        self.assertEqual(result["status"], "success")
        send_email_mock.assert_awaited_once_with("日报", "正文")


if __name__ == "__main__":
    unittest.main()
