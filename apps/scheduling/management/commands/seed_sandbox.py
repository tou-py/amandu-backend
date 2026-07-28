"""
Dev-only seeder: five tenants of different trades, sized and shaped to exercise
the front end rather than to look plausible in a screenshot.

`seed_demo` builds ONE minimal tenant, which is what a fresh database needs to
be loggable-into. This builds a playground: enough professionals to overflow the
agenda's six-colour palette, appointments stacked in the same slot, every
booking status and attendance value, long names, empty fields and one tenant
with nothing in it at all -- so the empty states have something to be empty
about.

What each tenant is here to prove is written on its entry in TENANTS below.

Idempotent: every row is fetched by its natural key, so re-running changes
nothing. Safe to re-run.
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Invitation, Membership
from apps.scheduling.models import Appointment, AppointmentClient, Category, Client, Service
from apps.tenancy.models import Tenant

User = get_user_model()

PASSWORD = 'demo12345'

OWNER = Membership.Role.OWNER
ADMIN = Membership.Role.ADMIN
COORDINATOR = Membership.Role.COORDINATOR
STAFF = Membership.Role.STAFF

SCHEDULED = Appointment.Status.SCHEDULED
COMPLETED = Appointment.Status.COMPLETED
CANCELLED = Appointment.Status.CANCELLED

PENDING = AppointmentClient.Attendance.PENDING
ATTENDED = AppointmentClient.Attendance.ATTENDED
NO_SHOW = AppointmentClient.Attendance.NO_SHOW
LATE_CANCEL = AppointmentClient.Attendance.LATE_CANCEL

# The address that belongs to three tenants at once. It is the only way to see
# the space switcher in the header, so it is called out rather than buried.
MULTI = 'multi@amandu.test'

# people: (email, role, attends_appointments, display name)
# services: (name, minutes, category)
# clients: (name, phone, email, notes) -- any of the last three may be blank,
#          which is what makes a field disappear from the card on a phone.
# appointments: (day offset from today, hour, minute, staff index, service
#          index, [client indexes], status, notes)
TENANTS = [
    {
        # The busy one. Seven professionals against a six-colour palette, so the
        # seventh reuses the first hue -- the known limit, made visible instead
        # of waiting to be discovered by a real salon.
        'slug': 'salon-lumiere',
        'name': 'Salón Lumière',
        'timezone': 'America/Argentina/Buenos_Aires',
        'country': 'AR',
        'people': [
            ('carla@lumiere.test', OWNER, True, 'Carla Benítez'),
            # Coordinates without attending: the front desk.
            ('recepcion@lumiere.test', COORDINATOR, False, 'Rocío Paz'),
            (MULTI, ADMIN, True, 'Sebastián Torres'),
            # Attends AND coordinates: the multitasking professional the role
            # was added for. Compare against the plain staff below, who can only
            # book their own day.
            ('marta@lumiere.test', COORDINATOR, True, 'Marta Quiroga'),
            ('juli@lumiere.test', STAFF, True, 'Julián Ferreyra'),
            ('nadia@lumiere.test', STAFF, True, 'Nadia Sosa'),
            ('leo@lumiere.test', STAFF, True, 'Leonardo Iglesias'),
            ('vera@lumiere.test', STAFF, True, 'Vera Antonelli'),
        ],
        'services': [
            ('Corte y peinado', 45, 'Peluquería'),
            ('Coloración completa con tratamiento de nutrición', 120, 'Coloración'),
            ('Reflejos', 90, 'Coloración'),
            ('Brushing', 30, 'Peluquería'),
            ('Manicura', 30, 'Manos y pies'),
            ('Pedicura', 45, 'Manos y pies'),
        ],
        'clients': [
            ('Ada Lovelace', '+5491133445566', 'ada@correo.test', 'Alérgica al amoníaco.'),
            ('Grace Hopper', '+5491144556677', '', ''),
            ('Rosalind Franklin', '', 'rosalind@correo.test', 'Prefiere turnos temprano.'),
            ('Hedy Lamarr', '+5491155667788', '', 'Viene con su hija.'),
            ('Marie Curie', '+5491166778899', 'marie@correo.test', ''),
            ('Chien-Shiung Wu', '+5491177889900', '', ''),
        ],
        'appointments': [
            # Three professionals booked at the same hour: side by side in the
            # day view, collapsed into "+N más" in the week view.
            (0, 10, 0, 0, 0, [0], SCHEDULED, 'Retoca la raíz.'),
            (0, 10, 0, 3, 4, [1], SCHEDULED, ''),
            (0, 10, 0, 4, 3, [2], SCHEDULED, ''),
            (0, 10, 30, 5, 4, [3], SCHEDULED, ''),
            # The long one: a 120-minute block with room for every line.
            (0, 11, 0, 0, 1, [4], SCHEDULED, 'Trajo la foto de referencia.'),
            (0, 14, 0, 6, 2, [5], SCHEDULED, ''),
            (0, 16, 0, 2, 0, [0], SCHEDULED, ''),
            # Yesterday: what a finished day looks like.
            (-1, 9, 0, 0, 0, [1], COMPLETED, ''),
            (-1, 10, 0, 3, 5, [2], COMPLETED, 'Pidió esmalte permanente.'),
            (-1, 11, 0, 4, 3, [3], CANCELLED, ''),
            # The rest of the week, so the week view and the phone list have
            # something to show on every day.
            (1, 9, 30, 0, 3, [4], SCHEDULED, ''),
            (1, 12, 0, 5, 0, [5], SCHEDULED, ''),
            (2, 15, 0, 6, 1, [0], SCHEDULED, ''),
            (3, 11, 0, 3, 4, [1], SCHEDULED, ''),
            (4, 17, 0, 4, 0, [2], SCHEDULED, 'Última del día.'),
        ],
        'invitations': [('nueva.colega@lumiere.test', STAFF)],
    },
    {
        # Short services back to back: 20 and 30 minutes, which is where the
        # appointment card has to drop a line to fit.
        'slug': 'barberia-el-corte',
        'name': 'Barbería El Corte',
        'timezone': 'America/Argentina/Buenos_Aires',
        'country': 'AR',
        'people': [
            ('dario@elcorte.test', OWNER, True, 'Darío Miranda'),
            (MULTI, STAFF, True, 'Sebastián Torres'),
            ('tincho@elcorte.test', STAFF, True, 'Martín Ojeda'),
        ],
        'services': [
            ('Corte', 30, 'Cortes'),
            ('Corte y barba', 45, 'Cortes'),
            ('Perfilado de barba', 20, 'Barbería'),
            ('Afeitado clásico', 30, 'Barbería'),
        ],
        'clients': [
            ('Juan Pablo Sánchez', '+5491198765432', '', ''),
            ('Facundo Ríos', '+5491187654321', 'facu@correo.test', ''),
            ('Emiliano Vega', '', '', 'Paga siempre en efectivo.'),
        ],
        'appointments': [
            (0, 9, 0, 0, 2, [0], SCHEDULED, ''),
            (0, 9, 30, 0, 0, [1], SCHEDULED, ''),
            (0, 10, 0, 0, 1, [2], SCHEDULED, ''),
            (0, 9, 0, 1, 3, [1], SCHEDULED, ''),
            (0, 11, 0, 2, 2, [0], SCHEDULED, ''),
            (-1, 16, 0, 0, 0, [2], COMPLETED, ''),
            (1, 10, 0, 2, 1, [0], SCHEDULED, ''),
            (2, 18, 0, 0, 3, [1], SCHEDULED, ''),
        ],
        'invitations': [],
    },
    {
        # Group bookings and long sessions: several clients on one appointment,
        # which is what the attendance controls in the dialog are for.
        'slug': 'zen-spa',
        'name': 'Zen Spa & Bienestar',
        'timezone': 'America/Mexico_City',
        'country': 'MX',
        'people': [
            ('lucia@zenspa.test', OWNER, False, 'Lucía Mendoza'),
            (MULTI, STAFF, True, 'Sebastián Torres'),
            ('ana@zenspa.test', STAFF, True, 'Ana Karina Rojas'),
            ('paulo@zenspa.test', STAFF, True, 'Paulo Restrepo'),
        ],
        'services': [
            ('Masaje descontracturante', 60, 'Masajes'),
            ('Masaje en pareja', 90, 'Masajes'),
            ('Facial hidratante', 45, 'Faciales'),
            ('Ritual de piedras calientes', 90, 'Rituales'),
        ],
        'clients': [
            ('Renata Ortega', '+525512345678', 'renata@correo.test', ''),
            ('Iván Domínguez', '+525587654321', '', ''),
            ('Sofía Aguilar', '+525511223344', 'sofia@correo.test', 'Embarazada, evitar aceites cítricos.'),
            ('Bruno Salas', '+525599887766', '', ''),
        ],
        'appointments': [
            # Two people on one booking, each with their own attendance.
            (0, 11, 0, 0, 1, [0, 1], SCHEDULED, 'Aniversario. Preparar la sala grande.'),
            (0, 13, 0, 1, 0, [2], SCHEDULED, ''),
            (0, 15, 0, 2, 3, [3], SCHEDULED, ''),
            (-2, 10, 0, 0, 1, [0, 1], COMPLETED, ''),
            (-1, 12, 0, 1, 2, [2], COMPLETED, ''),
            (1, 10, 0, 2, 0, [3], SCHEDULED, ''),
            (3, 16, 0, 0, 3, [0], SCHEDULED, ''),
        ],
        'invitations': [('terapeuta@zenspa.test', STAFF), ('gerencia@zenspa.test', ADMIN)],
    },
    {
        # The one with a messy history: cancellations with reasons, absences,
        # late cancellations. Everything the status lozenges have to render.
        'slug': 'kine-movimiento',
        'name': 'Kinesiología Movimiento',
        'timezone': 'Europe/Madrid',
        'country': 'ES',
        'people': [
            ('elena@movimiento.test', OWNER, True, 'Elena Vidal'),
            ('marc@movimiento.test', STAFF, True, 'Marc Puig'),
        ],
        'services': [
            ('Sesión de rehabilitación', 45, 'Rehabilitación'),
            ('Primera consulta', 60, 'Consultas'),
            ('Punción seca', 30, ''),
        ],
        'clients': [
            ('Núria Camps', '+34612345678', 'nuria@correo.test', 'Lesión de hombro derecho.'),
            ('Álvaro Ibáñez', '+34698765432', '', ''),
            ('Teresa Lorenzo', '+34677889900', 'teresa@correo.test', ''),
        ],
        'appointments': [
            (0, 9, 0, 0, 1, [0], SCHEDULED, ''),
            (0, 10, 0, 0, 0, [1], SCHEDULED, ''),
            (0, 12, 0, 1, 2, [2], SCHEDULED, ''),
            # Two people on one finished booking, one absent and one who called
            # too late: the case the model exists for -- an absence describes the
            # person, not the slot.
            (-3, 9, 0, 0, 1, [0, 1], COMPLETED, 'Sesión conjunta.'),
            (-3, 11, 0, 1, 0, [1], COMPLETED, ''),
            (-2, 9, 0, 0, 0, [2], CANCELLED, ''),
            (-1, 10, 0, 1, 1, [0], COMPLETED, 'Revisar progreso en dos semanas.'),
            (2, 9, 0, 0, 0, [1], SCHEDULED, ''),
            (5, 11, 0, 1, 2, [2], SCHEDULED, ''),
        ],
        'invitations': [],
    },
    {
        # Deliberately bare: one professional, no services, no clients, no
        # appointments. This is the tenant that proves the empty states read as
        # a starting point rather than as a failure.
        'slug': 'tinta-negra',
        'name': 'Estudio Tinta Negra',
        'timezone': 'America/Santiago',
        'country': 'CL',
        'people': [
            (MULTI, OWNER, True, 'Sebastián Torres'),
        ],
        'services': [],
        'clients': [],
        'appointments': [],
        'invitations': [('artista@tintanegra.test', STAFF)],
    },
]


class Command(BaseCommand):
    help = 'Seed five demo tenants covering the front end\'s edge cases (dev only). Idempotent.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Run even when DEBUG is False. This creates users with a known '
                 'password -- never point it at real data.',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Delete these five tenants and their data first. Only the slugs '
                 'listed in this file are touched; nothing else in the database is.',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        # A seeder that mints known-password users must fail closed outside dev.
        if not settings.DEBUG and not options['force']:
            raise CommandError(
                'Refusing to seed with DEBUG=False. Pass --force only against a '
                'throwaway database.'
            )

        if options['reset']:
            self.reset()

        skipped = 0
        for spec in TENANTS:
            skipped += self.seed_tenant(spec)

        self.report(skipped)

    def reset(self):
        """
        Wipe the sandbox tenants so the dataset can be edited and re-seeded.

        Scoped to the slugs in TENANTS -- `demo` and anything real are left
        alone. The order is explicit because Appointment PROTECTs its
        professional, service and clients, so a plain tenant delete would be
        refused rather than cascading.
        """
        tenants = Tenant.objects.filter(slug__in=[spec['slug'] for spec in TENANTS])
        if not tenants.exists():
            return

        appointments = Appointment.objects.filter(tenant__in=tenants)
        AppointmentClient.objects.filter(appointment__in=appointments).delete()
        appointments.delete()
        Client.objects.filter(tenant__in=tenants).delete()
        Service.objects.filter(tenant__in=tenants).delete()
        Category.objects.filter(tenant__in=tenants).delete()
        Invitation.objects.filter(tenant__in=tenants).delete()
        Membership.objects.filter(tenant__in=tenants).delete()
        count = tenants.count()
        tenants.delete()
        self.stdout.write(self.style.WARNING(f'Reset: removed {count} sandbox tenant(s).'))

    def seed_tenant(self, spec):
        tenant, _ = Tenant.objects.get_or_create(
            slug=spec['slug'],
            defaults={
                'name': spec['name'],
                'timezone': spec['timezone'],
                'country': spec['country'],
            },
        )

        staff = []
        first_admin = None
        for email, role, attends, display in spec['people']:
            first, _, last = display.partition(' ')
            user, _ = User.objects.get_or_create(
                email=email, defaults={'first_name': first, 'last_name': last}
            )
            # Reset every run so the password is always the documented one, even
            # if an earlier seed used a different value.
            user.set_password(PASSWORD)
            user.save(update_fields=['password'])

            membership, _ = Membership.objects.get_or_create(
                user=user,
                tenant=tenant,
                defaults={'role': role, 'attends_appointments': attends},
            )
            if attends:
                staff.append(membership)
            if first_admin is None and role in (OWNER, ADMIN):
                first_admin = membership

        categories = {}
        services = []
        for name, minutes, label in spec['services']:
            category = None
            if label:
                if label not in categories:
                    categories[label], _ = Category.objects.get_or_create(
                        tenant=tenant, name=label
                    )
                category = categories[label]
            service, _ = Service.objects.get_or_create(
                tenant=tenant,
                name=name,
                defaults={'duration': timedelta(minutes=minutes), 'category': category},
            )
            services.append(service)

        clients = []
        for name, phone, email, notes in spec['clients']:
            # Keyed on the phone where there is one -- that is the tenant's own
            # identity rule -- and on the name where there is not, since the
            # unique constraint deliberately ignores blank phones. The key is
            # kept out of `defaults` or get_or_create would pass it twice.
            fields = {'name': name, 'phone': phone, 'email': email, 'notes': notes}
            key = 'phone' if phone else 'name'
            client, _ = Client.objects.get_or_create(
                tenant=tenant,
                **{key: fields.pop(key)},
                defaults=fields,
            )
            clients.append(client)

        tz = ZoneInfo(tenant.timezone)
        today = timezone.now().astimezone(tz).date()
        skipped = 0

        for offset, hour, minute, who, what, guests, status, notes in spec['appointments']:
            professional = staff[who]
            service = services[what]
            start = datetime.combine(today + timedelta(days=offset), time(hour, minute), tzinfo=tz)

            # A savepoint per appointment: the exclusion constraint is the one
            # thing a hand-written dataset gets wrong, and one bad row should
            # report itself rather than roll back the other four tenants.
            try:
                with transaction.atomic():
                    appointment, created = Appointment.objects.get_or_create(
                        professional=professional,
                        start=start,
                        defaults={
                            'tenant': tenant,
                            'service': service,
                            'end': start + service.duration,
                            'status': status,
                            'notes': notes,
                            'cancelled_at': timezone.now() if status == CANCELLED else None,
                            'cancellation_reason': (
                                'El cliente avisó que no podía venir.' if status == CANCELLED else ''
                            ),
                        },
                    )
            except IntegrityError:
                self.stderr.write(
                    f'  overlap skipped: {tenant.slug} {start:%Y-%m-%d %H:%M} '
                    f'({professional.user.email})'
                )
                skipped += 1
                continue

            if not created:
                continue

            for position, guest in enumerate(guests):
                link = AppointmentClient.objects.create(
                    appointment=appointment, client=clients[guest]
                )
                # Attendance only means something once the booking has run. A
                # cancelled one never did, so everyone in it stays pending.
                if status != COMPLETED:
                    continue
                # One finished day carries the awkward outcomes, so all four
                # attendance values exist somewhere in the data instead of every
                # past booking reading as a tidy "attended".
                link.attendance = (
                    (NO_SHOW, LATE_CANCEL)[position % 2] if offset == -3 else ATTENDED
                )
                link.save(update_fields=['attendance'])

        for email, role in spec['invitations']:
            Invitation.objects.get_or_create(
                tenant=tenant,
                email=email,
                status=Invitation.Status.PENDING,
                defaults={'role': role, 'invited_by': first_admin},
            )

        return skipped

    def report(self, skipped):
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(f'Seeded {len(TENANTS)} tenants. Password: {PASSWORD}'))
        self.stdout.write('')
        shared = sum(
            1 for spec in TENANTS if any(email == MULTI for email, *_ in spec['people'])
        )
        self.stdout.write(
            f'  {MULTI}  belongs to {shared} of them -- use it to test the space switcher'
        )
        self.stdout.write('')
        for spec in TENANTS:
            tenant = Tenant.objects.get(slug=spec['slug'])
            owner = next(email for email, role, _, _ in spec['people'] if role == OWNER)
            self.stdout.write(
                f'  [{tenant.id}] {tenant.name:<28} {owner:<28} '
                f'{len(spec["people"])} people, {len(spec["appointments"])} appointments'
            )
        if skipped:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(f'{skipped} appointment(s) skipped as overlapping.'))
