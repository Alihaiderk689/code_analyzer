from django.conf import settings
from django.db import models


class Analysis(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'
        CANCELLED = 'cancelled', 'Cancelled'

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='analyses')
    name = models.CharField(max_length=255)
    language = models.CharField(max_length=50, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    quality_score = models.FloatField(null=True, blank=True)
    issues_count = models.PositiveIntegerField(default=0)
    lines_of_code = models.PositiveIntegerField(default=0)
    issues = models.JSONField(default=list, blank=True)
    source_code = models.TextField(blank=True)
    ai_suggestions = models.JSONField(default=list, blank=True)
    ai_explanation = models.TextField(blank=True)
    ai_refactored_code = models.TextField(blank=True)
    ai_refactor_explanation = models.TextField(blank=True)
    security_report = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'analyses'

    def __str__(self):
        return f'{self.name} ({self.language})'
