from django.contrib.auth import get_user_model
from rest_framework import serializers

from analyses.models import Analysis

User = get_user_model()


class AdminUserSerializer(serializers.ModelSerializer):
    is_verified = serializers.BooleanField(source='profile.is_verified', read_only=True)
    analyses_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'username', 'email', 'first_name', 'last_name',
            'is_active', 'is_staff', 'is_verified', 'analyses_count', 'date_joined',
        ]


class AdminAnalysisSerializer(serializers.ModelSerializer):
    owner_id = serializers.IntegerField(source='owner.id', read_only=True)
    owner_username = serializers.CharField(source='owner.username', read_only=True)
    owner_email = serializers.CharField(source='owner.email', read_only=True)

    class Meta:
        model = Analysis
        fields = [
            'id', 'owner_id', 'owner_username', 'owner_email', 'name', 'language', 'status',
            'quality_score', 'issues_count', 'lines_of_code', 'created_at', 'updated_at',
        ]
