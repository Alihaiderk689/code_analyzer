from django.urls import path

from . import webhook_views

urlpatterns = [
    path('github/', webhook_views.GitHubWebhookView.as_view(), name='github-webhook'),
]
