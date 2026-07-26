from django.db import models

from analyses.models import Analysis


class Conversation(models.Model):
    """'Chat with Your Code' follow-up Q&A for a single Analysis. OneToOne (not a
    plain ForeignKey) so "one analysis has one conversation" is enforced by the
    database, not just application code. No source code/analysis data is
    duplicated here - it's always read from `analysis` when a prompt is built."""

    analysis = models.OneToOneField(Analysis, on_delete=models.CASCADE, related_name='conversation')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Conversation for analysis #{self.analysis_id}'


class ChatMessage(models.Model):
    class Role(models.TextChoices):
        USER = 'user', 'User'
        ASSISTANT = 'assistant', 'Assistant'

    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=10, choices=Role.choices)
    message = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f'{self.get_role_display()}: {self.message[:50]}'
