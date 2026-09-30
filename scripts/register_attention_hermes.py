"""Register the fixed /bili reader, preserving the rest of Hermes config verbatim."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from scripts.attention_config import PROJECT_ROOT, load_job_config
from scripts.attention_storage import atomic_text


def register(home, config_path):
    config = load_job_config(config_path)
    path = Path(home) / 'config.yaml'
    text = path.read_text(encoding='utf-8')
    existing = yaml.safe_load(text) or {}
    commands = existing.get('quick_commands') or {}
    command = subprocess.list2cmdline([sys.executable, str(PROJECT_ROOT / 'scripts/attention_reader.py'),
                                      '--output-dir', config['output_dir']])
    definition = {'type': 'exec', 'command': command}
    if commands.get('bili') == definition:
        return 'Hermes /bili already configured.'
    if 'bili' in commands:
        raise ValueError('Hermes 已存在不同的 /bili 命令，请先检查冲突')
    commands['bili'] = definition
    block = yaml.safe_dump({'quick_commands': commands}, allow_unicode=True, sort_keys=False)
    pattern = re.compile(r'^quick_commands:[^\n]*(?:\n(?:[ \t]+[^\n]*|[ \t]*))*\n?', re.MULTILINE)
    if pattern.search(text):
        updated = pattern.sub(lambda _: block, text, count=1)
    else:
        updated = text.rstrip() + '\n\n' + block
    # Check semantic preservation before touching a user's unrelated settings.
    expected = {**existing, 'quick_commands': commands}
    if yaml.safe_load(updated) != expected:
        raise ValueError('无法安全保留 Hermes 配置结构')
    backup = path.with_name('config.attention-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.yaml')
    shutil.copy2(path, backup)
    atomic_text(path, updated)
    return 'Hermes /bili registered (fixed read-only command, no model invocation).'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-home', default=os.environ.get('HERMES_HOME'))
    parser.add_argument('--config', default=str(PROJECT_ROOT / 'config/attention.json'))
    args = parser.parse_args()
    if not args.hermes_home:
        parser.error('--hermes-home is required when HERMES_HOME is unset')
    print(register(args.hermes_home, args.config))
