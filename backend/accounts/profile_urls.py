from django.urls import path

from . import views

urlpatterns = [
    path('profile/', views.ProfileView.as_view(), name='user-profile'),
    path('avatar/', views.AvatarUploadView.as_view(), name='user-avatar'),
]
