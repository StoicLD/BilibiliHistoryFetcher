"""Read published reports only. No database, network, Docker or server imports."""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


def read_brief(root, *, now=None):
    root = Path(root).resolve()
    notices = []
    state_path = root / 'run-state.json'
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if state.get('status') == 'failed':
            notices.append('最近一次日报任务失败：' + state.get('message', '未知原因'))
        elif state.get('status') == 'running':
            notices.append('最近一次任务仍在运行或未正常结束；以下仅显示已发布报告。')
    index_path = root / 'reports/index.json'
    if not index_path.exists():
        return '\n'.join(notices + ['尚未生成日报。查询不会启动抓取服务。'])
    index = json.loads(index_path.read_text(encoding='utf-8'))
    latest = index.get('latest')
    if not latest:
        return '\n'.join(notices + ['尚未生成日报。'])
    reports_root = root / 'reports'
    report_path = (reports_root / index['dates'][latest]).resolve()
    if not report_path.is_relative_to(reports_root):
        raise ValueError('报告索引路径不合法')
    report = json.loads(report_path.read_text(encoding='utf-8'))
    tz = ZoneInfo(report['timezone'])
    yesterday = ((now or datetime.now(tz)).astimezone(tz).date() - timedelta(days=1)).isoformat()
    if yesterday not in index['dates']:
        notices.append(f'{yesterday} 的日报尚未生成；以下为 {latest} 的已保存简报。')
    return '\n\n'.join(notices + [report['brief']])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    try:
        print(read_brief(args.output_dir))
        return 0
    except (OSError, ValueError, KeyError):
        print('已保存的日报暂时无法读取。查询不会启动抓取服务。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
