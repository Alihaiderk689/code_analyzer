from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Analysis
from .serializers import AnalysisSerializer


class SearchView(APIView):
    def get(self, request):
        query = request.query_params.get('q', '').strip()
        if not query:
            return Response({'detail': 'Query parameter "q" is required.'}, status=status.HTTP_400_BAD_REQUEST)

        queryset = Analysis.objects.filter(owner=request.user).filter(
            Q(name__icontains=query) | Q(language__icontains=query) | Q(source_code__icontains=query)
        )

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        language_filter = request.query_params.get('language')
        if language_filter:
            queryset = queryset.filter(language__iexact=language_filter)

        serializer = AnalysisSerializer(queryset, many=True)
        return Response({'query': query, 'count': queryset.count(), 'results': serializer.data})
