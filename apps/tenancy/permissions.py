from rest_framework.permissions import BasePermission

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant


class HasActiveMembership(BasePermission):
    """
    Resolves which tenant the caller is acting for, and refuses the request if
    that answer is not exactly one live membership.

    This is the counterpart of the identity-only JWT: the token says WHO you
    are, this says WHERE you are acting. The pairing is re-checked on every
    request against the database, so revoking a membership takes effect
    immediately instead of when the access token expires.

    Resolution:
      - `X-Tenant-ID` header present -> it must match one of the caller's own
        active memberships. The header SELECTS, it never GRANTS: an id the user
        has no membership for is a 403, exactly like an id that does not exist.
      - No header and exactly one active membership -> that one, so
        single-tenant clients never have to send it.
      - No header and several memberships -> refused. Guessing which tenant the
        caller meant is how data is written into the wrong account.

    A superuser has zero memberships by design and is therefore refused here
    too: platform-level access is the Django admin, not the tenant API.

    ponytail: the permission both authorizes and publishes `request.tenant` /
    `request.membership`. Move to middleware only if non-DRF views ever need
    the same resolution.
    """

    message = 'No active membership for the requested tenant.'

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False

        memberships = list(
            user.memberships.select_related('tenant').filter(
                status=Membership.Status.ACTIVE,
                # Mirrors Tenant.is_operational: a suspended tenant suspends
                # everyone inside it, whatever their own membership says.
                tenant__status=Tenant.Status.ACTIVE,
            )
        )

        raw_tenant_id = request.headers.get('X-Tenant-ID')
        if raw_tenant_id:
            # Compared as text against ids we already own, so an unparseable
            # header is a plain 403 instead of a 500
            membership = next(
                (m for m in memberships if str(m.tenant_id) == raw_tenant_id),
                None,
            )
        elif len(memberships) == 1:
            membership = memberships[0]
        else:
            membership = None

        if membership is None:
            return False

        request.tenant = membership.tenant
        # Carries the role. Views must read permissions from here, never from
        # the token, or a demotion would not apply until the token expires.
        request.membership = membership
        return True


class IsTenantAdmin(BasePermission):
    """
    In-app authorization on the tenant plane: the caller's active membership must
    be owner or admin. Layered AFTER HasActiveMembership, which is what set
    request.membership; on its own this returns False (no membership resolved).
    """

    message = 'Requires a tenant owner or admin role.'

    def has_permission(self, request, view):
        membership = getattr(request, 'membership', None)
        return membership is not None and membership.role in (
            Membership.Role.OWNER,
            Membership.Role.ADMIN,
        )


class IsTenantOwner(BasePermission):
    """
    Stricter than IsTenantAdmin: the owner alone.

    For the few things that are decisions about the business rather than about
    its day -- its name, the timezone every appointment is read in, the country
    phone numbers are parsed against -- where an admin hired to run the diary
    has no standing. Layered AFTER HasActiveMembership, which set the membership.
    """

    message = 'Requires the tenant owner role.'

    def has_permission(self, request, view):
        membership = getattr(request, 'membership', None)
        return membership is not None and membership.role == Membership.Role.OWNER
