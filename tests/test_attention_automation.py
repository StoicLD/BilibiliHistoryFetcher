import copy
import json
import os
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import requests

from scripts.attention_config import DEFAULTS, load_job_config
from scripts.attention_job import execute_pipeline, select_dates, run_job
from scripts.attention_reader import read_brief
from scripts.attention_report import build_daily_attention_report, prepare_export
from scripts.attention_schedule import describe
from scripts.attention_storage import atomic_json, job_lock, JobAlreadyRunning, publish_reports, read_index
from scripts.history_sync import HistorySyncError, fetch_records, import_records, save_records


TZ = ZoneInfo('Asia/Singapore')
DAY = date(2026, 9, 29)


def record(*, bvid='BV-test', cid=1, business='archive', view_at=None, progress=30, **extra):
    item = {'title': '人工智能 编程', 'history': {'bvid': bvid, 'cid': cid, 'page': 1,
            'business': business, 'oid': cid, 'epid': cid if business == 'pgc' else 0, 'dt': 2},
            'view_at': view_at or int(datetime(2026, 9, 29, 12, tzinfo=TZ).timestamp()),
            'progress': progress, 'duration': 100, 'tag_name': '计算机技术', 'author_name': '测试 UP'}
    item.update(extra)
    return item


def response(records=(), cursor=None, code=0, status=200):
    return Mock(status_code=status, json=Mock(return_value={
        'code': code, 'data': {'list': list(records), 'cursor': cursor or {'max': 0, 'view_at': 0}}}))


class HistoryTransportTests(unittest.TestCase):
    def test_login_failure_is_not_empty_success(self):
        session = Mock(get=Mock(return_value=response(code=-101)))
        with self.assertRaisesRegex(HistorySyncError, '登录已失效'):
            fetch_records('secret', 0, session=session)
        self.assertEqual(session.get.call_count, 1)

    def test_network_retries_are_bounded_and_secret_not_in_error(self):
        session = Mock(get=Mock(side_effect=requests.ConnectionError('SESSDATA=secret')))
        with patch('scripts.history_sync.time.sleep'), self.assertRaises(HistorySyncError) as exc:
            fetch_records('secret', 0, max_attempts=3, session=session)
        self.assertEqual(session.get.call_count, 3)
        self.assertNotIn('secret', str(exc.exception))

    def test_partial_pagination_failure_raises(self):
        session = Mock(get=Mock(side_effect=[response([record()], {'max': 10, 'view_at': 100}), response(code=-500)]))
        with patch('scripts.history_sync.time.sleep'), self.assertRaises(HistorySyncError):
            fetch_records('secret', 0, session=session)

    def test_includes_midnight_boundary(self):
        cutoff = int(datetime(2026, 9, 29, tzinfo=TZ).timestamp())
        session = Mock(get=Mock(return_value=response([record(view_at=cutoff), record(view_at=cutoff - 1)])))
        self.assertEqual(len(fetch_records('secret', cutoff, session=session)), 1)

    def test_empty_terminal_page_is_success(self):
        self.assertEqual(fetch_records('secret', 0, session=Mock(get=Mock(return_value=response()))), [])

    def test_repeated_cursor_is_failure(self):
        session = Mock(get=Mock(return_value=response([record()], {'max': 10, 'view_at': 100})))
        with patch('scripts.history_sync.time.sleep'), self.assertRaisesRegex(HistorySyncError, '游标重复'):
            fetch_records('secret', 0, session=session)


class StorageFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'history.db'

    def insert(self, items, **kwargs):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            return import_records(connection, items, years=[2026], **kwargs)

class PersistenceTests(StorageFixture):
    def test_completed_record_is_normalized_on_first_insert(self):
        self.insert([record(progress=-1)])
        self.assertEqual(self.insert([record(progress=-1)])['updated'], 0)

    def test_progress_refresh_is_idempotent_and_preserves_remarks(self):
        self.insert([record(progress=20)])
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute("UPDATE bilibili_history_2026 SET remark='保留', remark_time=123")
        result = self.insert([record(progress=80)])
        self.assertEqual(result['updated'], 1)
        self.assertEqual(self.insert([record(progress=80)])['updated'], 0)
        with closing(sqlite3.connect(self.db)) as connection, connection:
            self.assertEqual(connection.execute('SELECT progress, remark, remark_time FROM bilibili_history_2026').fetchall(), [(80, '保留', 123)])

    def test_parts_and_episodes_without_bvid_are_distinct(self):
        self.insert([record(cid=1), record(cid=2), record(bvid='', cid=3, business='pgc'), record(bvid='', cid=4, business='pgc')])
        self.assertEqual(build_daily_attention_report(self.db, DAY, TZ.key)['overview']['distinct_video_count'], 4)

    def test_deleted_record_is_not_restored(self):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('CREATE TABLE deleted_history(bvid TEXT, view_at INTEGER)')
            connection.execute('INSERT INTO deleted_history VALUES (?,?)', ('BV-test', record()['view_at']))
        self.assertEqual(self.insert([record()])['skipped'], 1)
        self.assertEqual(build_daily_attention_report(self.db, DAY, TZ.key)['overview']['history_record_count'], 0)

    def test_raw_json_refreshes_progress(self):
        save_records([record(progress=10)], self.root, TZ.key)
        save_records([record(progress=70)], self.root, TZ.key)
        items = json.loads((self.root / '2026/09/29.json').read_text(encoding='utf-8'))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['progress'], 70)

    def test_cross_year_and_zero_day(self):
        self.insert([record(view_at=int(datetime(2025, 12, 31, 23, 59, tzinfo=TZ).timestamp()))])
        self.assertEqual(build_daily_attention_report(self.db, '2025-12-31', TZ.key)['overview']['distinct_video_count'], 1)
        self.assertEqual(build_daily_attention_report(self.db, '2026-01-01', TZ.key)['overview']['distinct_video_count'], 0)

    def test_topic_first_match_and_all_categories_total(self):
        self.insert([record(), record(cid=2, title='其他', tag_name=''), record(cid=3, business='live')])
        rules = [{'name': 'AI', 'title_keywords': ['人工智能']}, {'name': '编程', 'title_keywords': ['编程']}]
        report = build_daily_attention_report(self.db, DAY, TZ.key, topic_rules=rules)
        self.assertEqual({x['name'] for x in report['topics']}, {'AI', '未分类'})
        for groups in (report['topics'], report['categories']):
            self.assertEqual(sum(x['video_count'] for x in groups), 2)
            self.assertEqual(sum(x['estimated_watched_seconds'] for x in groups), report['overview']['estimated_watched_seconds'])
        self.assertEqual(report['overview']['video_record_count'], 2)
        self.assertEqual(report['overview']['non_video_record_count'], 1)


class JobAndPublicationTests(StorageFixture):
    def report(self, day=DAY):
        self.insert([record()])
        return prepare_export(build_daily_attention_report(self.db, day, TZ.key), '2026-09-30T09:00:00+08:00')

    def test_late_backfill_does_not_move_latest_backwards(self):
        publish_reports(self.root, [self.report()])
        publish_reports(self.root, [self.report(DAY - timedelta(days=1))])
        self.assertEqual(read_index(self.root)['latest'], DAY.isoformat())
        self.assertIn('估算观看时长', read_brief(self.root))

    def test_failed_publication_preserves_entire_index(self):
        publish_reports(self.root, [self.report()])
        before = (self.root / 'reports/index.json').read_bytes()
        with patch('scripts.attention_storage.atomic_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                publish_reports(self.root, [self.report()])
        self.assertEqual((self.root / 'reports/index.json').read_bytes(), before)

    def test_os_lock_rejects_overlap_and_releases(self):
        with job_lock(self.root):
            with self.assertRaises(JobAlreadyRunning):
                with job_lock(self.root):
                    self.fail('second job entered')
        with job_lock(self.root):
            pass

    def test_failed_fetch_does_not_replace_report(self):
        publish_reports(self.root, [self.report()])
        before = read_index(self.root)
        config = copy.deepcopy(DEFAULTS)
        config['output_dir'] = str(self.root)
        with patch('scripts.attention_job.load_cookie_file', return_value='secret'), patch('scripts.history_sync.fetch_records', side_effect=HistorySyncError('登录失效')):
            with self.assertRaises(HistorySyncError):
                execute_pipeline(config, 'not-used', DAY.isoformat())
        self.assertEqual(read_index(self.root), before)

    def test_reader_in_separate_process_needs_no_network_or_database(self):
        publish_reports(self.root, [self.report()])
        self.db.unlink()
        script = "import sys; from scripts.attention_reader import read_brief; read_brief(sys.argv[1]); assert 'requests' not in sys.modules; assert 'sqlite3' not in sys.modules; assert 'scripts.attention_job' not in sys.modules"
        result = subprocess.run([sys.executable, '-c', script, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reader_distinguishes_missing_failed_and_stale(self):
        self.assertIn('尚未生成', read_brief(self.root))
        publish_reports(self.root, [self.report()])
        atomic_json(self.root / 'run-state.json', {'status': 'failed', 'message': '登录失效'})
        text = read_brief(self.root, now=datetime(2026, 10, 2, tzinfo=TZ))
        self.assertIn('登录失效', text)
        self.assertIn('2026-10-01 的日报尚未生成', text)

    def test_supervised_failure_records_status_without_publishing(self):
        config = copy.deepcopy(DEFAULTS)
        config['output_dir'] = str(self.root)
        result = run_job(config, self.root / 'missing.env', DAY.isoformat())
        self.assertEqual(result['status'], 'failed')
        self.assertFalse((self.root / 'reports/index.json').exists())

    def test_timeout_preserves_previous_report_and_releases_lock(self):
        publish_reports(self.root, [self.report()])
        before = read_index(self.root)
        config = copy.deepcopy(DEFAULTS)
        config.update(output_dir=str(self.root), job_timeout_minutes=0.00001)
        result = run_job(config, self.root / 'missing.env', DAY.isoformat())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_type'], 'Timeout')
        self.assertEqual(read_index(self.root), before)
        with job_lock(self.root):
            pass

    def test_reader_rejects_path_outside_reports(self):
        atomic_json(self.root / 'reports/index.json', {'latest': '2026-09-29', 'dates': {'2026-09-29': '../../secret.json'}})
        with self.assertRaises(ValueError):
            read_brief(self.root)


class ConfigurationAndDatesTests(unittest.TestCase):
    def test_scheduler_json_survives_windows_native_stdout_decoding(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, '-m', 'scripts.attention_schedule', '--config',
                                 str(root / 'config/attention.json')], capture_output=True, check=True)
        self.assertTrue(result.stdout.isascii())
        self.assertEqual(json.loads(result.stdout)['report']['topic_rules'][0]['title_keywords'][0], '人工智能')

    def test_bad_config_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for config in ({'unknown': True}, {'schedule': {'time': '25:10'}}, {'sync': {'max_attempts': True}}, {'report': {'topic_rules': [{'name': 'AI', 'title_keywords': []}]}}):
                atomic_json(path, config)
                with self.assertRaises(ValueError):
                    load_job_config(path)

    def test_initial_yesterday_then_missing_and_refresh_days(self):
        now = datetime(2026, 9, 30, 9, tzinfo=TZ)
        self.assertEqual(select_dates(DEFAULTS, {'dates': {}}, now=now), [DAY])
        result = select_dates(DEFAULTS, {'dates': {'2026-09-24': 'x', '2026-09-28': 'x'}}, now=now)
        self.assertEqual(result, [date(2026, 9, x) for x in range(25, 30)])

    def test_schedule_time_and_enabled_are_applied(self):
        config = copy.deepcopy(DEFAULTS)
        with tempfile.TemporaryDirectory() as directory:
            config['output_dir'] = directory
            self.assertFalse(describe(config, datetime(2026, 9, 30, 8, 59, tzinfo=TZ))['due'])
            self.assertTrue(describe(config, datetime(2026, 9, 30, 9, tzinfo=TZ))['due'])
            config['schedule']['enabled'] = False
            self.assertFalse(describe(config, datetime(2026, 9, 30, 10, tzinfo=TZ))['due'])


if __name__ == '__main__':
    unittest.main()
