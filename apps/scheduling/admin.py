from django.contrib import admin
from django.db.models import Count

from apps.scheduling.models import (
    Appointment,
    AppointmentClient,
    AppointmentSeries,
    Category,
    Client,
    ClientField,
    OutboundMessage,
    Plan,
    Service,
    Subscription,
)
from apps.tenancy.admin import TenantOwnedAdmin


@admin.register(Client)
class ClientAdmin(TenantOwnedAdmin):
    """
    Superuser-only reach across every tenant, like the rest of this admin. The
    tenant column is here because without it two identical names from two tenants
    are indistinguishable.
    """

    list_display = ('name', 'tenant', 'phone', 'email', 'appointment_count', 'created_at')
    list_filter = (('tenant', admin.RelatedOnlyFieldListFilter),)
    search_fields = ('name', 'phone', 'email')
    readonly_fields = ('id', 'created_at', 'updated_at')

    def get_queryset(self, request):
        # Counted in the list query, not once per row: this column exists to be
        # scanned down, which is exactly when N+1 hurts most.
        return super().get_queryset(request).annotate(_appointments=Count('appointments'))

    @admin.display(description='Appointments', ordering='_appointments')
    def appointment_count(self, client):
        return client._appointments


@admin.register(ClientField)
class ClientFieldAdmin(TenantOwnedAdmin):
    """
    The questions a tenant asks about its own clients -- the dentist's history,
    the salon's colour formula. Registered because onboarding a new tenant means
    seeding these, and because a wrong `kind` is only fixable from here.

    `key` is load-bearing: every stored answer in Client.custom_data is filed
    under it, so renaming it here orphans them all with no error. The API refuses
    the rename for that reason; this admin does not, which makes it the one place
    the damage is possible. Change `label` instead -- that is what the form shows.
    """

    list_display = ('label', 'key', 'tenant', 'kind', 'required', 'position')
    list_filter = ('kind', 'required', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('label', 'key', 'tenant__name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Category)
class CategoryAdmin(TenantOwnedAdmin):
    list_display = ('name', 'tenant', 'created_at')
    list_filter = (('tenant', admin.RelatedOnlyFieldListFilter),)
    search_fields = ('name', 'tenant__name')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Service)
class ServiceAdmin(TenantOwnedAdmin):
    list_display = ('name', 'tenant', 'duration', 'category', 'created_at')
    list_filter = (('tenant', admin.RelatedOnlyFieldListFilter), 'category')
    search_fields = ('name', 'tenant__name')
    autocomplete_fields = ('category',)
    list_select_related = ('tenant', 'category')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(AppointmentSeries)
class AppointmentSeriesAdmin(TenantOwnedAdmin):
    """
    The recurrence rule, not the bookings. Deleting one leaves its appointments
    standing (SET_NULL) -- they happened, or are about to -- so this list is for
    reading why a run exists, not for cancelling it. Cancel the appointments.
    """

    list_display = ('id', 'tenant', 'frequency', 'interval', 'until', 'occurrences', 'created_at')
    list_filter = ('frequency', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('tenant__name',)
    readonly_fields = ('created_at', 'updated_at')

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_occurrences=Count('appointments'))

    @admin.display(description='Appointments', ordering='_occurrences')
    def occurrences(self, series):
        return series._occurrences


class AppointmentClientInline(admin.TabularInline):
    """The roster, and where attendance is recorded. `attendance` is the only
    field worth touching here: a no-show marked wrong is the correction support
    actually gets asked for."""

    model = AppointmentClient
    extra = 0
    autocomplete_fields = ('client',)


@admin.register(Appointment)
class AppointmentAdmin(TenantOwnedAdmin):
    """
    Add is off deliberately. A booking reaches its tenant by four paths --
    its own, the professional's, the client's and the service's -- and nothing in
    the database checks they agree; AppointmentSerializer is what scopes every FK
    to one tenant. A form here that lets you pick each one independently is a
    cross-tenant appointment waiting to be created by hand. Book in the app.

    Editing an existing one stays on, because fixing a status or a note is the
    intervention that actually comes up.
    """

    list_display = ('start', 'tenant', 'professional', 'service', 'status', 'capacity', 'reminder_sent_at')
    list_filter = ('status', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('clients__name', 'professional__user__email', 'tenant__name')
    date_hierarchy = 'start'
    autocomplete_fields = ('professional', 'service')
    list_select_related = ('tenant', 'professional__user', 'service')
    readonly_fields = ('id', 'end', 'cancelled_at', 'reminder_sent_at', 'created_at', 'updated_at')
    inlines = (AppointmentClientInline,)

    def has_add_permission(self, request):
        return False


@admin.register(Plan)
class PlanAdmin(TenantOwnedAdmin):
    """
    `categories` is read-only here: nothing in the database stops a plan from
    covering another tenant's category, and this form would offer every one of
    them. A plan created here covers everything (empty means all); narrow it in
    the app. Archive rather than delete -- a plan in use is PROTECTed anyway.
    """

    list_display = ('name', 'tenant', 'price', 'sessions_per_period', 'archived', 'created_at')
    list_filter = ('archived', ('tenant', admin.RelatedOnlyFieldListFilter))
    search_fields = ('name', 'tenant__name')
    readonly_fields = ('categories', 'created_at', 'updated_at')


@admin.register(Subscription)
class SubscriptionAdmin(TenantOwnedAdmin):
    """
    Add is off for the same reason as Appointment: client and plan reach their
    tenant separately and only SubscriptionSerializer makes them agree, so both
    are frozen on edit too. What stays editable is the date or price fix support
    gets asked for. Changing plan is ending this row and opening another -- do
    it in the app, where the periods already owed keep their price.
    """

    list_display = ('client', 'plan', 'tenant', 'start_date', 'end_date', 'price_override')
    list_filter = (('tenant', admin.RelatedOnlyFieldListFilter),)
    search_fields = ('client__name', 'plan__name', 'tenant__name')
    date_hierarchy = 'start_date'
    list_select_related = ('tenant', 'client', 'plan')
    readonly_fields = ('client', 'plan', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        return False


@admin.register(OutboundMessage)
class OutboundMessageAdmin(TenantOwnedAdmin):
    """
    The WhatsApp outbox, to answer "did it go out, and if not why". Read-only:
    a hand-edited body or a cleared `sent_at` would send twice or send words
    nobody wrote. Deleting a stuck row is the one edit allowed.
    """

    list_display = ('to', 'tenant', 'created_at', 'sent_at', 'attempts')
    list_filter = (('tenant', admin.RelatedOnlyFieldListFilter), ('sent_at', admin.EmptyFieldListFilter))
    search_fields = ('to', 'body')
    readonly_fields = ('tenant', 'appointment', 'to', 'body', 'attempts', 'sent_at', 'last_error', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
