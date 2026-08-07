from django.urls import path

from . import views

urlpatterns = [
    path('csrf/', views.CsrfCookieView.as_view(), name='auth-csrf'),
    path('register/', views.RegisterView.as_view(), name='auth-register'),
    path('login/', views.LoginView.as_view(), name='auth-login'),
    path('google/', views.GoogleLoginView.as_view(), name='auth-google-login'),
    path('github/', views.GitHubLoginInitiateView.as_view(), name='auth-github-login'),
    path('logout/', views.LogoutView.as_view(), name='auth-logout'),
    path('refresh/', views.RefreshView.as_view(), name='auth-refresh'),
    path('forgot-password/', views.ForgotPasswordView.as_view(), name='auth-forgot-password'),
    path('reset-password/', views.ResetPasswordView.as_view(), name='auth-reset-password'),
    path('verify-email/', views.VerifyEmailView.as_view(), name='auth-verify-email'),
    path('resend-verification/', views.ResendVerificationView.as_view(), name='auth-resend-verification'),
    path('change-password/', views.ChangePasswordView.as_view(), name='auth-change-password'),
]
