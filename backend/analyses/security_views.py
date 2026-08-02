import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.throttling import AnalysisCreateRateThrottle

from .models import Analysis
from .serializers import SecurityReportSerializer
from .services.security_service import SecurityAnalysisService

logger = logging.getLogger(__name__)


class SecurityAnalysisView(APIView):
    """Security Analysis Mode - deliberately separate from the regular code
    review (AnalyzeView/ai_views.py): a different pipeline (Bandit + custom
    rules, not pyflakes/ast), a different score, and its own cache field
    (Analysis.security_report) rather than being mixed into `issues`.

    GET  /api/analysis/<id>/security/  - returns the cached report, if any.
    POST /api/analysis/<id>/security/  - runs the scan (or re-runs it with
                                          ?regenerate=true) and caches the result.
    """

    throttle_classes = [AnalysisCreateRateThrottle]

    def _get_owned_completed_analysis(self, request, pk):
        analysis = get_object_or_404(Analysis, pk=pk, owner=request.user)
        if analysis.status != Analysis.Status.COMPLETED:
            return None, Response(
                {'detail': f'Analysis must be completed before running a security scan (current status: "{analysis.status}").'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return analysis, None

    def get(self, request, pk):
        analysis, error = self._get_owned_completed_analysis(request, pk)
        if error:
            return error

        if not analysis.security_report:
            return Response(
                {'detail': 'Security analysis has not been run for this analysis yet.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = SecurityReportSerializer(analysis.security_report)
        return Response({**serializer.data, 'cached': True})

    def post(self, request, pk):
        analysis, error = self._get_owned_completed_analysis(request, pk)
        if error:
            return error

        regenerate = request.query_params.get('regenerate', '').lower() == 'true'
        if analysis.security_report and not regenerate:
            serializer = SecurityReportSerializer(analysis.security_report)
            return Response({**serializer.data, 'cached': True})

        try:
            report = SecurityAnalysisService().analyze(
                source_code=analysis.source_code,
                language=analysis.language,
                filename=analysis.name,
            )
        except Exception:
            logger.exception('Security analysis failed for analysis #%s.', analysis.pk)
            return Response(
                {'detail': 'Security analysis is currently unavailable.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        analysis.security_report = report
        analysis.save(update_fields=['security_report', 'updated_at'])

        serializer = SecurityReportSerializer(report)
        return Response({**serializer.data, 'cached': False}, status=status.HTTP_201_CREATED)
