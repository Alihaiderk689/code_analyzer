"""Celery application for background work - currently just the GitHub PR
analysis pipeline (github_integration/tasks.py). A webhook must respond to
GitHub in a few seconds or GitHub marks the delivery failed and retries it,
so the actual fetch-analyze-comment pipeline runs here, off the request path.
"""
import os

from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
# Reads every CELERY_* setting from Django settings.py (namespace='CELERY' means
# CELERY_BROKER_URL in settings.py becomes broker_url here, etc.).
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()
