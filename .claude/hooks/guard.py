"""Claude Code hooks for this repo (wired in .claude/settings.json). The mode is argv[1]:

  pre  (PreToolUse)  refuses edits and commands that are hard to undo here
  lint (PostToolUse) ruff on the edited file, bug rules only (the repo carries style debt)
  stop (Stop)        the cheap CI steps, when .py files have uncommitted changes

Exit 2 hands stderr back to Claude and blocks the step.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
data = json.load(sys.stdin)
tool_input = data.get('tool_input') or {}
path = (tool_input.get('file_path') or '').replace('\\', '/')


def block(msg):
    print(msg, file=sys.stderr)
    sys.exit(2)


def ask(reason):
    print(json.dumps({'hookSpecificOutput': {
        'hookEventName': 'PreToolUse', 'permissionDecision': 'ask', 'permissionDecisionReason': reason,
    }}))
    sys.exit(0)


def run(*cmd, cwd=ROOT):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding='utf-8')


def tail(text, n=80):
    return '\n'.join(text.strip().splitlines()[-n:])


def ships_to_prod(cmd, cwd):
    if re.search(r'\bgit\b.*\bpush\b', cmd):
        return bool(re.search(r'\bProd\b', cmd)) or run('git', 'branch', '--show-current', cwd=cwd).stdout.strip() == 'Prod'
    merge = re.search(r'\bgh pr merge\b\s*(\d*)', cmd)
    if not merge:
        return False
    view = ['gh', 'pr', 'view', *([merge[1]] if merge[1] else []), '--json', 'baseRefName', '-q', '.baseRefName']
    return run(*view, cwd=cwd).stdout.strip() == 'Prod'


mode = sys.argv[1]
if mode == 'pre':
    if path:
        name = path.rsplit('/', 1)[-1]
        if re.match(r'\.env(\.|$)', name) and name != '.env.example':
            block('.env holds real values; edit it by hand. .env.example is fine.')
        if re.search(r'/migrations/\d{4}_\w+\.py$', path) and run('git', 'ls-files', '--error-unmatch', path).returncode == 0:
            block('This migration is committed and may already be applied somewhere. Write a new migration instead.')
    cmd = tool_input.get('command') or ''
    if cmd and ships_to_prod(cmd, data.get('cwd') or ROOT):
        ask('This ships to Prod (Dokploy deploys on every push). Confirm by hand.')

elif mode == 'lint':
    inside = path.lower().startswith(ROOT.as_posix().lower() + '/')
    if inside and path.endswith('.py'):
        r = run('uvx', 'ruff', 'check', '--select', 'F,E9', '--output-format', 'concise', path)
        if r.returncode:
            block(tail(r.stdout + r.stderr))

elif mode == 'stop':
    # ponytail: one retry, then let it stop, so a check it cannot fix never loops forever
    if data.get('stop_hook_active') or not run('git', 'status', '--porcelain', '--', '*.py').stdout.strip():
        sys.exit(0)
    checks = [
        ['manage.py', 'makemigrations', '--check', '--dry-run'],
        ['manage.py', 'spectacular', '--validate', '--fail-on-warn', '--file', os.devnull],
    ]
    for check in checks:
        r = run(sys.executable, *check)
        if r.returncode:
            block(f'`{" ".join(check)}` failed:\n{tail(r.stdout + r.stderr)}')
