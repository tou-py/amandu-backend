# Showcase seed

Backend only. Its companion is the pre-launch polish in `amandu-frontend`
(`docs/specs/pre-launch-polish.md` there). Neither depends on the other.

## Problem Statement

Kyo is about to be advertised, and every screenshot, screen recording and live
demo needs a business that looks real on the day it is captured. The two seeders
do not give one:

- `seed_sandbox` exists to break the front end: overflowing palettes, stacked
  turns, long names, an empty tenant, a client called "a", 15 unclosed turns.
  That is right for testing and wrong for a campaign.
- Both seeders are idempotent by natural key, so dates are fixed the first time
  they run. Two weeks later the current week is empty, every past turn is
  "sin cerrar", and requests are "Vencida".
- The data mixes markets: an `America/Argentina/Buenos_Aires` timezone and `AR`
  country, +54 and +595 phones in the same tenant, and prices in guaraníes
  (the app formats every amount as PYG on purpose).
- Client names include famous scientists (Ada Lovelace, Marie Curie), which
  reads as test data in a screenshot.

The person promoting Kyo has to edit the database by hand before each capture,
or capture a product that looks abandoned.

## Solution

A dev-only command, `seed_showcase`, builds **one** tenant meant to be looked
at: a plausible Paraguayan small business, in Asunción's timezone, priced in
guaraníes, with a believable week. Running it again **re-dates** the showcase
tenant's turns around today, so a capture made any day shows a full current
week, a few things to act on, and a sensible recent past. It never touches any
other tenant.

## User Stories

1. As the person promoting Kyo, I want one command that gives me a business ready to screenshot, so that I do not set data up by hand before each capture.
2. As the person promoting Kyo, I want the current week full of turns whenever I run it, so that the agenda never looks empty in a capture.
3. As the person promoting Kyo, I want to re-run the command on the day of a demo, so that today's turns sit around the time of the demo.
4. As the person promoting Kyo, I want a small, believable number of items in Para hacer (one request, two or three unclosed turns), so that the attention line shows the feature without looking like a neglected business.
5. As the person promoting Kyo, I want some clients to owe money and some to be on a monthly plan, so that Por cobrar and Planes have something to show.
6. As the person promoting Kyo, I want past turns closed (attended, one no-show, charged), so that the client timeline and the charges look lived-in.
7. As the person promoting Kyo, I want every phone to be a Paraguayan number, so that the data matches the guaraní prices.
8. As the person promoting Kyo, I want client and staff names that are ordinary local names, so that nobody reading a screenshot thinks it is test data.
9. As the person promoting Kyo, I want three or four professionals with distinct agenda colours, so that the shared agenda shows what it is for without overflowing.
10. As the person promoting Kyo, I want a service catalogue with prices and categories that fit one trade, so that Servicios reads as one real business.
11. As the person promoting Kyo, I want one login per role (owner, coordinator, staff), so that I can capture what each role sees.
12. As the person promoting Kyo, I want the business name, timezone and country to be consistent with Paraguay, so that times and prices agree.
13. As the person promoting Kyo, I want the public booking page switched on for the showcase tenant, so that I can capture it too.
14. As a developer, I want the command to refuse to run with DEBUG off unless forced, so that known-password users are never created against real data by accident.
15. As a developer, I want re-running to leave the same number of tenants, users, memberships, services, clients and plans, so that the command is safe to run every day.
16. As a developer, I want re-running to replace only the showcase tenant's turns and their charges, so that no other tenant's data is ever deleted.
17. As a developer, I want the dates to be computed in the tenant's timezone, so that "today" is Asunción's today and not UTC's.
18. As a developer, I want the command to print the logins and the tenant slug at the end, so that I can sign in straight away.
19. As a developer, I want `seed_demo` and `seed_sandbox` to stay as they are, so that the existing dev and test flows do not change.

## Implementation Decisions

- **A new command, not a mode of an existing one.** `seed_showcase` lives beside
  `seed_demo` and `seed_sandbox` in the scheduling app's management commands.
  The sandbox's job (exercise edge cases) and this one (look true) pull in
  opposite directions, so they do not share a data table.
- **One tenant, fixed identity.** A fixed slug (for example `showcase`), country
  `PY`, timezone `America/Asuncion`, one trade (a hair and beauty salon is the
  shape the agenda shows best: short turns, several professionals, a group
  service). Its name is invented and generic: no real business, no real person.
  Users have the `.test` domain and the dev password the other seeders use.
  `public_booking` is on, with a business phone, so the public page at
  `/reservar/<slug>/` can be captured too.
- **Two kinds of rows.** *Stable* rows (tenant, users, memberships, working
  hours, categories, services, clients, plans and plan assignments) are fetched
  by natural key with `get_or_create`, as the other seeders do. *Dated* rows
  (appointments, their attendees, their charges and the request) belong to the
  showcase tenant only and are **deleted and rebuilt** on every run, from a table
  of day offsets relative to today in the tenant's timezone.
- **Shape of the week.** From today −14 to today +7. Past turns are closed:
  mostly attended, one no-show, one cancelled, and most attendees charged, with a
  few left owing so that Por cobrar has three to five clients. Today has turns
  before and after the current hour. Exactly one pending public-booking request
  in the coming days, and two or three past turns left unclosed. Turns sit
  within the professionals' working hours.
- **Plans.** Two or three plans in the catalogue. A few clients hold one: one
  up to date, one due within the grace window, so that the plan states show.
- **Deletion is scoped by tenant, inside one transaction**, with the same
  `DEBUG` / `--force` guard as `seed_demo`. The command deletes nothing outside
  the showcase tenant, and that is what the tests pin.
- No model, migration, endpoint or setting changes.

## Testing Decisions

- A good test runs the command through `call_command` and asserts on what a
  person would check in the database. It does not assert on the seeder's
  internal tables.
- Prior art: `apps/scheduling/test_seed_demo.py` (idempotency by counting rows
  across two runs, and refusal with `DEBUG=False`).
- **Refuses in prod:** with `DEBUG=False` and no `--force`, it raises and creates
  nothing.
- **Stable rows are idempotent:** two runs leave the same counts of tenants,
  users, memberships, services, clients and plans.
- **Dated rows follow today:** run with the clock frozen on one date, then again
  frozen two weeks later. The second run's appointments all fall within today −14
  to today +7 of the second date, and at least one is today. Freeze the clock
  with whatever the suite already uses, or `unittest.mock.patch` on
  `django.utils.timezone.now`; do not add a dependency.
- **Other tenants are untouched:** seed another tenant (`seed_demo`), run
  `seed_showcase` twice, and assert the other tenant's appointment count and ids
  are unchanged.
- **One market:** every client phone starts with `+595`, and the tenant country
  is `PY`.
- **Something to show:** after a run there is exactly one pending request, at
  least one unclosed past turn, and at least one client with an amount owed.

## Out of Scope

- Seeding a deployed environment. If a public demo account is wanted on a real
  domain, that is a separate decision (who can log in, how it is reset, and
  whether it is billed).
- A scheduled re-run (cron). It is a command to run by hand before a capture.
- Changes to `seed_demo` or `seed_sandbox`.
- Marketing copy, the landing page, and the screenshots themselves.

## Further Notes

- `PRODUCT.md` in the frontend forbids inventing customers, testimonials or
  metrics. This data is for showing the product's screens and must never be
  presented as a real customer.
- Never use a Japanese name or kanji anywhere in the seeded data.
