"""Bounded one-shot fetch/import/report job; no HTTP server or model required."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import sqlite3
from contextlib import closing
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.attention_config import PROJECT_ROOT, load_cookie_file, load_job_config
from scripts.attention_storage import JobAlreadyRunning, atomic_json, job_lock, publish_reports, read_index


def select_dates(config, index, *, target=None, now=None):
    tz = ZoneInfo(config['timezone'])
    yesterday = ((now or datetime.now(tz)).astimezone(tz).date() - timedelta(days=1))
    if target:
        requested = date.fromisoformat(target)
        if requested > yesterday:
            raise ValueError('只能生成已经结束的日期的日报')
        return [requested]
    dates = {yesterday}
    published = index.get('dates', {})
    if published:
        first = date.fromisoformat(min(published))
        for offset in range(config['sync']['catch_up_days']):
            day = yesterday - timedelta(days=offset)
            if day >= first and day.isoformat() not in published:
                dates.add(day)
        for offset in range(config['sync']['refresh_days']):
            day = yesterday - timedelta(days=offset)
            if day.isoformat() in published:
                dates.add(day)
    return sorted(dates)


def execute_pipeline(config, credentials, target=None):
    from scripts.attention_report import build_daily_attention_report, prepare_export
    from scripts.history_sync import fetch_records, import_records, save_records

    root = Path(config['output_dir'])
    tz = ZoneInfo(config['timezone'])
    days = select_dates(config, read_index(root), target=target)
    refresh_start = datetime.now(tz).date() - timedelta(days=config['sync']['refresh_days'])
    start = min(min(days), refresh_start)
    cutoff = int(datetime.combine(start, time.min, tzinfo=tz).timestamp())
    records = fetch_records(load_cookie_file(credentials), cutoff,
                            timeout=config['sync']['request_timeout_seconds'], max_attempts=config['sync']['max_attempts'])
    save_records(records, root / 'history_by_date', config['timezone'])
    with closing(sqlite3.connect(root / 'history.db', timeout=30)) as connection, connection:
        counts = import_records(connection, records, timezone=config['timezone'], years={day.year for day in days})
    generated = datetime.now(tz).isoformat(timespec='seconds')
    reports = [prepare_export(build_daily_attention_report(root / 'history.db', day, config['timezone'],
                config['report']['top_n'], config['report']['topic_rules']), generated) for day in days]
    # Parent commits only after this worker has finished within the time limit.
    atomic_json(root / 'pending-reports.json', reports)
    return {'dates': [day.isoformat() for day in days], 'records_received': len(records), **counts}


def _worker(config, credentials, target, pipe):
    try:
        pipe.send({'ok': True, 'result': execute_pipeline(config, credentials, target)})
    except Exception as exc:
        # Do not serialize request objects, URLs, cookies or unfiltered exception messages.
        from scripts.history_sync import HistorySyncError
        message = str(exc) if isinstance(exc, HistorySyncError) else {
            FileNotFoundError: '所需文件不存在，请检查凭据或配置路径',
            sqlite3.Error: '历史数据库写入失败',
            ValueError: '配置、日期或历史数据格式不正确',
        }.get(type(exc), f'日报生成失败（{type(exc).__name__}）')
        pipe.send({'ok': False, 'message': message, 'error_type': type(exc).__name__})
    finally:
        pipe.close()


def run_job(config, credentials, target=None):
    root = Path(config['output_dir'])
    tz = ZoneInfo(config['timezone'])
    with job_lock(root):
        # Validate the requested date before starting a subprocess.
        select_dates(config, read_index(root), target=target)
        started = datetime.now(tz).isoformat(timespec='seconds')
        state = {'status': 'running', 'started_at': started, 'timezone': config['timezone'], 'target': target}
        atomic_json(root / 'run-state.json', state)
        context = multiprocessing.get_context('spawn')
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=_worker, args=(config, str(credentials), target, sender))
        process.start()
        sender.close()
        try:
            process.join(config['job_timeout_minutes'] * 60)
            if process.is_alive():
                process.terminate()
                process.join(5)
                if process.is_alive():
                    process.kill()
                    process.join()
                result = {'ok': False, 'message': '日报任务超过运行时限，已终止', 'error_type': 'Timeout'}
            elif receiver.poll():
                try:
                    result = receiver.recv()
                except EOFError:
                    result = {'ok': False, 'message': '日报子进程异常退出'}
            else:
                result = {'ok': False, 'message': '日报子进程异常退出'}
        finally:
            if process.is_alive():
                process.terminate()
                process.join()
            receiver.close()
        pending = root / 'pending-reports.json'
        try:
            if result['ok']:
                reports = json.loads(pending.read_text(encoding='utf-8'))
                publish_reports(root, reports)
        except (OSError, ValueError, KeyError):
            result = {'ok': False, 'message': '报告发布失败，保留前次已发布报告'}
        finally:
            pending.unlink(missing_ok=True)
        state.update(result)
        state.update(status='success' if result['ok'] else 'failed', finished_at=datetime.now(tz).isoformat(timespec='seconds'))
        atomic_json(root / 'run-state.json', state)
        log = root / 'runs' / (started.replace(':', '').replace('+', '_') + '.json')
        atomic_json(log, state)
        return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(PROJECT_ROOT / 'config/attention.json'))
    parser.add_argument('--credentials', default=os.environ.get('ATTENTION_CREDENTIALS_FILE', str(PROJECT_ROOT / 'secrets/bilibili.env')))
    parser.add_argument('--date')
    parser.add_argument('--check-config', action='store_true')
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        config = load_job_config(args.config)
        if args.check_config:
            print(json.dumps(config, ensure_ascii=False))
            return 0
        result = run_job(config, Path(args.credentials).resolve(), args.date)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result['status'] == 'success' else 1
    except JobAlreadyRunning:
        print('已有日报任务正在运行，本次跳过。')
        return 0
    except (OSError, ValueError) as exc:
        print(f'日报任务无法启动（{type(exc).__name__}），请检查配置文件和目录。')
        return 2


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
