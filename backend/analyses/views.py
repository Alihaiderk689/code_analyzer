from django.db.models import Avg, Count, Max, Min, Sum
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Analysis
from .serializers import AnalysisSerializer


def _stats_for(queryset):
    completed = queryset.filter(status=Analysis.Status.COMPLETED)
    totals = queryset.aggregate(total_lines_of_code=Sum('lines_of_code'), total_issues=Sum('issues_count'))
    return {
        'total_analyses': queryset.count(),
        'pending': queryset.filter(status=Analysis.Status.PENDING).count(),
        'running': queryset.filter(status=Analysis.Status.RUNNING).count(),
        'completed': completed.count(),
        'failed': queryset.filter(status=Analysis.Status.FAILED).count(),
        'total_lines_of_code': totals['total_lines_of_code'] or 0,
        'total_issues': totals['total_issues'] or 0,
        'average_quality_score': completed.aggregate(avg=Avg('quality_score'))['avg'],
    }


def _language_breakdown_for(queryset):
    total = queryset.count()
    breakdown = (
        queryset.exclude(language='')
        .values('language')
        .annotate(analyses_count=Count('id'), lines_of_code=Sum('lines_of_code'))
        .order_by('-analyses_count')
    )
    return [
        {
            'language': row['language'],
            'analyses_count': row['analyses_count'],
            'lines_of_code': row['lines_of_code'] or 0,
            'percentage': round(row['analyses_count'] / total * 100, 1) if total else 0,
        }
        for row in breakdown
    ]


def _score_summary_for(queryset):
    scored = queryset.filter(status=Analysis.Status.COMPLETED, quality_score__isnull=False)
    aggregates = scored.aggregate(
        average=Avg('quality_score'), highest=Max('quality_score'), lowest=Min('quality_score'),
    )
    buckets = {'excellent': 0, 'good': 0, 'fair': 0, 'poor': 0}
    for score in scored.values_list('quality_score', flat=True):
        if score >= 90:
            buckets['excellent'] += 1
        elif score >= 70:
            buckets['good'] += 1
        elif score >= 50:
            buckets['fair'] += 1
        else:
            buckets['poor'] += 1
    return {
        'average_score': aggregates['average'],
        'highest_score': aggregates['highest'],
        'lowest_score': aggregates['lowest'],
        'scored_count': scored.count(),
        'distribution': buckets,
    }


class DashboardSummaryView(APIView):
    def get(self, request):
        queryset = Analysis.objects.filter(owner=request.user)
        return Response({
            'stats': _stats_for(queryset),
            'recent_analyses': AnalysisSerializer(queryset[:5], many=True).data,
            'top_languages': _language_breakdown_for(queryset)[:5],
            'scores': _score_summary_for(queryset),
        })


class DashboardStatsView(APIView):
    def get(self, request):
        queryset = Analysis.objects.filter(owner=request.user)
        return Response(_stats_for(queryset))


class RecentAnalysesView(APIView):
    def get(self, request):
        queryset = Analysis.objects.filter(owner=request.user)
        try:
            limit = int(request.query_params.get('limit', 10))
        except ValueError:
            limit = 10
        limit = max(1, min(limit, 50))
        serializer = AnalysisSerializer(queryset[:limit], many=True)
        return Response({'count': queryset.count(), 'results': serializer.data})


class LanguageUsageView(APIView):
    def get(self, request):
        queryset = Analysis.objects.filter(owner=request.user)
        return Response({'languages': _language_breakdown_for(queryset)})


class QualityScoresView(APIView):
    def get(self, request):
        queryset = Analysis.objects.filter(owner=request.user)
        return Response(_score_summary_for(queryset))
