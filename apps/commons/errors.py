"""
Every error answer carries a machine-readable `code` next to its message, so
the front end can tell rejections apart without matching on English text that
a reword would silently break.

The code is DRF's own: the `default_code` of an APIException (Overlaps,
AlreadyPaid, Referenced), the `code` of a permission class, or the `code=` a
ValidationError is raised with. This handler only lifts it to the top level:

    {"detail": "This has already been paid.", "code": "already_paid"}
    {"count": ["..."], "code": "not_enough_periods"}

A field-keyed answer whose fields disagree on why (two fields, two reasons)
gets the generic `invalid`; the per-field detail is still there. An answer
that is a bare list (a view raising ValidationError('...') directly) has
nowhere to put a key and stays as it was -- raise Refused instead.
"""

from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


class Refused(APIException):
    """
    A rejection with its stable code, answered as {"detail", "code"}: 400 by
    default, 409 when what stops the request is state rather than input.
    """

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, detail, code, status_code=None):
        super().__init__(detail, code)
        if status_code is not None:
            self.status_code = status_code


def _leaf_codes(codes):
    if isinstance(codes, str):
        yield codes
    else:
        for value in codes.values() if isinstance(codes, dict) else codes:
            yield from _leaf_codes(value)


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None or not isinstance(response.data, dict) or not isinstance(exc, APIException):
        return response
    codes = set(_leaf_codes(exc.get_codes()))
    response.data['code'] = codes.pop() if len(codes) == 1 else 'invalid'
    return response
