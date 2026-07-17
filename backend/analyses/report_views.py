import io
import json

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from xhtml2pdf import pisa

from .models import Analysis


def _get_owned_analysis(request, analysis_id):
    return get_object_or_404(Analysis, pk=analysis_id, owner=request.user)


def _render_report_html(analysis):
    return render_to_string('analyses/report.html', {'analysis': analysis, 'generated_at': timezone.now()})


class JSONReportView(APIView):
    def get(self, request, analysis_id):
        analysis = _get_owned_analysis(request, analysis_id)
        data = {
            'id': analysis.id,
            'name': analysis.name,
            'language': analysis.language,
            'status': analysis.status,
            'quality_score': analysis.quality_score,
            'issues_count': analysis.issues_count,
            'lines_of_code': analysis.lines_of_code,
            'issues': analysis.issues,
            'ai_explanation': analysis.ai_explanation or None,
            'ai_suggestions': analysis.ai_suggestions or None,
            'ai_refactored_code': analysis.ai_refactored_code or None,
            'created_at': analysis.created_at.isoformat(),
            'updated_at': analysis.updated_at.isoformat(),
        }
        response = HttpResponse(json.dumps(data, indent=2), content_type='application/json')
        response['Content-Disposition'] = f'attachment; filename="analysis-{analysis.id}-report.json"'
        return response


class HTMLReportView(APIView):
    def get(self, request, analysis_id):
        analysis = _get_owned_analysis(request, analysis_id)
        return HttpResponse(_render_report_html(analysis), content_type='text/html')


class PDFReportView(APIView):
    def get(self, request, analysis_id):
        analysis = _get_owned_analysis(request, analysis_id)
        html = _render_report_html(analysis)
        buffer = io.BytesIO()
        result = pisa.CreatePDF(html, dest=buffer)
        if result.err:
            return Response({'detail': 'Failed to generate PDF report.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        response = HttpResponse(buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="analysis-{analysis.id}-report.pdf"'
        return response
