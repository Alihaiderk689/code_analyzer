from django.conf import settings
from django.db import models

from analyses.models import Analysis

from .services.encryption import decrypt_token, encrypt_token


class GitHubIntegration(models.Model):
    """One GitHub account connected per app user (OneToOne, matching 'a user
    connects their GitHub account' - not multiple GitHub accounts per user)."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='github_integration')
    github_user_id = models.BigIntegerField(unique=True)
    username = models.CharField(max_length=255)
    # Encrypted at rest (Fernet) - never stored or returned as plaintext. Use
    # get_access_token()/set_access_token() below rather than touching this directly.
    access_token = models.BinaryField()
    connected_at = models.DateTimeField(auto_now_add=True)
    # Set when a GitHub API call comes back 401 (token revoked/expired) - lets
    # the UI prompt "reconnect" instead of Celery tasks retrying forever
    # against a token that will never work again.
    token_invalid = models.BooleanField(default=False)

    def __str__(self):
        return f'GitHubIntegration({self.username})'

    def set_access_token(self, raw_token: str) -> None:
        self.access_token = encrypt_token(raw_token)

    def get_access_token(self) -> str:
        return decrypt_token(bytes(self.access_token))


class GitHubRepository(models.Model):
    integration = models.ForeignKey(GitHubIntegration, on_delete=models.CASCADE, related_name='repositories')
    repository_id = models.BigIntegerField()  # GitHub's numeric repo id, stable across renames
    full_name = models.CharField(max_length=255)  # "owner/repo"
    default_branch = models.CharField(max_length=255, default='main')
    webhook_id = models.BigIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # A user can only monitor a given GitHub repo once, not create
        # duplicate rows (and duplicate webhooks) for the same repo.
        constraints = [
            models.UniqueConstraint(fields=['integration', 'repository_id'], name='unique_repo_per_integration'),
        ]
        ordering = ['-created_at']
        verbose_name_plural = 'GitHub repositories'

    def __str__(self):
        return self.full_name


class PullRequestAnalysis(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        RUNNING = 'running', 'Running'
        COMPLETED = 'completed', 'Completed'
        FAILED = 'failed', 'Failed'

    repository = models.ForeignKey(GitHubRepository, on_delete=models.CASCADE, related_name='pull_request_analyses')
    pull_request_number = models.PositiveIntegerField()
    commit_sha = models.CharField(max_length=40)
    title = models.CharField(max_length=500, blank=True)
    author = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    overall_score = models.FloatField(null=True, blank=True)
    summary = models.TextField(blank=True)
    review_posted = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # One analysis per (repo, PR, commit) - a "synchronize" event (new
        # commits pushed) naturally gets its own row via the new commit_sha;
        # re-delivery of the *same* webhook for the *same* commit is a no-op
        # rather than a duplicate row, which is the real defense against
        # duplicate/retried webhook deliveries (see WebhookEvent for the
        # transport-level defense).
        constraints = [
            models.UniqueConstraint(
                fields=['repository', 'pull_request_number', 'commit_sha'], name='unique_pr_commit_analysis',
            ),
        ]
        ordering = ['-created_at']
        verbose_name_plural = 'pull request analyses'

    def __str__(self):
        return f'{self.repository.full_name}#{self.pull_request_number} @ {self.commit_sha[:7]}'


class FileAnalysis(models.Model):
    pull_request_analysis = models.ForeignKey(PullRequestAnalysis, on_delete=models.CASCADE, related_name='file_analyses')
    file_path = models.CharField(max_length=1000)
    language = models.CharField(max_length=50, blank=True)
    # Unified issue list - each item shaped like:
    # {source: 'security'|'quality'|'performance', severity, type, line, message, explanation, remediation}
    issues = models.JSONField(default=list, blank=True)
    score = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['file_path']
        verbose_name_plural = 'file analyses'

    def __str__(self):
        return self.file_path


class RepositoryFileCheck(models.Model):
    """One row per on-demand 'analyze this file' click (see pr_analysis_service
    .PRAnalysisService.analyze_file_by_path) - deliberately a log, not a
    counter, so the daily quota (see file_check_rate_limit.py) is derived live
    from these rows the same way chat/rate_limit.py derives its own daily
    quota from ChatMessage rows, rather than a separate counter that could
    drift out of sync."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='github_file_checks')
    repository = models.ForeignKey(GitHubRepository, on_delete=models.CASCADE, related_name='file_checks')
    path = models.CharField(max_length=1000)
    language = models.CharField(max_length=50, blank=True)
    issues = models.JSONField(default=list, blank=True)
    score = models.FloatField(null=True, blank=True)
    # Backs the "chat about this file" feature - reuses the exact same
    # Analysis + chat.Conversation machinery the paste/upload flow uses
    # (same UI component, same 3-messages/day quota), instead of building a
    # parallel chat system. Null only for skipped-file checks, which are
    # never persisted as a RepositoryFileCheck row in the first place - kept
    # nullable regardless since SET_NULL must point at a nullable field.
    analysis = models.ForeignKey(Analysis, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.repository.full_name}:{self.path}'


class WebhookEvent(models.Model):
    """Raw log of every webhook delivery received, independent of whether we
    acted on it - `delivery_id` is GitHub's own per-delivery GUID
    (X-GitHub-Delivery header). The unique constraint is the first line of
    defense against duplicate deliveries; PullRequestAnalysis's own uniqueness
    is the second (see its Meta docstring) since a genuine redelivery can
    arrive with a *different* delivery_id for the same logical event."""

    event_type = models.CharField(max_length=100)  # X-GitHub-Event header, e.g. "pull_request"
    delivery_id = models.CharField(max_length=100, unique=True)
    payload = models.JSONField()
    processed = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.event_type} ({self.delivery_id})'
