from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Avg, Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import Analysis

from .serializers import AdminAnalysisSerializer, AdminUserSerializer

User = get_user_model()


class AdminUserListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        queryset = (
            User.objects.select_related('profile')
            .annotate(analyses_count=Count('analyses'))
            .order_by('-date_joined')
        )

        query = request.query_params.get('q', '').strip()
        if query:
            queryset = queryset.filter(Q(username__icontains=query) | Q(email__icontains=query))

        is_active = request.query_params.get('is_active')
        if is_active is not None:
            queryset = queryset.filter(is_active=is_active.lower() == 'true')

        serializer = AdminUserSerializer(queryset, many=True)
        return Response({'count': queryset.count(), 'results': serializer.data})


class AdminUserDeleteView(APIView):
    permission_classes = [IsAdminUser]

    def delete(self, request, pk):
        if pk == request.user.id:
            return Response(
                {'detail': 'You cannot delete your own account via this endpoint.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = get_object_or_404(User, pk=pk)
        user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminAnalysisListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        queryset = Analysis.objects.select_related('owner').all()

        status_filter = request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        language_filter = request.query_params.get('language')
        if language_filter:
            queryset = queryset.filter(language__iexact=language_filter)

        owner_id = request.query_params.get('owner_id')
        if owner_id:
            queryset = queryset.filter(owner_id=owner_id)

        serializer = AdminAnalysisSerializer(queryset, many=True)
        return Response({'count': queryset.count(), 'results': serializer.data})


class AdminStatsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        now = timezone.now()
        users_qs = User.objects.all()
        analyses_qs = Analysis.objects.all()

        totals = analyses_qs.aggregate(
            total_lines_of_code=Sum('lines_of_code'),
            total_issues=Sum('issues_count'),
            average_quality_score=Avg('quality_score', filter=Q(status=Analysis.Status.COMPLETED)),
        )

        return Response({
            'users': {
                'total': users_qs.count(),
                'active': users_qs.filter(is_active=True).count(),
                'staff': users_qs.filter(is_staff=True).count(),
                'verified': users_qs.filter(profile__is_verified=True).count(),
                'joined_last_7_days': users_qs.filter(date_joined__gte=now - timedelta(days=7)).count(),
                'joined_last_30_days': users_qs.filter(date_joined__gte=now - timedelta(days=30)).count(),
            },
            'analyses': {
                'total': analyses_qs.count(),
                'pending': analyses_qs.filter(status=Analysis.Status.PENDING).count(),
                'running': analyses_qs.filter(status=Analysis.Status.RUNNING).count(),
                'completed': analyses_qs.filter(status=Analysis.Status.COMPLETED).count(),
                'failed': analyses_qs.filter(status=Analysis.Status.FAILED).count(),
                'cancelled': analyses_qs.filter(status=Analysis.Status.CANCELLED).count(),
                'total_lines_of_code': totals['total_lines_of_code'] or 0,
                'total_issues': totals['total_issues'] or 0,
                'average_quality_score': totals['average_quality_score'],
            },
        })
