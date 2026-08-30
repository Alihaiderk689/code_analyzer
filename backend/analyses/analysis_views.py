import logging

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from core.throttling import AnalysisCreateRateThrottle

from .engine import analyze_code, detect_language, detect_language_from_code
from .models import Analysis
from .pagination import AnalysisPagination
from .serializers import (
    AnalysisDetailSerializer,
    AnalysisSerializer,
    AnalysisStatusSerializer,
    AnalyzeRequestSerializer,
    UploadRequestSerializer,
)

logger = logging.getLogger(__name__)


def _run_analysis(analysis):
    """Runs the (possibly sandboxed) analysis engine and marks the row COMPLETED.
    On any unexpected failure (e.g. the sandboxed subprocess itself misbehaving in
    a way engine.py/sandbox.py didn't already anticipate), the row is marked
    FAILED instead of being left stuck in PENDING/RUNNING forever, and the error
    is logged rather than propagating into a raw 500."""
    try:
        result = analyze_code(analysis.source_code, analysis.language)
    except Exception:
        logger.exception('analysis_run_failed', extra={'analysis_id': analysis.pk})
        analysis.status = Analysis.Status.FAILED
        analysis.save(update_fields=['status', 'updated_at'])
        return analysis

    analysis.lines_of_code = result['lines_of_code']
    analysis.issues = result['issues']
    analysis.issues_count = result['issues_count']
    analysis.quality_score = result['quality_score']
    analysis.status = Analysis.Status.COMPLETED
    analysis.save()
    return analysis


class AnalyzeView(APIView):
    throttle_classes = [AnalysisCreateRateThrottle]

    def post(self, request):
        serializer = AnalyzeRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        analysis = Analysis.objects.create(
            owner=request.user,
            name=data.get('name') or f'snippet-{timezone.now():%Y%m%d%H%M%S}',
            language=detect_language_from_code(data['code']),
            source_code=data['code'],
            status=Analysis.Status.PENDING,
        )
        _run_analysis(analysis)
        return Response(AnalysisDetailSerializer(analysis).data, status=status.HTTP_201_CREATED)


class UploadView(APIView):
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [AnalysisCreateRateThrottle]

    def post(self, request):
        serializer = UploadRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        uploaded_file = serializer.validated_data['file']
        name = serializer.validated_data.get('name') or uploaded_file.name
        language = detect_language(uploaded_file.name)

        try:
            code = uploaded_file.read().decode('utf-8')
        except UnicodeDecodeError:
            # Previously this created a FAILED Analysis row and returned 201,
            # so the client saw "created" for something that never analysed and
            # carried no reason why (Analysis has no error field). A rejected
            # upload is a client error: say so, and don't persist a dead row.
            return Response(
                {'file': 'File must be UTF-8 encoded text. Binary or differently-encoded files cannot be analysed.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Matches AnalyzeRequestSerializer.validate_code, which already rejects
        # whitespace-only pasted input. Without this an all-whitespace upload
        # analyses to lines_of_code=0 and scores 0.0 - indistinguishable from
        # genuinely terrible code, when it actually means "no code at all".
        if not code.strip():
            return Response(
                {'file': 'File is empty or contains only whitespace.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        analysis = Analysis.objects.create(
            owner=request.user, name=name, language=language, source_code=code,
            status=Analysis.Status.PENDING,
        )
        _run_analysis(analysis)
        return Response(AnalysisDetailSerializer(analysis).data, status=status.HTTP_201_CREATED)


class AnalysisListView(APIView):
    pagination_class = AnalysisPagination

    def get(self, request):
        queryset = Analysis.objects.filter(owner=request.user)
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(queryset, request)
        serializer = AnalysisSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AnalysisDetailView(APIView):
    def get(self, request, pk):
        analysis = get_object_or_404(Analysis, pk=pk, owner=request.user)
        return Response(AnalysisDetailSerializer(analysis).data)

    def delete(self, request, pk):
        analysis = get_object_or_404(Analysis, pk=pk, owner=request.user)
        analysis.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ReanalyzeView(APIView):
    throttle_classes = [AnalysisCreateRateThrottle]

    def post(self, request, pk):
        analysis = get_object_or_404(Analysis, pk=pk, owner=request.user)
        if not analysis.source_code:
            return Response(
                {'detail': 'No source code stored for this analysis; the original upload could not be decoded.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        analysis.status = Analysis.Status.PENDING
        analysis.save(update_fields=['status', 'updated_at'])
        _run_analysis(analysis)
        return Response(AnalysisDetailSerializer(analysis).data)


class CancelView(APIView):
    def post(self, request, pk):
        analysis = get_object_or_404(Analysis, pk=pk, owner=request.user)
        if analysis.status not in (Analysis.Status.PENDING, Analysis.Status.RUNNING):
            return Response(
                {'detail': f'Cannot cancel an analysis with status "{analysis.status}".'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        analysis.status = Analysis.Status.CANCELLED
        analysis.save(update_fields=['status', 'updated_at'])
        return Response(AnalysisDetailSerializer(analysis).data)


class StatusView(APIView):
    def get(self, request, pk):
        analysis = get_object_or_404(Analysis, pk=pk, owner=request.user)
        return Response(AnalysisStatusSerializer(analysis).data)


class ClearHistoryView(APIView):
    def delete(self, request):
        deleted_count, _ = Analysis.objects.filter(owner=request.user).delete()
        return Response({'detail': f'Deleted {deleted_count} analysis record(s).', 'deleted_count': deleted_count})
