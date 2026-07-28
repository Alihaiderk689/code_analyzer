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


class SecurityVulnerabilitySerializer(serializers.Serializer):
    """One entry in a security report's `vulnerabilities` list - shaped for a
    React "vulnerability card" with an expandable-details section."""
    id = serializers.CharField()
    scanner = serializers.CharField()
    rule_id = serializers.CharField()
    vulnerability_type = serializers.CharField()
    severity = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    line_number = serializers.IntegerField(allow_null=True)
    code_snippet = serializers.CharField(allow_blank=True)
    confidence = serializers.CharField(allow_null=True, required=False)
    explanation = serializers.CharField(allow_null=True, required=False)
    remediation = serializers.CharField(allow_null=True, required=False)


class SecuritySummarySerializer(serializers.Serializer):
    critical = serializers.IntegerField()
    high = serializers.IntegerField()
    medium = serializers.IntegerField()
    low = serializers.IntegerField()
    total = serializers.IntegerField()


class SecurityReportSerializer(serializers.Serializer):
    """The full Security Analysis Mode response: overall score + risk badge
    at the top level, then the vulnerability list for the card grid."""
    score = serializers.IntegerField()
    risk_level = serializers.CharField()
    summary = SecuritySummarySerializer()
    vulnerabilities = SecurityVulnerabilitySerializer(many=True)
