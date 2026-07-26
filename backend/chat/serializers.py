from rest_framework import serializers

from .models import ChatMessage, Conversation


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['id', 'role', 'message', 'created_at']


class ConversationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Conversation
        fields = ['id', 'analysis', 'created_at']


class SendMessageSerializer(serializers.Serializer):
    conversation_id = serializers.IntegerField()
    message = serializers.CharField()

    def validate_message(self, value):
        if not value.strip():
            raise serializers.ValidationError('Message must not be empty.')
        if len(value) > 4_000:
            raise serializers.ValidationError('Message must be under 4,000 characters.')
        return value
