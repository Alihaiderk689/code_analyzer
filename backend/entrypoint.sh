#!/bin/sh
# Shared entrypoint for every service built from this image (web, celery_worker
# in docker-compose.yml) - only the gunicorn (web) container runs migrate/
# collectstatic, so two containers starting at once can't race each other
# applying the same migration. celery_worker's docker-compose `depends_on`
# waits on the backend service's healthcheck, which only passes once this has
# already run - so by the time Celery starts, the schema is guaranteed current.
set -e

if [ "$1" = "gunicorn" ]; then
    # Which database is this container actually pointed at? Printed (password
    # masked) before migrate runs, because a stale connection string and a
    # broken database produce the same traceback: migrate names the host only
    # after failing to connect, so there is no way to tell "the platform never
    # picked up my new value" from "the new value is wrong". On Render a
    # service-level env var silently shadows an env-group one, which makes the
    # first case both common and invisible.
    if [ "$ENVIRONMENT" = "production" ]; then _db="$DATABASE_URL_PROD"; else _db="$DATABASE_URL_DEV"; fi
    echo "DB target ($ENVIRONMENT): $(printf '%s' "$_db" | sed -E 's#://([^:]*):[^@]*@#://\1:****@#')"

    echo "Applying database migrations..."
    python manage.py migrate --noinput

    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

exec "$@"
