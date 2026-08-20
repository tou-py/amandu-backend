from django import forms
from django.contrib import admin
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html

# accounts depends on tenancy, not the other way round. The import below is the
# one exception and it is deliberate: the admin is a single cross-cutting surface
# by nature -- "who works here" is the first question anyone opens a tenant to
# answer, and it cannot be answered without the join row. Models only; importing
# apps.accounts.admin from here would be a real cycle.
from apps.accounts.models import Invitation, Membership
from apps.commons.dates import one_month_after
from apps.tenancy.models import Tenant


class TenantOwnedAdminForm(forms.ModelForm):
    """
    TenantOwnedMixin marks `tenant` editable=False so that no serializer can ever
    be talked into writing another customer's tenant id. That guard also hides the
    field from every admin form, which would leave the platform owner unable to
    create a row for a tenant at all -- so it is re-added here, explicitly, in the
    one place where choosing the tenant IS the job.

    Frozen after creation on purpose. Moving an existing row to another tenant
    does not move what hangs off it: a client's appointments, a service's
    bookings, a category's services all stay behind, pointing across a tenant
    boundary that the rest of the system assumes cannot be crossed. That is a
    data migration, not a dropdown.
    """

    tenant = forms.ModelChoiceField(queryset=Tenant.objects.all())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['tenant'].initial = self.instance.tenant_id
            # Disabled, not readonly: Django ignores the submitted value entirely
            # and falls back to `initial`, so a hand-crafted POST cannot move the
            # row either.
            self.fields['tenant'].disabled = True

    def save(self, commit=True):
        self.instance.tenant = self.cleaned_data['tenant']
        return super().save(commit)


class TenantOwnedAdmin(admin.ModelAdmin):
    """
    Base for everything that hangs off a tenant. Two things every one of them
    needs and none of them had: a settable tenant (above), and the tenant fetched
    in the same query as the list -- every list_display here shows it, which was
    one extra SELECT per row.
    """

    form = TenantOwnedAdminForm
    list_select_related = ('tenant',)

    def get_form(self, request, obj=None, change=False, **kwargs):
        """
        `tenant` is declared on the form above, not derived from the model, and
        the two paths meet badly here: the admin reads the field off base_fields
        to build its fieldsets, then hands that same list back as the model
        fields to build the form from -- where a non-editable name is a hard
        error. Dropping it from that second list is what lets a declared field
        stand in for a non-editable one. It survives in base_fields, so it still
        renders.

        System checks do not catch this; the page 500s on open. That is what
        apps/commons/test_admin_smoke.py is for.
        """
        fields = kwargs.get('fields')
        if fields:
            kwargs['fields'] = [name for name in fields if name != 'tenant']
        return super().get_form(request, obj, change=change, **kwargs)


class MembershipInline(admin.TabularInline):
    """
    Who works at this tenant, editable from the tenant page. The mirror image of
    the inline on the user page: the same rows, reached from whichever end the
    question started at.
    """

    model = Membership
    extra = 0
    autocomplete_fields = ('user',)
    fields = ('user', 'role', 'status', 'attends_appointments', 'joined_at')
    readonly_fields = ('joined_at',)


class InvitationInline(admin.TabularInline):
    """
    Invitations that have not been accepted yet. Read-only: an invitation is a
    token that was emailed to somebody, and editing the row here would not change
    the link already sitting in their inbox. It is here to answer "did it ever go
    out, and has it expired", which is the actual support question.
    """

    model = Invitation
    extra = 0
    can_delete = False
    fields = ('email', 'role', 'status', 'expires_at', 'created_at')
    readonly_fields = fields

    def get_queryset(self, request):
        return super().get_queryset(request).filter(status=Invitation.Status.PENDING)

    def has_add_permission(self, request, obj):
        return False


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'status', 'paid_until', 'member_count', 'client_count', 'timezone')
    list_filter = ('status',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('created_at', 'updated_at')
    inlines = (MembershipInline, InvitationInline)
    actions = ('register_monthly_payment', 'suspend', 'reactivate')
    fieldsets = (
        (None, {'fields': ('name', 'slug', 'status')}),
        ('Billing', {
            'fields': ('paid_until',),
            'description': (
                'Payment is a bank transfer confirmed by hand. Use the '
                '"Register a monthly payment" action rather than editing the date: '
                'it also lifts a suspension.'
            ),
        }),
        ('Locale', {'fields': ('timezone', 'country')}),
        ('Timestamps', {'fields': ('created_at', 'updated_at')}),
    )

    def get_queryset(self, request):
        # distinct=True on both, and it is not decoration: two joins in one query
        # multiply each other, so a tenant with 3 members and 40 clients would
        # otherwise report 120 of each.
        return super().get_queryset(request).annotate(
            _members=Count('memberships', distinct=True),
            _clients=Count('scheduling_client_set', distinct=True),
        )

    @admin.display(description='Members', ordering='_members')
    def member_count(self, tenant):
        """Links through to the memberships filtered to this tenant, because the
        next thing anyone does after reading the number is go look at the list."""
        url = reverse('admin:accounts_membership_changelist')
        return format_html('<a href="{}?tenant__id__exact={}">{}</a>', url, tenant.pk, tenant._members)

    @admin.display(description='Clients', ordering='_clients')
    def client_count(self, tenant):
        return tenant._clients

    @admin.action(description='Register a monthly payment (extend one month, restore access)')
    def register_monthly_payment(self, request, queryset):
        """
        What a confirmed bank transfer does. Kept as an action rather than left to
        editing the date by hand because this is the one thing that happens every
        month, for every tenant, forever -- and typing a date is where somebody
        eventually types the wrong year.

        Extending from today when the period already lapsed is a decision, not an
        oversight: a tenant who paid late does not owe us the days they spent
        locked out. Paying early, on the other hand, stacks on top of what is
        left, so nobody is punished for being punctual.
        """
        today = timezone.localdate()
        restored = 0

        for tenant in queryset:
            tenant.paid_until = one_month_after(max(tenant.paid_until or today, today))
            fields = ['paid_until', 'updated_at']
            # Only ever lifts a suspension. A CLOSED tenant left the platform and
            # does not come back through a payment.
            if tenant.status == Tenant.Status.SUSPENDED:
                tenant.status = Tenant.Status.ACTIVE
                fields.append('status')
                restored += 1
            tenant.save(update_fields=fields)

        self.message_user(request, f'{len(queryset)} tenant(s) extended, {restored} reactivated.')

    @admin.action(description='Suspend (cut off access, keep the data)')
    def suspend(self, request, queryset):
        """The manual counterpart of the billing sweep, for the reasons money is
        not: abuse, a dispute, a tenant asking to be paused."""
        count = queryset.exclude(status=Tenant.Status.CLOSED).update(
            status=Tenant.Status.SUSPENDED, updated_at=timezone.now()
        )
        self.message_user(request, f'{count} tenant(s) suspended.')

    @admin.action(description='Reactivate')
    def reactivate(self, request, queryset):
        today = timezone.localdate()
        # Read before the update, or the warning below can never fire.
        lapsed = [
            t.name
            for t in queryset.filter(status=Tenant.Status.SUSPENDED, paid_until__lt=today)
        ]

        count = queryset.filter(status=Tenant.Status.SUSPENDED).update(
            status=Tenant.Status.ACTIVE, updated_at=timezone.now()
        )
        self.message_user(request, f'{count} tenant(s) reactivated.')

        if lapsed:
            # Otherwise this looks like it silently failed: the sweep runs every
            # five minutes and will suspend them straight back for the reason
            # nobody changed.
            self.message_user(
                request,
                'Still unpaid, so the sweep will suspend again within minutes: '
                f'{", ".join(lapsed)}. Register the payment instead.',
                level='WARNING',
            )
