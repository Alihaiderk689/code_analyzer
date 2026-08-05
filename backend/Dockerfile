FROM python:3.11-slim

# psycopg2-binary still needs libpq at runtime; gcc/libpq-dev cover building
# any package here that doesn't ship a prebuilt wheel for this platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Don't buffer stdout/stderr (logs must appear immediately in `docker logs`,
# not sit in a buffer) and don't write .pyc files into the image layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as a non-root user - the app never needs root inside the container,
# and it's one less thing a container-escape bug could leverage.
RUN useradd --create-home appuser \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["./entrypoint.sh"]
# Overridden by docker-compose.yml's `command:` for the celery_worker service -
# this is just the default when the container's run with no command override.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
