"""Host-side scheduler configuration, without starting a job."""

import argparse
import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.attention_config import load_job_config


def describe(config, now=None):
    tz = ZoneInfo(config['timezone'])
    now = (now or datetime.now(tz)).astimezone(tz)
    at = datetime.combine(now.date(), time.fromisoformat(config['schedule']['time']), tzinfo=tz)
    boundary = at if at > now else at + timedelta(days=1)
    due = config['schedule']['enabled'] and now >= at
    state_path = Path(config['output_dir']) / 'run-state.json'
    if due and state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            finished = datetime.fromisoformat(state['finished_at']).astimezone(tz)
            yesterday = (now.date() - timedelta(days=1)).isoformat()
            if state['status'] == 'success' and finished.date() == now.date() and yesterday in state.get('result', {}).get('dates', []):
                due = False
        except (KeyError, ValueError):
            pass
    return {**config, 'due': due, 'start_boundary': boundary.isoformat(), 'python': sys.executable}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--record-failure', action='store_true')
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    config = load_job_config(args.config)
    if args.record_failure:
        from scripts.attention_storage import job_lock, atomic_json, JobAlreadyRunning
        try:
            with job_lock(config['output_dir']):
                atomic_json(Path(config['output_dir']) / 'run-state.json', {
                    'status': 'failed', 'finished_at': datetime.now(ZoneInfo(config['timezone'])).isoformat(),
                    'message': os.environ.get('ATTENTION_HOST_FAILURE', '容器启动失败'),
                })
        except JobAlreadyRunning:
            pass
    else:
        # Windows PowerShell 5.1 decodes redirected native stdout using its OEM
        # codepage. ASCII JSON preserves Chinese rules and paths in scheduled runs.
        print(json.dumps(describe(config), ensure_ascii=True))
