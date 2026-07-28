from django.contrib import admin

from .models import FileAnalysis, GitHubIntegration, GitHubRepository, PullRequestAnalysis, WebhookEvent

admin.site.register(GitHubIntegration)
admin.site.register(GitHubRepository)
admin.site.register(PullRequestAnalysis)
admin.site.register(FileAnalysis)
admin.site.register(WebhookEvent)
