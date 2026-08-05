"""Prompt-building helpers shared by every chat surface in the app - the
floating general-purpose assistant (ai/views.ChatView) and the analysis-scoped
'Chat with Your Code' panel (chat/views.py) - so the two never drift into
describing the same Analysis differently."""

BASE_CHAT_INSTRUCTION = (
    'You are a helpful AI assistant for a code analysis tool. Answer clearly and concisely, '
    'formatting code with markdown fences when useful.'
)


def build_analysis_context(analysis):
    """Renders an Analysis as text for inclusion in a chat system prompt. Issues
    are numbered to match what the user sees in the UI, so the model can reliably
    answer questions like "why is issue #3 a problem?"."""
    issues = analysis.issues or []
    if issues:
        issues_text = '\n'.join(
            f'{i}. [{issue.get("type")}] line {issue.get("line", "?")}: {issue.get("message")}'
            for i, issue in enumerate(issues, start=1)
        )
    else:
        issues_text = 'None.'

    # Only set for analyses backed by a monitored GitHub repository file (see
    # github_integration.repository_views._create_analysis_for_file_check) -
    # blank, and so a no-op here, for pasted/uploaded code.
    repo_context_block = f'\n\n{analysis.repo_context}' if analysis.repo_context else ''

    return (
        f'\n\nThe user is asking about this analysis:\n'
        f'Name: {analysis.name}\n'
        f'Language: {analysis.language}\n'
        f'Quality score: {analysis.quality_score}\n'
        f'Lines of code: {analysis.lines_of_code}\n'
        f'Issues found (numbered to match what the user sees in the UI):\n{issues_text}\n\n'
        f'Source code:\n{analysis.source_code}'
        f'{repo_context_block}'
    )
