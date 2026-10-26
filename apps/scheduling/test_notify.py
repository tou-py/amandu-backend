"""
notify.py is the one place that decides who is told about what. This keeps it
that way: a notice filed from a view or a command is how the rules drifted
apart before (a cancel told the professional, its series twin did not).
"""
import re
from pathlib import Path

from django.conf import settings

PRODUCER = re.compile(r'(?<!class )\b(Notification|OutboundMessage)(\(|\.objects\.(create|bulk_create|get_or_create))')
ALLOWED = {Path('apps/scheduling/notify.py')}


def test_only_notify_files_notices():
    root = Path(settings.BASE_DIR)
    offenders = [
        f'{path.relative_to(root)}:{number}'
        for path in (root / 'apps').rglob('*.py')
        if path.relative_to(root) not in ALLOWED
        and not path.name.startswith('test_')
        and 'migrations' not in path.parts
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1)
        if PRODUCER.search(line)
    ]
    assert offenders == [], 'File notices through apps.scheduling.notify.notify()'
