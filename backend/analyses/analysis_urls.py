from django.urls import path

from . import ai_views, analysis_views as views

urlpatterns = [
    path('', views.AnalysisListView.as_view(), name='analysis-list'),
    path('analyze/', views.AnalyzeView.as_view(), name='analysis-analyze'),
    path('upload/', views.UploadView.as_view(), name='analysis-upload'),
    path('<int:pk>/', views.AnalysisDetailView.as_view(), name='analysis-detail'),
    path('<int:pk>/reanalyze/', views.ReanalyzeView.as_view(), name='analysis-reanalyze'),
    path('<int:pk>/cancel/', views.CancelView.as_view(), name='analysis-cancel'),
    path('<int:pk>/status/', views.StatusView.as_view(), name='analysis-status'),
    path('<int:pk>/suggestions/', ai_views.SuggestionsView.as_view(), name='analysis-suggestions'),
    path('<int:pk>/explanation/', ai_views.ExplanationView.as_view(), name='analysis-explanation'),
    path('<int:pk>/refactor/', ai_views.RefactorView.as_view(), name='analysis-refactor'),
]
