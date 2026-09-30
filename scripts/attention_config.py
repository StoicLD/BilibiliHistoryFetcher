"""Strict JSON configuration for the standalone daily job (stdlib only)."""

import copy
import json
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = {
    'version': 1, 'timezone': 'Asia/Singapore',
    'schedule': {'enabled': True, 'time': '09:00'},
    'sync': {'refresh_days': 3, 'catch_up_days': 7, 'request_timeout_seconds': 30, 'max_attempts': 3},
    'job_timeout_minutes': 20, 'output_dir': './output/attention',
    'report': {'top_n': 5, 'topic_rules': []},
}


def _merge(defaults, supplied, prefix=''):
    if not isinstance(supplied, dict):
        raise ValueError(f'{prefix or "配置"} 必须是对象')
    unknown = supplied.keys() - defaults.keys()
    if unknown:
        raise ValueError(f'未知配置项: {prefix}{sorted(unknown)[0]}')
    result = copy.deepcopy(defaults)
    for key, value in supplied.items():
        result[key] = _merge(defaults[key], value, prefix + key + '.') if isinstance(defaults[key], dict) else value
    return result


def load_job_config(path):
    config = _merge(DEFAULTS, json.loads(Path(path).read_text(encoding='utf-8-sig')))
    if type(config['version']) is not int or config['version'] != 1:
        raise ValueError('仅支持配置 version=1')
    try:
        ZoneInfo(config['timezone'])
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError('timezone 必须是有效的 IANA 时区') from None
    if type(config['schedule']['enabled']) is not bool:
        raise ValueError('schedule.enabled 必须是布尔值')
    if not isinstance(config['schedule']['time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', config['schedule']['time']):
        raise ValueError('schedule.time 必须为 HH:MM')
    bounded = [(config, 'job_timeout_minutes', 1, 120), (config['report'], 'top_n', 1, 20),
               (config['sync'], 'refresh_days', 1, 30), (config['sync'], 'catch_up_days', 1, 90),
               (config['sync'], 'request_timeout_seconds', 1, 120), (config['sync'], 'max_attempts', 1, 10)]
    for section, key, low, high in bounded:
        if type(section[key]) is not int or not low <= section[key] <= high:
            raise ValueError(f'{key} 必须是 {low}–{high} 的整数')
    rules = config['report']['topic_rules']
    if not isinstance(rules, list):
        raise ValueError('topic_rules 必须是数组')
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {'name', 'title_keywords'}:
            raise ValueError('主题规则需要 name 和 title_keywords')
        if not isinstance(rule['name'], str) or not rule['name'].strip():
            raise ValueError('主题名称不能为空')
        words = rule['title_keywords']
        if not isinstance(words, list) or not words or any(not isinstance(x, str) or not x.strip() for x in words):
            raise ValueError('title_keywords 必须包含非空关键词')
    if not isinstance(config['output_dir'], str) or not config['output_dir'].strip():
        raise ValueError('output_dir 不能为空')
    # Relative paths always refer to the repository root, independent of scheduler cwd.
    output = Path(os.environ.get('ATTENTION_OUTPUT_DIR') or config['output_dir']).expanduser()
    config['output_dir'] = str((PROJECT_ROOT / output).resolve())
    return config


def load_cookie_file(path):
    """Read one secret without ever including its value in an error or log."""
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        name, separator, value = line.strip().partition('=')
        if separator and name == 'SESSDATA' and value.strip().strip('\"\''):
            cookie = value.strip().strip('\"\'')
            if not cookie.isascii() or any(char.isspace() for char in cookie):
                raise ValueError('SESSDATA 必须是有效的 ASCII 凭据，不能使用占位文字')
            return cookie
    raise ValueError('凭据文件缺少 SESSDATA')
