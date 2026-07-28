from rest_framework import serializers

from .models import FileAnalysis, GitHubIntegration, GitHubRepository, PullRequestAnalysis


class GitHubIntegrationSerializer(serializers.ModelSerializer):
    class Meta:
        model = GitHubIntegration
        # access_token is deliberately never exposed via the API, encrypted or not.
        fields = ['id', 'username', 'github_user_id', 'connected_at', 'token_invalid']
        read_only_fields = fields


class GitHubRepositorySerializer(serializers.ModelSerializer):
    class Meta:
        model = GitHubRepository
        fields = ['id', 'repository_id', 'full_name', 'default_branch', 'is_active', 'created_at']
        read_only_fields = fields


class RepositorySelectSerializer(serializers.Serializer):
    repository_id = serializers.IntegerField()
    repository_name = serializers.RegexField(
        r'^[^/\s]+/[^/\s]+$', error_messages={'invalid': 'repository_name must be in "owner/repo" form.'},
    )


class FileAnalysisSerializer(serializers.ModelSerializer):
    class Meta:
        model = FileAnalysis
        fields = ['id', 'file_path', 'language', 'issues', 'score', 'created_at']
        read_only_fields = fields


class PullRequestAnalysisListSerializer(serializers.ModelSerializer):
    repository_name = serializers.CharField(source='repository.full_name', read_only=True)

    class Meta:
        model = PullRequestAnalysis
        fields = [
            'id', 'repository_name', 'pull_request_number', 'title', 'author', 'commit_sha',
            'status', 'overall_score', 'review_posted', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class PullRequestAnalysisDetailSerializer(serializers.ModelSerializer):
    repository_name = serializers.CharField(source='repository.full_name', read_only=True)
    file_analyses = FileAnalysisSerializer(many=True, read_only=True)

    class Meta:
        model = PullRequestAnalysis
        fields = [
            'id', 'repository_name', 'pull_request_number', 'title', 'author', 'commit_sha',
            'status', 'overall_score', 'summary', 'review_posted', 'error',
            'file_analyses', 'created_at', 'updated_at',
        ]
        read_only_fields = fields


class DashboardMetricsSerializer(serializers.Serializer):
    total_prs_analyzed = serializers.IntegerField()
    average_quality_score = serializers.FloatField(allow_null=True)
    critical_vulnerabilities = serializers.IntegerField()
    most_common_issue_type = serializers.CharField(allow_null=True)


class QualityTrendPointSerializer(serializers.Serializer):
    date = serializers.DateField()
    average_score = serializers.FloatField()
    prs_count = serializers.IntegerField()
