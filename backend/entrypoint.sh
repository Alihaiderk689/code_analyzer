#!/bin/sh
# Shared entrypoint for every service built from this image (web, celery_worker
# in docker-compose.yml) - only the gunicorn (web) container runs migrate/
# collectstatic, so two containers starting at once can't race each other
# applying the same migration. celery_worker's docker-compose `depends_on`
# waits on the backend service's healthcheck, which only passes once this has
# already run - so by the time Celery starts, the schema is guaranteed current.
set -e

if [ "$1" = "gunicorn" ]; then
    echo "Applying database migrations..."
    python manage.py migrate --noinput

    echo "Collecting static files..."
    python manage.py collectstatic --noinput
fi

exec "$@"
