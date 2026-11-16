"""
Every endpoint the schema does not mark public refuses an anonymous caller with
a 401, before it looks at the object or the body. One sweep instead of a
"requires authentication" test per view, and a new view is covered the day it
lands. A 403 here would mean the client shows "forbidden" instead of logging in.
"""
import re

import pytest
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

# simplejwt's own views: AllowAny, but the schema leaves them unmarked.
TOKEN_VIEWS = {'/api/auth/login/', '/api/auth/logout/', '/api/auth/refresh/'}

PRIVATE = [
    (method, path)
    for path, methods in SchemaGenerator().get_schema(request=None, public=True)['paths'].items()
    for method, operation in methods.items()
    if {} not in operation.get('security', []) and path not in TOKEN_VIEWS
]


@pytest.mark.parametrize('method, path', PRIVATE, ids=lambda v: v)
def test_an_anonymous_caller_gets_401(method, path):
    url = re.sub(r'\{[^}]+\}', '0190a000-0000-7000-8000-000000000000', path)

    assert getattr(APIClient(), method)(url, {}, format='json').status_code == 401
