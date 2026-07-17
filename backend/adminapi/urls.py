from django.urls import path

from . import views

urlpatterns = [
    path('users/', views.AdminUserListView.as_view(), name='admin-users'),
    path('users/<int:pk>/', views.AdminUserDeleteView.as_view(), name='admin-user-delete'),
    path('analysis/', views.AdminAnalysisListView.as_view(), name='admin-analysis'),
    path('stats/', views.AdminStatsView.as_view(), name='admin-stats'),
]
