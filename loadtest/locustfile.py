"""
Kyo under the traffic it actually gets, modelled on what the SPA sends (see
../amandu-frontend/src/lib/queries.ts and useChangeProbe.ts):

- Opening the agenda is a burst: me, professionals, services, the week on
  screen (page_size=300), its summary, the backlog and the request queue.
- Then the one timer: GET /api/state/ every 10 s while somebody is working,
  and the screens refetch only when its token moves.
- The front desk books, moves and closes turns; a professional marks their own;
  the owner reads the money; strangers browse /reservar/<slug>/ and ask for one.

Expected refusals (a slot just taken, a turn already closed) are counted as
successes: they are the API doing its job, not failures of the system.

Every simulated user sends its own X-Forwarded-For, as Traefik does for each
real client, so the per-IP throttles see many people instead of one. And
X-Forwarded-Proto: https, or the prod settings redirect every request to TLS.
"""
import html
import itertools
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote

from locust import HttpUser, between, task

TENANTS = int(os.environ.get('LOAD_TENANTS', 20))
PASSWORD = 'load12345'
_addresses = itertools.count(1)


def address():
    n = next(_addresses)
    return f'10.{n >> 16 & 255}.{n >> 8 & 255}.{n & 255}'


class Visitor(HttpUser):
    abstract = True

    def on_start(self):
        self.client.headers.update({'X-Forwarded-For': address(), 'X-Forwarded-Proto': 'https'})
        self.slug = f'load-{random.randint(1, TENANTS)}'


class Member(Visitor):
    """A logged-in person working the agenda of one business."""

    abstract = True
    who = 'owner'

    def on_start(self):
        super().on_start()
        self.login()
        self.open_agenda()

    def login(self):
        res = self.client.post('/api/auth/login/', json={'email': f'{self.who}@{self.slug}.test', 'password': PASSWORD})
        res.raise_for_status()
        membership = res.json()['memberships'][0]
        self.membership = membership['membership_id']
        self.client.headers.update({
            'Authorization': f"Bearer {res.json()['access']}",
            'X-Tenant-ID': str(membership['tenant_id']),
        })

    def get(self, url, name=None, **params):
        with self.client.get(url, params=params, name=name or url, catch_response=True) as res:
            if res.status_code == 401:  # the 15-minute access token ran out
                res.success()
                self.login()
            elif res.status_code not in (200, 304):
                res.failure(f'{res.status_code}')
            return res

    def data(self, url, name=None, **params):
        """The body of a GET, or an empty page when it failed (already counted)."""
        res = self.get(url, name, **params)
        return res.json() if res.status_code == 200 else {'results': []}

    def week(self, offset=0):
        monday = date.today() - timedelta(days=date.today().weekday()) + timedelta(weeks=offset)
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()

    def open_agenda(self, offset=0):
        start, end = self.week(offset)
        self.get('/api/auth/me/')
        self.pros = [p['id'] for p in self.data('/api/professionals/') if isinstance(p, dict)]
        self.services = [s['id'] for s in self.data('/api/services/')['results']]
        self.turns = self.data('/api/appointments/', name='/api/appointments/ [week]', **{'from': start, 'to': end, 'page_size': 300})['results']
        self.get('/api/appointments/summary/', **{'from': start, 'to': end})
        self.get('/api/appointments/', name='/api/appointments/ [backlog]', status='scheduled', to=date.today().isoformat())
        self.get('/api/appointments/', name='/api/appointments/ [queue]', status='pending')
        self.get('/api/auth/notifications/')

    @task(10)
    def probe(self):
        self.get('/api/state/')

    @task(2)
    def another_week(self):
        self.open_agenda(random.choice((-1, 1)))

    def close_a_turn(self):
        now = datetime.now(timezone.utc)
        past = [t for t in self.turns if t['status'] == 'scheduled' and t['attendees'] and datetime.fromisoformat(t['end']) < now]
        if not past:
            return
        turn = random.choice(past)
        with self.client.post(
            f"/api/appointments/{turn['id']}/attendance/", name='/api/appointments/{id}/attendance/',
            json={'client': turn['attendees'][0]['id'], 'attendance': 'attended'}, catch_response=True,
        ) as res:
            if res.status_code in (200, 403, 409):  # someone else closed it, or it is not ours
                res.success()


class FrontDesk(Member):
    """Books for the whole team: the heaviest writer."""

    weight = 3
    wait_time = between(3, 10)
    who = 'desk'

    @task(3)
    def book(self):
        clients = self.data('/api/clients/', page=random.randint(1, 6))['results']
        if not (clients and self.pros and self.services):
            return
        day = date.today() + timedelta(days=random.randint(0, 7))
        start = datetime(day.year, day.month, day.day, random.randint(9, 16), random.choice((0, 30)))
        body = {
            'professional': random.choice(self.pros), 'service': random.choice(self.services),
            'clients': [random.choice(clients)['id']], 'start': start.isoformat() + '-03:00',
        }
        with self.client.post('/api/appointments/', json=body, catch_response=True) as res:
            if res.status_code in (201, 400, 409):  # taken, or out of hours: the agenda said no
                res.success()

    @task(2)
    def client_file(self):
        clients = self.data('/api/clients/')['results']
        if not clients:
            return
        one = random.choice(clients)['id']
        self.get(f'/api/clients/{one}/', name='/api/clients/{id}/')
        self.get(f'/api/clients/{one}/timeline/', name='/api/clients/{id}/timeline/')

    @task(2)
    def close(self):
        self.close_a_turn()


class Professional(Member):
    weight = 5
    wait_time = between(5, 15)

    def on_start(self):
        self.who = f'pro{random.randint(1, 3)}'
        super().on_start()

    @task(2)
    def close(self):
        self.close_a_turn()


class Owner(Member):
    """Reads the business: the heavy aggregate queries."""

    weight = 1
    wait_time = between(5, 20)

    @task(2)
    def money(self):
        self.get('/api/receivables/')
        self.get('/api/cash-entries/summary/')
        self.get('/api/cash-entries/')

    @task(1)
    def team(self):
        self.get('/api/auth/members/')
        self.get('/api/tenant/subscription/')


class Stranger(Visitor):
    """The public booking page: server-rendered, slot maths on every view."""

    weight = 2
    wait_time = between(2, 8)

    def page(self, **params):
        with self.client.get(f'/reservar/{self.slug}/', params=params, name='/reservar/{slug}/', catch_response=True) as res:
            if res.status_code == 429:  # one stranger browsing too fast is throttled by design
                res.success()
            return res.text

    @task(5)
    def browse(self):
        self.page()
        services = re.findall(r'[?&;]service=([0-9a-f-]{36})', self.page())
        if services:
            self.page(service=random.choice(services), paso='cuando')

    @task(1)
    def ask_for_a_turn(self):
        body = self.page()
        services = re.findall(r'[?&;]service=([0-9a-f-]{36})', body)
        if not services:
            return
        service = random.choice(services)
        slots = re.findall(r'[?&;]start=([^&"]+)', html.unescape(self.page(service=service, paso='cuando')))
        if not slots:
            return
        form = {
            'service': service, 'start': unquote(random.choice(slots)), 'name': 'Visita',
            'phone': f'+5959820{random.randint(10000, 99999)}',
        }
        with self.client.post(f'/reservar/{self.slug}/', data=form, name='/reservar/{slug}/ [ask]',
                              allow_redirects=False, catch_response=True) as res:
            if res.status_code in (200, 302, 429):  # 200 = the form back with a reason
                res.success()
