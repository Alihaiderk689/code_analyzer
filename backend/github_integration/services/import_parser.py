"""Best-effort, regex-based import extraction and resolution - deliberately
not a real per-language AST/import-resolver (there's no single library that
covers Python + every JS/TS module system + tsconfig path aliases + etc.,
and getting that exactly right isn't worth the cost for what this only ever
feeds: extra *context* for an AI prompt, not a build system). Same tradeoff
this project already makes elsewhere for cross-file signal - see
pr_analysis_service._find_settings_source (heuristic, silently degrades).

Only Python and JS/TS/JSX/TSX are supported - the two ecosystems
SUPPORTED_EXTENSIONS in pr_analysis_service.py already targets most heavily,
and where "what imports what" is simple enough to get right with regexes.
Other supported-for-analysis languages (Java, C#, C++, Go, Rust, PHP) are
still analyzed on their own, they just never get repo_context.
"""
from __future__ import annotations

import os
import re

_PY_IMPORT_RE = re.compile(r'^\s*import\s+([\w.]+)', re.MULTILINE)
_PY_FROM_IMPORT_RE = re.compile(r'^\s*from\s+([\w.]+)\s+import\b', re.MULTILINE)

# Matches `import ... from 'x'`, `export ... from 'x'`, and `require('x')` -
# covers ESM and CommonJS, the two module systems actually in use across a
# typical JS/TS repo.
_JS_FROM_RE = re.compile(r'''\bfrom\s+['"]([^'"]+)['"]''')
_JS_REQUIRE_RE = re.compile(r'''\brequire\(\s*['"]([^'"]+)['"]\s*\)''')

_JS_RESOLUTION_SUFFIXES = (
    '', '.js', '.jsx', '.ts', '.tsx',
    '/index.js', '/index.jsx', '/index.ts', '/index.tsx',
)


def extract_imports(content: str, language: str) -> list[str]:
    """Returns raw import specifiers (a Python dotted path, or a JS/TS module
    string) exactly as written - not yet resolved to a repo path, since that
    needs the full file list (see resolve_import)."""
    if language == 'Python':
        return [
            *_PY_IMPORT_RE.findall(content),
            *_PY_FROM_IMPORT_RE.findall(content),
        ]
    if language in ('JavaScript', 'TypeScript'):
        return [
            *_JS_FROM_RE.findall(content),
            *_JS_REQUIRE_RE.findall(content),
        ]
    return []


def resolve_import(raw_import: str, importing_path: str, all_paths: set[str], language: str) -> str | None:
    """Maps a raw import specifier to an actual path in `all_paths`, or None
    if it doesn't resolve to a file in this repo (e.g. a third-party package
    like `requests` or `react`) - only intra-repo edges are worth storing."""
    if language == 'Python':
        return _resolve_python_import(raw_import, all_paths)
    if language in ('JavaScript', 'TypeScript'):
        return _resolve_js_import(raw_import, importing_path, all_paths)
    return None


def _resolve_python_import(raw_import: str, all_paths: set[str]) -> str | None:
    # Absolute imports in a real codebase are relative to *some* package root,
    # which varies per repo (src/, backend/, the repo root itself, ...) and
    # isn't knowable without a project config this parser doesn't read - so
    # instead of guessing a root, match the dotted path as a path *suffix*
    # against every file in the repo, and take the shortest match (same
    # "shortest match wins" heuristic _find_settings_source already uses for
    # exactly this kind of ambiguity).
    module_path = raw_import.replace('.', '/')
    candidates = [
        f'{module_path}.py',
        f'{module_path}/__init__.py',
    ]
    matches = sorted(
        (p for p in all_paths if any(p == c or p.endswith(f'/{c}') for c in candidates)),
        key=len,
    )
    return matches[0] if matches else None


def _resolve_js_import(raw_import: str, importing_path: str, all_paths: set[str]) -> str | None:
    # Bare specifiers ('react', '@scope/pkg') are npm packages, not repo
    # files - only './x' / '../x' relative imports can possibly be this repo's
    # own files, so anything else is skipped rather than guessed at.
    if not raw_import.startswith('.'):
        return None

    base_dir = os.path.dirname(importing_path)
    joined = os.path.normpath(os.path.join(base_dir, raw_import)).replace(os.sep, '/')

    for suffix in _JS_RESOLUTION_SUFFIXES:
        candidate = f'{joined}{suffix}'
        if candidate in all_paths:
            return candidate
    return None
