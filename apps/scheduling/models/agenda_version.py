from django.db import models


class AgendaVersion(models.Model):
    """
    A counter per business that moves whenever anything the agenda shows
    changes: turns, who is on them and whether they came, their payments, plans
    and subscriptions, clients, services, the team. /api/state/ hands it out as
    the token the app polls.

    Moved by Postgres triggers (migration 0027), never by Python. That way every
    write path counts, including bulk_create, queryset.update(), the admin and
    commands, and there is no call for a view to forget. The triggers fire at
    commit (DEFERRABLE INITIALLY DEFERRED). A reader that sees the new number
    therefore sees the data, and this row is the last lock a transaction takes,
    so two writers to the same business cannot deadlock on it.

    ponytail: renaming a person (tb_user) moves nothing. The name refreshes on
    the next change or when the tab is refocused. Add a trigger on tb_user over
    its memberships if that ever matters.
    """

    tenant = models.OneToOneField('tenancy.Tenant', on_delete=models.CASCADE, primary_key=True)
    version = models.BigIntegerField(default=0)

    class Meta:
        db_table = 'tb_agenda_version'
