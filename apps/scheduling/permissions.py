from rest_framework.permissions import SAFE_METHODS, BasePermission


class OwnsAppointmentOrActsForTheTeam(BasePermission):
    """
    Object-level: is this specific appointment the caller's to change.

    validate_professional (AppointmentSerializer) only draws this boundary for
    the value being WRITTEN, and only when `professional` is present in the
    payload -- a partial PATCH that omits it, or a custom action like cancel,
    complete and attendance, never touches that field at all, so the rule was
    silently absent everywhere except a full-form edit that happens to resend
    it. This is the counterpart for a slot that already exists: it runs from
    get_object() on every detail route and custom action alike, so there is
    exactly one place left to bypass it -- nowhere.

    The shared calendar stays tenant-wide on purpose: list and retrieve are
    SAFE_METHODS and pass straight through, because seeing a colleague's day
    is the whole point of one agenda. Only a write needs to answer "is this
    slot yours", mirrored from can_schedule_for_others() so the two checks
    can never quietly disagree about who counts as the team.

    Layered after HasActiveMembership, which is what sets request.membership.
    """

    message = 'Your role only allows acting on your own appointments.'

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        membership = request.membership
        return obj.professional_id == membership.id or membership.can_schedule_for_others()
