from django.db.models import Max
from django.utils import timezone
from django.utils.http import http_date


class LastModifiedListMixin:
    """
    Stamps a `list()` response with a Last-Modified header computed from the
    filtered queryset, so ConditionalGetMiddleware can turn a matching
    If-Modified-Since into a cheap 304 instead of resending the page -- the
    payoff a mobile client polling a feed on a metered connection actually
    wants.

    Falls back to `now()` when the queryset is empty: an empty result must
    never read as "unchanged since forever" to a client that later gets its
    first row -- that row would otherwise arrive with an ETag/Last-Modified
    older than the request that's supposed to reveal it.

    Default aggregates `last_modified_field` (a plain `updated_at`, as given by
    TimestampMixin). Override `get_last_modified` for a model that tracks
    freshness through more than one column.

    Also stamps `Cache-Control: private, no-cache`. Without it, a browser that
    sees Last-Modified but no explicit freshness directive is entitled to
    invent one (RFC 7234 4.2.2, heuristic freshness -- ~10% of the age of
    Last-Modified) and serve the NEXT several polls straight from disk cache
    without asking the server at all. That is silent, not a 304: a poller can
    sit on a stale week for as long as its heuristic window lasts, which grows
    with how old the row was when it was first cached. `no-cache` forces
    revalidation on every request -- still a cheap 304 on no change, just never
    skipped -- and `private` keeps a tenant's agenda out of any shared cache
    sitting between the browser and this API.
    """

    last_modified_field = 'updated_at'

    def get_last_modified(self, queryset):
        latest = queryset.aggregate(value=Max(self.last_modified_field))['value']
        return latest or timezone.now()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        last_modified = self.get_last_modified(queryset)
        response = super().list(request, *args, **kwargs)
        response['Last-Modified'] = http_date(last_modified.timestamp())
        response['Cache-Control'] = 'private, no-cache'
        return response
