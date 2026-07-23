from django.contrib import admin

from apps.scheduling.models import (
    Appointment,
    AppointmentClient,
    Category,
    Client,
    Service,
)


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    """
    Superuser-only reach across every tenant, like the rest of this admin. The
    tenant column is here because without it two identical names from two tenants
    are indistinguishable.
    """

    list_display = ('name', 'tenant', 'phone', 'email', 'created_at')
    list_filter = ('tenant',)
    search_fields = ('name', 'phone', 'email')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'tenant', 'created_at')
    list_filter = ('tenant',)
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'tenant', 'duration', 'category', 'created_at')
    list_filter = ('tenant', 'category')
    search_fields = ('name',)
    readonly_fields = ('created_at', 'updated_at')


class AppointmentClientInline(admin.TabularInline):
    model = AppointmentClient
    extra = 1


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ('start', 'end', 'tenant', 'professional', 'service', 'status')
    list_filter = ('tenant', 'status')
    search_fields = ('clients__name',)
    readonly_fields = ('end', 'cancelled_at', 'created_at', 'updated_at')
    inlines = (AppointmentClientInline,)