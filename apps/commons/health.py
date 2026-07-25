from django.core.cache import cache
from django.db import Error, connection
from django.http import HttpRequest, JsonResponse


def healthz(request: HttpRequest) -> JsonResponse:
    """
    Liveness + readiness in one: 200 only if the process is up AND every backing
    service this API cannot serve a request without answers. A plain Django view
    on purpose -- no DRF, so no auth, no throttle, nothing between the
    orchestrator's probe and the answer.

    A 503 pulls the instance out of rotation instead of routing traffic to a box
    that cannot reach its dependencies. The per-check keys are always present so a
    503 says which dependency is down without opening the logs.
    """
    checks = {}

    try:
        connection.ensure_connection()
        checks['database'] = 'ok'
    except Error:
        checks['database'] = 'unavailable'

    # The cache is not optional here: AnonRateThrottle and UserRateThrottle are
    # DEFAULT_THROTTLE_CLASSES, so every DRF request reads it. An unreachable
    # Redis turns the whole API into 500s -- while this view, being plain Django,
    # would otherwise keep answering 200 and hide the outage from the probe.
    #
    # Broad except on purpose: the exception type depends on the configured
    # backend (redis-py raises its own, unrelated to django.db.Error), and a
    # health probe must never itself be the thing that raises.
    try:
        cache.get('healthz')
        checks['cache'] = 'ok'
    except Exception:
        checks['cache'] = 'unavailable'

    healthy = all(state == 'ok' for state in checks.values())
    return JsonResponse(
        {'status': 'ok' if healthy else 'unavailable', **checks},
        status=200 if healthy else 503,
    )
