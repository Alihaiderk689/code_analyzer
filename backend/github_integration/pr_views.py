from collections import Counter

from django.db.models import Avg, Count
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FileAnalysis, PullRequestAnalysis
from .serializers import (
    DashboardMetricsSerializer,
    PullRequestAnalysisDetailSerializer,
    PullRequestAnalysisListSerializer,
    QualityTrendPointSerializer,
)


class PullRequestAnalysisPagination(PageNumberPagination):
    # PR analyses accumulate continuously from webhook traffic (unlike the
    # manually-run analyses elsewhere in this app), so - unlike those - this
    # listing genuinely needs real pagination, not just count+results.
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class PullRequestAnalysisListView(APIView):
    """GET /api/github/pull-requests/ - every PR analysis across all
    repositories the user monitors, newest first. Optional ?repository_id=
    filters to one repository."""

    pagination_class = PullRequestAnalysisPagination

    def get(self, request):
        queryset = PullRequestAnalysis.objects.filter(
            repository__integration__user=request.user,
        ).select_related('repository')

        repository_id = request.query_params.get('repository_id')
        if repository_id:
            queryset = queryset.filter(repository_id=repository_id)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request)
        serializer = PullRequestAnalysisListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class PullRequestAnalysisDetailView(APIView):
    """GET /api/github/pull-requests/<pk>/ - full detail including every
    analyzed file and its issues, for the PR Analysis Details page."""

    def get(self, request, pk):
        pr_analysis = get_object_or_404(
            PullRequestAnalysis.objects.select_related('repository').prefetch_related('file_analyses'),
            pk=pk, repository__integration__user=request.user,
        )
        return Response(PullRequestAnalysisDetailSerializer(pr_analysis).data)


class DashboardMetricsView(APIView):
    """GET /api/github/metrics/ - the numbers the dashboard requirements ask
    for: total PRs analysed, average quality score, critical vulnerabilities,
    most common issue type, scoped to the current user's repositories (or to
    one, via ?repository_id=, matching the list/trends endpoints below)."""

    def get(self, request):
        analyses = PullRequestAnalysis.objects.filter(
            repository__integration__user=request.user, status=PullRequestAnalysis.Status.COMPLETED,
        )

        repository_id = request.query_params.get('repository_id')
        if repository_id:
            analyses = analyses.filter(repository_id=repository_id)

        scores = list(analyses.exclude(overall_score__isnull=True).values_list('overall_score', flat=True))
        average_score = round(sum(scores) / len(scores), 1) if scores else None

        critical_count = 0
        issue_type_counts = Counter()
        for issues in FileAnalysis.objects.filter(pull_request_analysis__in=analyses).values_list('issues', flat=True):
            for issue in issues:
                issue_type_counts[issue['type']] += 1
                if issue['severity'] == 'critical':
                    critical_count += 1

        most_common = issue_type_counts.most_common(1)

        data = {
            'total_prs_analyzed': analyses.count(),
            'average_quality_score': average_score,
            'critical_vulnerabilities': critical_count,
            'most_common_issue_type': most_common[0][0] if most_common else None,
        }
        return Response(DashboardMetricsSerializer(data).data)


class QualityTrendsView(APIView):
    """GET /api/github/pull-requests/trends/ - average quality score per day
    over the trailing window (?days=, default 30), optionally scoped to one
    repository (?repository_id=). Powers the "Code Quality Trends" chart -
    unlike DashboardMetricsView's running totals, this is a real time series."""

    DEFAULT_DAYS = 30
    MAX_DAYS = 365

    def get(self, request):
        try:
            days = int(request.query_params.get('days', self.DEFAULT_DAYS))
        except ValueError:
            days = self.DEFAULT_DAYS
        days = max(1, min(days, self.MAX_DAYS))

        queryset = PullRequestAnalysis.objects.filter(
            repository__integration__user=request.user,
            status=PullRequestAnalysis.Status.COMPLETED,
            overall_score__isnull=False,
            created_at__gte=timezone.now() - timezone.timedelta(days=days),
        )

        repository_id = request.query_params.get('repository_id')
        if repository_id:
            queryset = queryset.filter(repository_id=repository_id)

        points = (
            queryset
            .annotate(date=TruncDate('created_at'))
            .values('date')
            .annotate(average_score=Avg('overall_score'), prs_count=Count('id'))
            .order_by('date')
        )
        data = [
            {'date': p['date'], 'average_score': round(p['average_score'], 1), 'prs_count': p['prs_count']}
            for p in points
        ]
        return Response({'results': QualityTrendPointSerializer(data, many=True).data})
