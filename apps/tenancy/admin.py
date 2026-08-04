import calendar
from datetime import date

from django.contrib import admin
from django.utils import timezone

from apps.tenancy.models import Tenant


def one_month_after(day: date) -> date:
    """
    The same day next month, clamped to a day that exists there: a period paid on
    the 31st ends on the 30th, not on the 1st of the month after. Written out
    rather than pulled from dateutil, which is not a dependency of this project
    and would be a whole package for these four lines.
    """
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'status', 'paid_until', 'timezone', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'slug')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('created_at', 'updated_at')
    actions = ('register_monthly_payment',)

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
