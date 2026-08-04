from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import CustomUser, Invitation, Membership, Notification


class MembershipInline(admin.TabularInline):
    """Manage a user's tenant memberships (and their role) from the user page."""

    model = Membership
    extra = 0
    autocomplete_fields = ('tenant',)
    fields = ('tenant', 'role', 'status', 'attends_appointments', 'joined_at')
    readonly_fields = ('joined_at',)


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    """
    UserAdmin's own fieldsets name `username` and `date_joined`, neither of which
    exists on CustomUser, so every field-bearing attribute has to be restated.
    Inheriting is still worth it for the password-change view and the add form.
    """

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (_('Personal info'), {'fields': ('first_name', 'last_name')}),
        (_('Agenda'), {'fields': ('reminder_lead',)}),
        (
            _('Permissions'),
            {
                'fields': (
                    'is_active',
                    'is_staff',
                    'is_superuser',
                    'groups',
                    'user_permissions',
                ),
            },
        ),
        (_('Important dates'), {'fields': ('last_login', 'created_at', 'updated_at')}),
    )
    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': ('email', 'usable_password', 'password1', 'password2'),
            },
        ),
    )
    list_display = ('email', 'first_name', 'last_name', 'works_at', 'is_active', 'is_staff')
    # Filtering people by the tenant they work for is how support starts: the
    # caller says the name of their business, never their own email.
    list_filter = ('is_active', 'is_staff', 'is_superuser', 'memberships__tenant')
    search_fields = ('email', 'first_name', 'last_name')
    ordering = ('email',)
    readonly_fields = ('last_login', 'created_at', 'updated_at')
    inlines = (MembershipInline,)

    def get_queryset(self, request):
        # works_at walks the memberships of every row on the page; without this
        # that is two queries per user.
        return super().get_queryset(request).prefetch_related('memberships__tenant')

    @admin.display(description='Works at')
    def works_at(self, user):
        """A user with no memberships is not broken -- that is what a platform
        superuser looks like -- so say so rather than showing an empty cell that
        reads as missing data."""
        names = [m.tenant.name for m in user.memberships.all()]
        return ', '.join(names) or '— platform only'


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    """
    Access itself, as a first-class list. Registered because the inlines above
    only answer the question from one end at a time, and the intervention that
    actually happens -- "cut this person off", "make them bookable" -- needs the
    row, not the page it hangs on.

    Deleting one is deliberately left available but is almost never right: it
    takes the person's role and history with it, and PROTECT on Appointment will
    refuse anyway once they have been booked. SUSPENDED is the reversible answer.
    """

    list_display = ('user', 'tenant', 'role', 'status', 'attends_appointments', 'joined_at')
    list_filter = ('status', 'role', 'attends_appointments', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('user__email', 'user__first_name', 'user__last_name', 'tenant__name')
    autocomplete_fields = ('user', 'tenant')
    list_select_related = ('user', 'tenant')
    readonly_fields = ('joined_at',)
    actions = ('suspend', 'activate', 'make_bookable', 'make_unbookable')

    @admin.action(description='Suspend access to this tenant')
    def suspend(self, request, queryset):
        count = queryset.update(status=Membership.Status.SUSPENDED)
        self.message_user(request, f'{count} membership(s) suspended.')

    @admin.action(description='Restore access to this tenant')
    def activate(self, request, queryset):
        count = queryset.update(status=Membership.Status.ACTIVE)
        self.message_user(request, f'{count} membership(s) restored.')

    @admin.action(description='Show in the agenda (attends appointments)')
    def make_bookable(self, request, queryset):
        count = queryset.update(attends_appointments=True)
        self.message_user(request, f'{count} membership(s) now bookable.')

    @admin.action(description='Hide from the agenda (does not attend)')
    def make_unbookable(self, request, queryset):
        """Does not touch appointments already on the books, and cannot: they
        point at this membership with PROTECT. It only stops NEW ones."""
        count = queryset.update(attends_appointments=False)
        self.message_user(request, f'{count} membership(s) no longer bookable.')


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    """
    `token` is deliberately absent from every list and form here. It is the
    credential -- whoever holds it can join the tenant, exactly like a
    password-reset link -- and a superuser who wants to grant access has a
    shorter path: create the Membership. Showing it would put a live credential
    on a screen that gets shared.
    """

    list_display = ('email', 'tenant', 'role', 'status', 'expired', 'expires_at', 'created_at')
    list_filter = ('status', 'role', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('email', 'tenant__name')
    list_select_related = ('tenant',)
    readonly_fields = ('expires_at', 'created_at', 'updated_at')
    exclude = ('token',)
    actions = ('revoke',)

    @admin.display(boolean=True, description='Expired')
    def expired(self, invitation):
        return invitation.expires_at < timezone.now()

    @admin.action(description='Revoke')
    def revoke(self, request, queryset):
        """Kills the link that is already sitting in somebody's inbox. Only
        pending ones: revoking an accepted invitation would say nothing about the
        Membership it already created, which is what actually grants access."""
        count = queryset.filter(status=Invitation.Status.PENDING).update(
            status=Invitation.Status.REVOKED
        )
        self.message_user(request, f'{count} invitation(s) revoked.')


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """
    Read-only, and here for exactly one question: "they say they were never told
    -- was it sent?". `pushed_at` is the answer, and it is the sweep's own
    idempotency mark, so nothing here may write to it.

    PushSubscription is deliberately NOT registered: the endpoint it stores is a
    credential for pushing to somebody's browser, and no support question needs
    it on screen.
    """

    list_display = ('recipient', 'verb', 'actor', 'created_at', 'pushed_at', 'read_at')
    list_filter = ('verb', ('recipient__tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('recipient__user__email',)
    list_select_related = ('recipient__user', 'actor__user', 'appointment')
    date_hierarchy = 'created_at'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
