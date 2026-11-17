import json
import logging

import pytest
from django.urls import reverse

from apps.commons.csp_reports import clean_report

INVITE = 'https://app.kyo.test/invitations/accept?token=SECRET#x'


def test_report_to_format_keeps_the_path_and_drops_the_token():
    report = clean_report({
        'type': 'csp-violation',
        'url': INVITE,
        'body': {
            'documentURL': INVITE,
            'blockedURL': 'trusted-types-sink',
            'effectiveDirective': 'require-trusted-types-for',
            'sample': 'Element innerHTML|<img src=x onerror=alert(1)> and much more text',
            'sourceFile': 'https://app.kyo.test/assets/index.js?v=1',
            'lineNumber': 12,
        },
    })
    assert report == {
        'document': 'https://app.kyo.test/invitations/accept',
        'blocked': 'trusted-types-sink',
        'directive': 'require-trusted-types-for',
        'sample': 'Element innerHTML|<img src=x onerror=ale',
        'source': 'https://app.kyo.test/assets/index.js',
        'line': 12,
    }


def test_report_uri_format_reads_the_same():
    report = clean_report({'csp-report': {
        'document-uri': INVITE,
        'blocked-uri': 'inline',
        'violated-directive': 'script-src-elem',
        'line-number': 'not a number',
    }})
    assert report['document'] == 'https://app.kyo.test/invitations/accept'
    assert report['blocked'] == 'inline'
    assert report['directive'] == 'script-src-elem'
    assert report['line'] is None


@pytest.mark.parametrize('raw', [None, [], 'text', {'type': 'deprecation', 'body': {}}, {'csp-report': 'x'}])
def test_anything_else_is_not_a_report(raw):
    assert clean_report(raw) is None


@pytest.mark.django_db
def test_endpoint_logs_a_batch_without_auth_and_answers_204(client, caplog):
    batch = [{'type': 'csp-violation', 'body': {'documentURL': INVITE, 'blockedURL': 'eval'}}] * 3
    with caplog.at_level(logging.WARNING, logger='amandu.csp'):
        res = client.post(reverse('csp-reports'), json.dumps(batch), content_type='application/reports+json')
    assert res.status_code == 204
    assert len(caplog.records) == 3
    assert 'SECRET' not in caplog.text


@pytest.mark.django_db
def test_endpoint_takes_the_legacy_content_type(client, caplog):
    body = {'csp-report': {'document-uri': INVITE, 'blocked-uri': 'inline'}}
    with caplog.at_level(logging.WARNING, logger='amandu.csp'):
        res = client.post(reverse('csp-reports'), json.dumps(body), content_type='application/csp-report')
    assert res.status_code == 204
    assert len(caplog.records) == 1


@pytest.mark.django_db
def test_a_burst_is_cut_to_twenty(client, caplog):
    batch = [{'type': 'csp-violation', 'body': {'blockedURL': 'eval'}}] * 50
    with caplog.at_level(logging.WARNING, logger='amandu.csp'):
        client.post(reverse('csp-reports'), json.dumps(batch), content_type='application/reports+json')
    assert len(caplog.records) == 20
