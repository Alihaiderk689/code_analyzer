from django.urls import path

from . import views

urlpatterns = [
    path('start/<int:analysis_id>/', views.StartConversationView.as_view(), name='chat-start'),
    path('message/', views.SendMessageView.as_view(), name='chat-message'),
    path('history/<int:conversation_id>/', views.ChatHistoryView.as_view(), name='chat-history'),
    path('limit/', views.RateLimitStatusView.as_view(), name='chat-limit'),
]
