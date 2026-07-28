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
    # The client's UTC offset in minutes (JS Date.getTimezoneOffset() convention),
    # used to reset the daily chat quota at the user's own local midnight rather
    # than a server-time boundary. Defaults to UTC if the client doesn't send it.
    tz_offset_minutes = serializers.IntegerField(required=False, default=0)

    def validate_message(self, value):
        if not value.strip():
            raise serializers.ValidationError('Message must not be empty.')
        if len(value) > 4_000:
            raise serializers.ValidationError('Message must be under 4,000 characters.')
        return value
