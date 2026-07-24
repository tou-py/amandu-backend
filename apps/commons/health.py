from django.db import Error, connection
from django.http import HttpRequest, JsonResponse


def healthz(request: HttpRequest) -> JsonResponse:
    """
    Liveness + readiness in one: 200 only if the process is up AND the database
    answers. A plain Django view on purpose -- no DRF, so no auth, no throttle,
    nothing between the orchestrator's probe and the answer.

    A 503 pulls the instance out of rotation instead of routing traffic to a box
    that cannot reach Postgres.
    """
    try:
        connection.ensure_connection()
    except Error:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})
