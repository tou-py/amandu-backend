from drf_spectacular.openapi import AutoSchema
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter

from apps.tenancy.permissions import HasActiveMembership

TENANT_HEADER = OpenApiParameter(
    name='X-Tenant-ID',
    type=OpenApiTypes.INT,
    location=OpenApiParameter.HEADER,
    # Not strictly required: with a single active membership it is inferred (see
    # HasActiveMembership). Marking it required would make Swagger reject the
    # single-tenant case that the API deliberately allows.
    required=False,
    description=(
        'Selects which tenant you are acting for. Required only when your account '
        'has more than one active membership; with a single membership it is '
        'inferred. The id must be one of your own active memberships, or the '
        'request is refused.'
    ),
)


class TenantHeaderAutoSchema(AutoSchema):
    """
    Documents the X-Tenant-ID header on exactly the endpoints that resolve a
    tenant through it -- those guarded by HasActiveMembership -- and nowhere
    else, so login, refresh, /me and the public accept endpoint stay
    header-free. Wired as REST_FRAMEWORK['DEFAULT_SCHEMA_CLASS'], so every view
    gets it automatically without per-view decorators.
    """

    def get_override_parameters(self):
        params = super().get_override_parameters()
        if HasActiveMembership in getattr(self.view, 'permission_classes', ()):
            return [*params, TENANT_HEADER]
        return params
