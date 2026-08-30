import logging

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import Analysis

from .models import GitHubIntegration, GitHubRepository, RepositoryContextCheck, RepositoryFileCheck, RepositoryIndex
from .serializers import GitHubRepositorySerializer, RepositorySelectSerializer
from .services.context_check_rate_limit import get_context_check_status
from core.execution_budget import REASON_REQUEST_BUDGET_EXHAUSTED, BudgetExceeded

from .services.fetch_budget import TRUNCATED_BUDGET_EXHAUSTED, FetchBudgetExceeded
from .services.file_check_rate_limit import get_file_check_status
from .services.github_client import GitHubAPIError, GitHubAuthError, GitHubClient, GitHubFileTooLargeError, GitHubRateLimitError
from .services.pr_analysis_service import FileSkipReason, PRAnalysisService, classify_path
from .services.repository_service import RepositoryAccessDeniedError, RepositoryService
from .tasks import build_repository_index

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
        except RepositoryAccessDeniedError:
            return Response(
                {'detail': 'That repository is not accessible with your connected GitHub account.'},
                status=status.HTTP_403_FORBIDDEN,
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

        return Response({
            'repository': repository.full_name, 'default_branch': repository.default_branch,
            'results': tree['entries'], 'truncated': tree['truncated'],
        })


class RepositoryFileContentView(APIView):
    """GET /api/github/repositories/<pk>/file/?path=... - just the raw source
    of a file at the HEAD of the repo's default branch, free like the tree
    browse above (no quota, no quality/security/AI pipeline). Lets the file
    browser show code the moment a file is clicked; analyzing it is a
    separate, explicit action (RepositoryFileAnalyzeView) the user opts into."""

    def get(self, request, pk):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repository = get_object_or_404(GitHubRepository, pk=pk, integration=integration)

        path = (request.query_params.get('path') or '').strip()
        if not path:
            return Response({'detail': 'path is required.'}, status=status.HTTP_400_BAD_REQUEST)

        language, skip_reason = classify_path(path)
        if skip_reason:
            return Response({'path': path, 'language': language, 'skipped': True, 'skip_reason': skip_reason, 'content': None})

        owner, _, repo = repository.full_name.partition('/')
        try:
            content = GitHubClient(integration.get_access_token()).get_file_content(
                owner, repo, path, repository.default_branch, max_size_bytes=settings.GITHUB_MAX_FILE_SIZE_BYTES,
            )
        except GitHubFileTooLargeError:
            return Response({
                'path': path, 'language': language, 'skipped': True,
                'skip_reason': FileSkipReason.TOO_LARGE, 'content': None,
            })
        except GitHubAPIError as exc:
            return _handle_github_error(exc, integration)

        return Response({'path': path, 'language': language, 'skipped': False, 'skip_reason': None, 'content': content})


class RepositoryIndexStatusView(APIView):
    """GET /api/github/repositories/<pk>/index/ - status of the dependency-
    graph build (see repo_index_service.py) so the frontend can show
    "Understanding repository..." while it's running, without spending any
    quota or triggering GitHub calls itself."""

    def get(self, request, pk):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repository = get_object_or_404(GitHubRepository, pk=pk, integration=integration)

        index = RepositoryIndex.objects.filter(repository=repository).first()
        if index is None:
            return Response({'status': 'not_started'})
        return Response({
            'status': index.status,
            'files_total': index.files_total,
            'files_indexed': index.files_indexed,
            'truncated': index.truncated,
            'error': index.error or None,
            'indexed_at': index.indexed_at,
        })


class RepositoryReindexView(APIView):
    """POST /api/github/repositories/<pk>/reindex/ - manually rebuilds the
    dependency graph. There's no push webhook to invalidate it automatically
    (only pull_request events are subscribed to - see
    GitHubClient.create_webhook), so this is the way to refresh it after
    pushing new commits."""

    def post(self, request, pk):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repository = get_object_or_404(GitHubRepository, pk=pk, integration=integration)
        build_repository_index.delay(repository.id)
        return Response(status=status.HTTP_202_ACCEPTED)


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


class ContextCheckQuotaView(APIView):
    """GET /api/github/context-checks/quota/ - the ContextCheckQuotaView
    counterpart of FileCheckQuotaView above, for the separate 'analyze with
    repo context' quota (see context_check_rate_limit.py)."""

    def get(self, request):
        tz_offset_minutes = request.query_params.get('tz_offset_minutes', 0)
        limit_status = get_context_check_status(request.user, tz_offset_minutes)
        today_check = limit_status.pop('today_check')
        return Response({**limit_status, 'today_check': _serialize_context_check(today_check) if today_check else None})


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


class RepositoryFileContextAnalyzeView(APIView):
    """POST /api/github/repositories/<pk>/analyze-file-context/ - body {path}.
    Like RepositoryFileAnalyzeView, but also fetches and analyzes the file's
    direct dependency-graph neighbors (see PRAnalysisService
    .analyze_file_with_context), so the result shows a change's impact on
    related files instead of just the one file in isolation. Costs several
    GitHub content fetches and up to one Groq call per related file - gated
    to once per user per day under its own quota (see
    context_check_rate_limit.py), separate from the plain single-file check's
    quota above. Re-requesting the *same* path you already checked today is
    free, exactly like RepositoryFileAnalyzeView."""

    def post(self, request, pk):
        integration, error = _get_integration_or_error(request)
        if error:
            return error
        repository = get_object_or_404(GitHubRepository, pk=pk, integration=integration)

        path = (request.data.get('path') or '').strip()
        if not path:
            return Response({'detail': 'path is required.'}, status=status.HTTP_400_BAD_REQUEST)

        language, skip_reason = classify_path(path)
        if skip_reason:
            return Response({
                'path': path, 'language': language, 'skipped': True,
                'skip_reason': skip_reason, 'issues': [], 'score': None, 'related': [],
                'context_truncated': False, 'context_truncated_reason': '',
                'degraded_stages': [], 'cached': False,
            })

        tz_offset_minutes = request.data.get('tz_offset_minutes', 0)
        limit_status = get_context_check_status(request.user, tz_offset_minutes)
        today_check = limit_status.pop('today_check')

        if today_check is not None:
            if today_check.repository_id == repository.id and today_check.path == path:
                return Response({**_serialize_context_check(today_check), 'cached': True})
            return Response(
                {'detail': "You've used today's context check. Try again after the reset.", **limit_status},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        try:
            result = PRAnalysisService().analyze_file_with_context(repository, path, integration.get_access_token())
        except GitHubAPIError as exc:
            return _handle_github_error(exc, integration)
        except BudgetExceeded as exc:
            # Only reachable if a budget ran out on the *primary* file, i.e.
            # before any context existed to keep - there is no partial result
            # worth persisting, and the quota stays unspent. Kept separate
            # from _handle_github_error: GitHub was fine and no AI provider
            # failed, we ran out of time. Which budget is reported back so an
            # operator can tell "GitHub was slow" from "analysis was slow".
            reason = (
                TRUNCATED_BUDGET_EXHAUSTED if isinstance(exc, FetchBudgetExceeded)
                else REASON_REQUEST_BUDGET_EXHAUSTED
            )
            logger.warning(
                'github_context_check.budget_exhausted_on_primary',
                extra={'path': path, 'reason': reason},
            )
            return Response(
                {
                    'detail': 'Analyzing this file took too long. Please try again shortly.',
                    'context_truncated_reason': reason,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if result['skipped']:
            # Doesn't count against the daily quota - a misclick on a binary/
            # lock/generated file shouldn't burn the user's one check for the day.
            return Response({**result, 'cached': False})

        analysis = _create_analysis_for_file_check(request.user, repository, path, result)
        check = RepositoryContextCheck.objects.create(
            user=request.user, repository=repository, path=path,
            language=result['language'] or '', issues=result['issues'], score=result['score'],
            related=result['related'], analysis=analysis,
            context_truncated_reason=result.get('context_truncated_reason', ''),
            degraded_stages=result.get('degraded_stages', []),
        )
        return Response({**_serialize_context_check(check), 'cached': False}, status=status.HTTP_201_CREATED)


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
        # '' when there's no repo index yet (or this file wasn't indexed) -
        # every AI prompt about this Analysis (suggestions/explanation/
        # refactor/chat) includes it when present, see ai_views.py/ai/prompts.py.
        repo_context=result.get('repo_context', ''),
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


def _serialize_context_check(check: RepositoryContextCheck) -> dict:
    return {
        'path': check.path,
        'language': check.language or None,
        'skipped': False,
        'skip_reason': None,
        'issues': check.issues,
        'score': check.score,
        'related': check.related,
        # Partial-context marker - see RepositoryContextCheck
        # .context_truncated_reason. Carried on the cached response too, so a
        # truncated result never silently reads as complete.
        'context_truncated': bool(check.context_truncated_reason),
        'context_truncated_reason': check.context_truncated_reason,
        'degraded_stages': check.degraded_stages,
        'analysis_id': check.analysis_id,
        'content': check.analysis.source_code if check.analysis_id else None,
        'repository_id': check.repository_id,
        'created_at': check.created_at,
    }
