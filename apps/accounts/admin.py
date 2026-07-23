from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import CustomUser, Invitation, Membership


class MembershipInline(admin.TabularInline):
    """Manage a user's tenant memberships (and their role) from the user page."""

    model = Membership
    extra = 0
    autocomplete_fields = ('tenant',)


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
    list_display = ('email', 'first_name', 'last_name', 'is_staff')
    search_fields = ('email', 'first_name', 'last_name')
    ordering = ('email',)
    readonly_fields = ('last_login', 'created_at', 'updated_at')
    inlines = (MembershipInline,)


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ('email', 'tenant', 'role', 'status', 'expires_at', 'created_at')
    list_filter = ('tenant', 'status')
    search_fields = ('email',)
    readonly_fields = ('token', 'expires_at', 'created_at', 'updated_at')
