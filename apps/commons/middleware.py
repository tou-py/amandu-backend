import logging
import time

from asgiref.sync import iscoroutinefunction
from django.utils.decorators import sync_and_async_middleware

logger = logging.getLogger('amandu.access')


@sync_and_async_middleware
def access_log(get_response):
    """
    One line per request with how long it took -- the number the capacity
    question depends on. gunicorn's `%(D)s` used to carry it, but under
    UvicornWorker gunicorn's --access-logformat is ignored and uvicorn's own
    format has no duration field.

    `request.path`, not the full path: the query string is where a credential
    ends up when a client cannot send headers (EventSource), and logs are the
    last place it should land.

    For a streaming response this is time to first byte, not the stream's life.
    """

    def log(request, response, started):
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info('%s %s %s %.1fms', request.method, request.path, response.status_code, elapsed_ms)

    if iscoroutinefunction(get_response):

        async def middleware(request):
            started = time.perf_counter()
            response = await get_response(request)
            log(request, response, started)
            return response

    else:

        def middleware(request):
            started = time.perf_counter()
            response = get_response(request)
            log(request, response, started)
            return response

    return middleware
