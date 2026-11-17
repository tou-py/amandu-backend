"""
Where browsers report what the app's Content-Security-Policy blocked.

The frontend's security-headers.conf points `report-to` (Reporting API, Chrome)
and `report-uri` (the older format Safari and Firefox still send) here. Without
it a page the policy breaks for a user -- a Trusted Types sink nobody listed, say
-- fails silently in their browser and nobody hears of it.

Reports are logged, not stored: they are for noticing a breakage, and the log is
where the rest of production's trouble already goes. Only the path of every URL
is kept: an invitation link carries its token in the query string.
"""

import json
import logging
from urllib.parse import urlsplit

from drf_spectacular.utils import extend_schema
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

logger = logging.getLogger('amandu.csp')

# A page that trips the policy in a loop sends a burst; past this many reports
# in one request the rest say nothing new.
MAX_REPORTS = 20


def _path(value: object) -> str:
    """A URL cut to scheme, host and path; a keyword such as `inline` as is."""
    text = str(value or '')[:500]
    if '://' not in text:
        return text
    parts = urlsplit(text)
    return f'{parts.scheme}://{parts.netloc}{parts.path}'


def clean_report(raw: object) -> dict | None:
    """One report in either format, reduced to what says what broke and where."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get('csp-report'), dict):  # report-uri
        body = raw['csp-report']
        document, blocked = body.get('document-uri'), body.get('blocked-uri')
        directive = body.get('effective-directive') or body.get('violated-directive')
        sample, source, line = body.get('script-sample'), body.get('source-file'), body.get('line-number')
    elif raw.get('type') == 'csp-violation' and isinstance(raw.get('body'), dict):  # report-to
        body = raw['body']
        document, blocked = body.get('documentURL'), body.get('blockedURL')
        directive = body.get('effectiveDirective')
        sample, source, line = body.get('sample'), body.get('sourceFile'), body.get('lineNumber')
    else:
        return None
    return {
        'document': _path(document),
        'blocked': _path(blocked),
        'directive': str(directive or '')[:100],
        # Browsers cap the sample at 40 characters; so does this.
        'sample': str(sample or '')[:40],
        'source': _path(source),
        'line': line if isinstance(line, int) else None,
    }


class ReportsJSONParser(JSONParser):
    media_type = 'application/reports+json'


class CSPReportJSONParser(JSONParser):
    media_type = 'application/csp-report'


@extend_schema(exclude=True)  # called by browsers, never by the frontend's code
class CSPReportView(APIView):
    authentication_classes = ()
    permission_classes = (AllowAny,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'csp-report'
    parser_classes = (ReportsJSONParser, CSPReportJSONParser, JSONParser)

    def post(self, request):
        data = request.data
        batch = data if isinstance(data, list) else [data]
        for raw in batch[:MAX_REPORTS]:
            report = clean_report(raw)
            if report:
                logger.warning('csp violation %s', json.dumps(report, ensure_ascii=False))
        # Nothing for the browser to act on, whatever arrived.
        return Response(status=204)
