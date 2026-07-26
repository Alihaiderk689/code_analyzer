from django.contrib import admin

from .models import ChatMessage, Conversation

admin.site.register(Conversation)
admin.site.register(ChatMessage)
