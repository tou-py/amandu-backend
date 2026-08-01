class NoHeuristicCacheMixin:
    """
    Stamps `Cache-Control: private, no-cache` on a `list()` response.

    That is ALL this does. The 304 itself is already handled, correctly and for
    free, by ConditionalGetMiddleware: it hashes the rendered body into an ETag
    and answers a matching If-None-Match with an empty 304. GZipMiddleware
    downgrades that ETag to weak, which Django's own comparison accounts for, so
    the round trip survives compression -- the polling win this mixin was
    originally written for is kept without computing anything here.

    Why the header is still needed: a response with neither Cache-Control nor an
    explicit expiry lets a cache invent freshness of its own (RFC 9111 4.2.2),
    and serve the next several polls off disk without asking the server at all.
    That is silent, not a 304. `no-cache` forces revalidation every time -- still
    a cheap 304 when nothing changed, just never skipped -- and `private` keeps a
    tenant's agenda out of any shared cache between the browser and this API.

    What used to live here, and why it is gone: a `Last-Modified` header built
    from `Max(updated_at)` over the filtered queryset. Two defects, one fatal.

    1. HTTP dates have one-second resolution, so `http_date()` truncates. A write
       landing in the same second as the previous maximum produced an identical
       Last-Modified, If-Modified-Since matched, and the client got a 304 for a
       row that HAD changed -- permanently, because a 304 never advances the
       validator the client stored. Dragging an appointment moments after
       another edit is exactly that window.
    2. `Max()` over a filtered set can move BACKWARDS when a row leaves the
       filter, which reads to a client as "older than what I already have".

    Both were an extra aggregate query per request buying nothing the body hash
    did not already do exactly.
    """

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response['Cache-Control'] = 'private, no-cache'
        return response
