from django.urls import path

from . import views

urlpatterns = [
    path('', views.DashboardSummaryView.as_view(), name='dashboard-summary'),
    path('stats/', views.DashboardStatsView.as_view(), name='dashboard-stats'),
    path('recent/', views.RecentAnalysesView.as_view(), name='dashboard-recent'),
    path('languages/', views.LanguageUsageView.as_view(), name='dashboard-languages'),
    path('scores/', views.QualityScoresView.as_view(), name='dashboard-scores'),
]
