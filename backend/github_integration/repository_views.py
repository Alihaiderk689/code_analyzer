import logging

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import Analysis

from .models import GitHubIntegration, GitHubRepository, RepositoryFileCheck
from .serializers import GitHubRepositorySerializer, RepositorySelectSerializer
from .services.file_check_rate_limit import get_file_check_status
from .services.github_client import GitHubAPIError, GitHubAuthError, GitHubClient, GitHubRateLimitError
from .services.pr_analysis_service import PRAnalysisService, classify_path
from .services.repository_service import RepositoryService

logger = logging.getLogger(__name__)


def _get_integration_or_error(request):
    try:
        return request.user.github_integration, None
    except GitHubIntegration.DoesNotExist:
        return None, Response(
            {'detail': 'Connect your GitHub account first.'}, status=status.HTTP_400_BAD_REQUEST,
        )


def _handle_github_error(exc: GitHubAPIError, integration: GitHubIntegration) -> Response:
    if isinstance(exc, GitHubAuthError):
        integration.token_invalid = True
        integration.save(update_fields=['token_invalid'])
        return Response(
            {'detail': 'Your GitHub connection has expired or was revoked. Please reconnect.'},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if isinstance(exc, GitHubRateLimitError):
        return Response(
            {'detail': 'GitHub API rate limit exceeded. Please try again shortly.', 'reset_at': exc.reset_at},
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    logger.error('github_api.request_failed', exc_info=True, extra={'status_code': exc.status_code})
    return Response({'detail': 'Could not reach GitHub. Please try again shortly.'}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class RepositoryListView(APIView):
    """GET /api/github/repositories/ - a live list of every repo the user has
    access to on GitHub (not just monitored ones), each flagged with whether
    it's currently monitored, so the frontend can render a single toggle list."""

    def get(self, request):
        integration, error = _get_integration_or_error(request)
        if error:
            return error

        try:
            repos = RepositoryService().list_available_repositories(integration)
        except GitHubAPIError as exc:
            return _handle_github_error(exc, integration)

        monitored_ids = set(
            integration.repositories.filter(is_active=True).values_list('repository_id', flat=True)
        )
        results = [
            {
                'repository_id': repo['id'],
                'full_name': repo['full_name'],
                'private': repo.get('private', False),
                'default_branch': repo.get('default_branch', 'main'),
                'is_monitored': repo['id'] in monitored_ids,
            }
            for repo in repos
        ]
        return Response({'count': len(results), 'results': results})


class RepositorySelectView(APIView):
    """POST /api/github/repositories/select/ - starts monitoring a repository:
    creates a real webhook on it via the GitHub API and stores the webhook id."""

    def post(self, request):
        integration, error = _get_integration_or_error(request)
        if error:
            return error

        serializer = RepositorySelectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            repository = RepositoryService().select_repository(
                integration,
                serializer.validated_data['repository_id'],
                serializer.validated_data['repository_name'],
            )
        except GitHubAPIError as exc:
            return _handle_github_error(exc, integration)

        return Response(GitHubRepositorySerializer(repository).data, status=status.HTTP_201_CREATED)


class MonitoredRepositoryListView(APIView):
    """GET /api/github/repositories/monitored/ - the "Connected Repositories"
    dashboard section: DB-only, no GitHub API call, just what's actually
    being tracked. Distinct from RepositoryListView, which reflects the
    user's full GitHub access and calls out to the GitHub API."""

    def get(self, request):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repositories = integration.repositories.filter(is_active=True)
        return Response(GitHubRepositorySerializer(repositories, many=True).data)


class RepositoryDeselectView(APIView):
    """DELETE /api/github/repositories/<pk>/ - stops monitoring a repository
    and removes its webhook from GitHub. Not in the original spec, but an
    unavoidable counterpart to "select": a live webhook gets created on the
    user's repo, so there must be a way to remove it again."""

    def delete(self, request, pk):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repository = get_object_or_404(GitHubRepository, pk=pk, integration=integration)
        RepositoryService().deselect_repository(integration, repository)
        return Response(status=status.HTTP_204_NO_CONTENT)


class RepositoryTreeView(APIView):
    """GET /api/github/repositories/<pk>/tree/ - the full file tree of a
    monitored repository, browsable for free (one GitHub API call total,
    regardless of repo size - see GitHubClient.get_repository_tree). Browsing
    never touches the daily file-check quota; only analyzing a file does."""

    def get(self, request, pk):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repository = get_object_or_404(GitHubRepository, pk=pk, integration=integration)

        owner, _, repo = repository.full_name.partition('/')
        try:
            tree = GitHubClient(integration.get_access_token()).get_repository_tree(owner, repo, repository.default_branch)
        except GitHubAPIError as exc:
            return _handle_github_error(exc, integration)

        return Response({'repository': repository.full_name, 'default_branch': repository.default_branch, 'results': tree})


class FileCheckQuotaView(APIView):
    """GET /api/github/file-checks/quota/ - lets the frontend show "X/1 used
    today, resets in..." without spending the quota just to find that out.
    Global per user, not per-repository - matches "one repo monitored at a
    time" (see RepositoryService.select_repository)."""

    def get(self, request):
        tz_offset_minutes = request.query_params.get('tz_offset_minutes', 0)
        limit_status = get_file_check_status(request.user, tz_offset_minutes)
        today_check = limit_status.pop('today_check')
        return Response({**limit_status, 'today_check': _serialize_file_check(today_check) if today_check else None})


class RepositoryFileAnalyzeView(APIView):
    """POST /api/github/repositories/<pk>/analyze-file/ - body {path}. Costs
    one GitHub content fetch and (if the file has a security finding) one Groq
    call - gated to once per user per day (see file_check_rate_limit.py).
    Re-requesting the *same* path you already checked today is free (returns
    the stored result, no new API calls); a *different* path is rejected with
    429 until the quota resets."""

    def post(self, request, pk):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repository = get_object_or_404(GitHubRepository, pk=pk, integration=integration)

        path = (request.data.get('path') or '').strip()
        if not path:
            return Response({'detail': 'path is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Classify by path before the quota gate below - skip-eligible files
        # (binary/lock/generated/unsupported-language) are always free, even
        # once today's one real check has already been used elsewhere.
        language, skip_reason = classify_path(path)
        if skip_reason:
            return Response({
                'path': path, 'language': language, 'skipped': True,
                'skip_reason': skip_reason, 'issues': [], 'score': None, 'cached': False,
            })

        tz_offset_minutes = request.data.get('tz_offset_minutes', 0)
        limit_status = get_file_check_status(request.user, tz_offset_minutes)
        today_check = limit_status.pop('today_check')

        if today_check is not None:
            if today_check.repository_id == repository.id and today_check.path == path:
                return Response({**_serialize_file_check(today_check), 'cached': True})
            return Response(
                {'detail': "You've used today's file check. Try again after the reset.", **limit_status},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            result = PRAnalysisService().analyze_file_by_path(repository, path, integration.get_access_token())
        except GitHubAPIError as exc:
            return _handle_github_error(exc, integration)

        if result['skipped']:
            # Doesn't count against the daily quota - a misclick on a binary/
            # lock/generated file shouldn't burn the user's one check for the day.
            return Response({**result, 'cached': False})

        analysis = _create_analysis_for_file_check(request.user, repository, path, result)
        check = RepositoryFileCheck.objects.create(
            user=request.user, repository=repository, path=path,
            language=result['language'] or '', issues=result['issues'], score=result['score'],
            analysis=analysis,
        )
        return Response({**_serialize_file_check(check), 'cached': False}, status=status.HTTP_201_CREATED)


def _create_analysis_for_file_check(user, repository: GitHubRepository, path: str, result: dict) -> Analysis:
    """Backs the "chat about this file" feature: a real Analysis row so the
    existing chat.Conversation/ChatMessage machinery - same UI component, same
    3-messages/day quota - works completely unmodified, exactly as it already
    does for pasted/uploaded code (see chat/views.py, ai/prompts.py)."""
    content = result['content']
    return Analysis.objects.create(
        owner=user,
        name=f'{repository.full_name}: {path}'[:255],
        language=result['language'] or '',
        source_code=content,
        lines_of_code=len(content.splitlines()),
        issues=result['issues'],
        issues_count=len(result['issues']),
        quality_score=result['score'],
        status=Analysis.Status.COMPLETED,
    )


def _serialize_file_check(check: RepositoryFileCheck) -> dict:
    return {
        'path': check.path,
        'language': check.language or None,
        'skipped': False,
        'skip_reason': None,
        'issues': check.issues,
        'score': check.score,
        'analysis_id': check.analysis_id,
        # So the frontend can show the actual file alongside its score/issues
        # without a second fetch - already sitting on the linked Analysis row
        # (see _create_analysis_for_file_check), None only for legacy checks
        # that predate that field.
        'content': check.analysis.source_code if check.analysis_id else None,
        # The daily quota is global per user, not per-repository (see
        # file_check_rate_limit.py), so the frontend needs this to tell
        # whether today's check belongs to whichever repo it has open right
        # now versus a different (possibly no-longer-monitored) one.
        'repository_id': check.repository_id,
        'created_at': check.created_at,
    }
