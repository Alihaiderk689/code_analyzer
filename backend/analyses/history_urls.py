from django.urls import path

from . import analysis_views as views

urlpatterns = [
    path('', views.AnalysisListView.as_view(), name='history-list'),
    path('clear/', views.ClearHistoryView.as_view(), name='history-clear'),
    path('<int:pk>/', views.AnalysisDetailView.as_view(), name='history-detail'),
]
