from django.urls import path

from . import oauth_views, pr_views, repository_views

urlpatterns = [
    path('login/', oauth_views.GitHubLoginView.as_view(), name='github-login'),
    path('callback/', oauth_views.GitHubCallbackView.as_view(), name='github-callback'),
    path('disconnect/', oauth_views.GitHubDisconnectView.as_view(), name='github-disconnect'),
    path('status/', oauth_views.GitHubIntegrationStatusView.as_view(), name='github-status'),

    path('repositories/', repository_views.RepositoryListView.as_view(), name='github-repositories'),
    path('repositories/select/', repository_views.RepositorySelectView.as_view(), name='github-repositories-select'),
    path('repositories/monitored/', repository_views.MonitoredRepositoryListView.as_view(), name='github-repositories-monitored'),
    path('repositories/<int:pk>/', repository_views.RepositoryDeselectView.as_view(), name='github-repositories-deselect'),
    path('repositories/<int:pk>/tree/', repository_views.RepositoryTreeView.as_view(), name='github-repository-tree'),
    path('repositories/<int:pk>/file/', repository_views.RepositoryFileContentView.as_view(), name='github-repository-file'),
    path(
        'repositories/<int:pk>/analyze-file/', repository_views.RepositoryFileAnalyzeView.as_view(),
        name='github-repository-analyze-file',
    ),
    path(
        'repositories/<int:pk>/analyze-file-context/', repository_views.RepositoryFileContextAnalyzeView.as_view(),
        name='github-repository-analyze-file-context',
    ),
    path('repositories/<int:pk>/index/', repository_views.RepositoryIndexStatusView.as_view(), name='github-repository-index'),
    path('repositories/<int:pk>/reindex/', repository_views.RepositoryReindexView.as_view(), name='github-repository-reindex'),

    path('file-checks/quota/', repository_views.FileCheckQuotaView.as_view(), name='github-file-check-quota'),
    path('context-checks/quota/', repository_views.ContextCheckQuotaView.as_view(), name='github-context-check-quota'),

    path('pull-requests/', pr_views.PullRequestAnalysisListView.as_view(), name='github-pull-requests'),
    path('pull-requests/trends/', pr_views.QualityTrendsView.as_view(), name='github-pull-requests-trends'),
    path('pull-requests/<int:pk>/', pr_views.PullRequestAnalysisDetailView.as_view(), name='github-pull-request-detail'),
    path('metrics/', pr_views.DashboardMetricsView.as_view(), name='github-metrics'),
]
