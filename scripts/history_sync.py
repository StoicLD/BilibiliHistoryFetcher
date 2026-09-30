"""History transport and persistence, without server/configuration side effects."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from config.sql_statements_sqlite import CREATE_TABLE_DEFAULT, CREATE_INDEXES
from scripts.attention_storage import atomic_json


class HistorySyncError(RuntimeError):
    pass


def playable_key(row):
    business = str(row.get('business') or 'other').lower()
    identifier = row.get('bvid') or row.get('oid') or row.get('title') or 'unknown'
    if business == 'archive':
        part = f"cid:{row['cid']}" if row.get('cid') else f"page:{row.get('page') or 1}"
        return f'{business}:{identifier}:{part}'
    if business == 'pgc':
        identifier = row.get('epid') or row.get('cid') or row.get('oid') or identifier
    return f'{business}:{identifier}'


def event_key(item):
    row = dict(item.get('history', item))
    row.setdefault('title', item.get('title', ''))
    return playable_key(row), int(item['view_at'])


def merge_record(old, new):
    """Keep the furthest progress for an identical viewing event."""
    merged = {**old, **new}
    merged['duration'] = max(int(old.get('duration') or 0), int(new.get('duration') or 0))
    complete = any(int(x.get('progress') or 0) == -1 or x.get('is_finish') == 1 for x in (old, new))
    merged['is_finish'] = int(complete)
    merged['progress'] = -1 if complete else max(int(old.get('progress') or 0), int(new.get('progress') or 0))
    return merged


def fetch_records(cookie, start_timestamp, *, timeout=30, max_attempts=3, session=None):
    """Return a complete requested window or raise; never return a partial page set."""
    if not cookie or not cookie.strip():
        raise HistorySyncError('未配置 SESSDATA')
    if not cookie.isascii() or any(char.isspace() for char in cookie):
        raise HistorySyncError('SESSDATA 格式无效，请更新登录凭据')
    own_session = session is None
    session = session or requests.Session()
    headers = {'Cookie': f'SESSDATA={cookie.strip()}', 'User-Agent': 'Mozilla/5.0',
               'Referer': 'https://www.bilibili.com/'}
    params = {'ps': 30, 'max': 0, 'view_at': 0, 'business': ''}
    records, cursors = {}, set()
    try:
        while True:
            payload = None
            for attempt in range(max_attempts):
                try:
                    response = session.get('https://api.bilibili.com/x/web-interface/history/cursor',
                                           headers=headers, params=params, timeout=timeout)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise requests.ConnectionError('temporary HTTP failure')
                    if response.status_code != 200:
                        raise HistorySyncError(f'历史接口 HTTP {response.status_code}，同步未完成')
                    payload = response.json()
                    break
                except (requests.RequestException, ValueError):
                    if attempt + 1 == max_attempts:
                        raise HistorySyncError('历史接口网络或响应异常，重试后仍失败') from None
                    time.sleep(min(2 ** attempt, 8))
            if not isinstance(payload, dict):
                raise HistorySyncError('历史接口响应格式异常')
            code = payload.get('code')
            if code == -101:
                raise HistorySyncError('B 站登录已失效，请更新 SESSDATA')
            if code != 0:
                raise HistorySyncError(f'历史接口返回错误码 {code}，同步未完成')
            data = payload.get('data')
            if not isinstance(data, dict) or not isinstance(data.get('list'), list):
                raise HistorySyncError('历史接口缺少记录列表')
            crossed_boundary = False
            for item in data['list']:
                if not isinstance(item, dict) or not isinstance(item.get('history'), dict):
                    raise HistorySyncError('历史记录格式异常')
                if not isinstance(item.get('view_at'), int) or item['view_at'] <= 0:
                    raise HistorySyncError('历史记录缺少有效观看时间')
                if item['view_at'] < start_timestamp:
                    crossed_boundary = True
                    continue
                key = event_key(item)
                records[key] = merge_record(records[key], item) if key in records else item
            if crossed_boundary:
                return list(records.values())
            cursor = data.get('cursor')
            if not isinstance(cursor, dict) or 'max' not in cursor or 'view_at' not in cursor:
                raise HistorySyncError('历史接口缺少分页游标，同步未完成')
            if cursor['max'] == 0:
                return list(records.values())
            token = (cursor['max'], cursor['view_at'])
            if token in cursors:
                raise HistorySyncError('历史接口分页游标重复，同步未完成')
            cursors.add(token)
            params.update(max=cursor['max'], view_at=cursor['view_at'])
            time.sleep(1)
    finally:
        if own_session:
            session.close()


def ensure_year(connection, year):
    table = f'bilibili_history_{int(year)}'
    connection.execute(CREATE_TABLE_DEFAULT.format(table=table))
    for statement in CREATE_INDEXES:
        connection.execute(statement.format(table=table))
    return table


def save_records(records, root, timezone=None):
    tz = ZoneInfo(timezone) if timezone else None
    groups = {}
    for item in records:
        relative = datetime.fromtimestamp(item['view_at'], tz).strftime('%Y/%m/%d.json')
        groups.setdefault(relative, []).append(item)
    for relative, items in groups.items():
        path = Path(root) / relative
        old = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else []
        merged = {event_key(item): item for item in old}
        for item in items:
            key = event_key(item)
            merged[key] = merge_record(merged[key], item) if key in merged else item
        atomic_json(path, sorted(merged.values(), key=lambda item: item['view_at']))


def import_records(connection, records, *, timezone='Asia/Singapore', years=(), sync_deleted=False, id_factory=None):
    """Upsert remote fields; preserve IDs, user remarks and deletion tombstones."""
    tz = ZoneInfo(timezone) if timezone else None
    categories = json.loads((Path(__file__).resolve().parents[1] / 'config/categories.json').read_text(encoding='utf-8'))
    category_map = categories.get('unique_tag_to_main', {})
    groups = {int(year): [] for year in years}
    for item in records:
        year = datetime.fromtimestamp(int(item['view_at']), tz).year
        groups.setdefault(year, []).append(item)
    deleted = set()
    if not sync_deleted and connection.execute("SELECT 1 FROM sqlite_master WHERE name='deleted_history'").fetchone():
        deleted = set(connection.execute('SELECT bvid, view_at FROM deleted_history'))
    inserted = updated = skipped = 0
    for year, items in groups.items():
        table = ensure_year(connection, year)
        existing = {}
        if items:
            low, high = min(x['view_at'] for x in items), max(x['view_at'] for x in items)
            cursor = connection.execute(f'SELECT * FROM {table} WHERE view_at BETWEEN ? AND ?', (low, high))
            names = [x[0] for x in cursor.description]
            for values in cursor:
                row = dict(zip(names, values))
                if row.get('badge') != '互动补充' and not str(row.get('remark') or '').startswith('互动补充：'):
                    existing.setdefault(event_key(row), row)
        for item in items:
            h = item['history']
            if (h.get('bvid', ''), item['view_at']) in deleted:
                skipped += 1
                continue
            fields = ('title', 'long_title', 'cover', 'uri', 'author_name', 'author_face',
                      'badge', 'show_title', 'current', 'new_desc', 'tag_name')
            row = {key: item.get(key) or '' for key in fields}
            row.update({key: int(item.get(key) or 0) for key in
                        ('author_mid', 'view_at', 'progress', 'duration', 'total', 'is_finish', 'is_fav', 'live_status')})
            row.update({key: h.get(key) or 0 for key in ('oid', 'epid', 'page', 'cid', 'dt', 'videos', 'kid')})
            row.update(bvid=h.get('bvid') or '', business=h.get('business') or 'other', part=h.get('part') or '',
                       covers=json.dumps(item.get('covers') or []), main_category=category_map.get(row['tag_name'], '待定'))
            row = merge_record(row, row)
            key = event_key(row)
            old = existing.get(key)
            if old:
                row = {k: v for k, v in merge_record(old, row).items() if k in row}
                if any(old.get(k) != v for k, v in row.items()):
                    connection.execute(f"UPDATE {table} SET {', '.join(k + '=?' for k in row)} WHERE id=?",
                                       (*row.values(), old['id']))
                    updated += 1
                row['id'] = old['id']
            else:
                if id_factory is not None:
                    row['id'] = id_factory()
                cursor = connection.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
                row['id'] = cursor.lastrowid
                inserted += 1
            existing[key] = row
    return {'inserted': inserted, 'updated': updated, 'skipped': skipped}
