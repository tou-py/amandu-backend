#!/bin/sh
# Apply migrations before the server accepts traffic. Without this a deploy that
# ships a migration starts fine and then 500s on the first request touching the
# new column.
#
# If Postgres is not up yet, migrate fails and the container exits: the runtime
# restarts it until the database answers. That is the wait loop -- no need to
# write one.
set -e

python manage.py migrate --noinput

# exec: replace this shell with gunicorn so it becomes PID 1 and receives the
# stop signals directly, instead of the shell swallowing them.
exec "$@"
