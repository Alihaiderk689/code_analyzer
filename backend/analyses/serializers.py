from rest_framework import serializers

from .models import Analysis


class AnalysisSerializer(serializers.ModelSerializer):
    class Meta:
        model = Analysis
        fields = [
            'id', 'name', 'language', 'status', 'quality_score',
            'issues_count', 'lines_of_code', 'created_at', 'updated_at',
        ]


class AnalysisDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Analysis
        fields = [
            'id', 'name', 'language', 'status', 'quality_score',
            'issues_count', 'lines_of_code', 'issues', 'created_at', 'updated_at',
        ]


class AnalysisStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = Analysis
        fields = ['id', 'status', 'quality_score', 'issues_count', 'updated_at']


class AnalyzeRequestSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True)
    language = serializers.CharField(max_length=50)
    code = serializers.CharField()

    def validate_code(self, value):
        if not value.strip():
            raise serializers.ValidationError('Code must not be empty.')
        if len(value) > 200_000:
            raise serializers.ValidationError('Code must be under 200,000 characters.')
        return value


class UploadRequestSerializer(serializers.Serializer):
    file = serializers.FileField()
    name = serializers.CharField(required=False, allow_blank=True)

    def validate_file(self, value):
        max_size = 2 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError('File must be smaller than 2MB.')
        return value
