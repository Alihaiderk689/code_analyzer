from rest_framework.pagination import PageNumberPagination


class AnalysisPagination(PageNumberPagination):
    # Mirrors github_integration.pr_views.PullRequestAnalysisPagination - same
    # page_size/params, for the same reason: a user's analysis history grows
    # unboundedly over time (every paste/upload/reanalyze creates a row), so
    # returning the full queryset in one response doesn't stay cheap forever.
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100
