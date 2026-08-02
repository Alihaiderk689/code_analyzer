from rest_framework import serializers


class ChatMessageSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=['user', 'assistant'])
    # Same cap as ChatRequestSerializer.message below - history entries are
    # client-supplied (unlike chat.SendMessageView, whose history comes from
    # already-validated stored ChatMessage rows), so without this a client
    # could send 20 arbitrarily large entries and inflate the Groq prompt.
    content = serializers.CharField(max_length=8000)


class ChatRequestSerializer(serializers.Serializer):
    message = serializers.CharField()
    history = ChatMessageSerializer(many=True, required=False, default=list)
    analysis_id = serializers.IntegerField(required=False, allow_null=True)

    def validate_message(self, value):
        if not value.strip():
            raise serializers.ValidationError('Message must not be empty.')
        if len(value) > 8000:
            raise serializers.ValidationError('Message must be under 8,000 characters.')
        return value

    def validate_history(self, value):
        return value[-20:]
