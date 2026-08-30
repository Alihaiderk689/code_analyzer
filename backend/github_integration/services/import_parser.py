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
_PY_FROM_IMPORT_RE = re.compile(r'^\s*from\s+([\w.]+)\s+import\s+(.+)$', re.MULTILINE)

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
    needs the full file list (see resolve_import).

    One Python form needs expanding at extraction time rather than left as
    written: `from . import serializers` (or `from .. import x, y`) has no
    dotted path of its own to resolve - the module part is *only* dots, and
    the actual submodule names being imported are the names after `import`.
    That's the idiomatic use of this form (unlike `from .serializers import
    UserSerializer`, where the names after `import` are usually classes/
    functions, not files - deliberately not expanded, same as the absolute
    `from myapp.utils import helper` case never was). So each name becomes
    its own '<dots><name>' specifier, e.g. '.serializers', resolved exactly
    like a normal `from .serializers import ...`."""
    if language == 'Python':
        raw: list[str] = list(_PY_IMPORT_RE.findall(content))
        for module, names in _PY_FROM_IMPORT_RE.findall(content):
            if module.strip('.') == '':
                raw.extend(f'{module}{name}' for name in _split_relative_import_names(names))
            else:
                raw.append(module)
        return raw
    if language in ('JavaScript', 'TypeScript'):
        return [
            *_JS_FROM_RE.findall(content),
            *_JS_REQUIRE_RE.findall(content),
        ]
    return []


def _split_relative_import_names(names_blob: str) -> list[str]:
    """Best-effort split of the name list in `from <dots> import <names>` -
    handles comma-separated names, a wrapping `(...)`, an ` as alias` (the
    alias is irrelevant to resolution, only the real submodule name is), and
    a trailing inline comment. Doesn't handle a multi-line parenthesized list
    (`from . import (\\n    x,\\n)`) - this parser works one line at a time
    everywhere else too, so that's an existing, accepted limitation rather
    than a new one."""
    names_blob = names_blob.split('#', 1)[0].strip().strip('()')
    names = []
    for entry in names_blob.split(','):
        entry = entry.strip()
        if not entry or entry == '*':
            continue
        names.append(entry.split()[0])  # 'x as y' -> 'x'
    return names


def resolve_import(raw_import: str, importing_path: str, all_paths: set[str], language: str) -> str | None:
    """Maps a raw import specifier to an actual path in `all_paths`, or None
    if it doesn't resolve to a file in this repo (e.g. a third-party package
    like `requests` or `react`) - only intra-repo edges are worth storing."""
    if language == 'Python':
        return _resolve_python_import(raw_import, importing_path, all_paths)
    if language in ('JavaScript', 'TypeScript'):
        return _resolve_js_import(raw_import, importing_path, all_paths)
    return None


def _resolve_python_import(raw_import: str, importing_path: str, all_paths: set[str]) -> str | None:
    """Two cases, handled differently because one has a knowable anchor and
    the other doesn't:

    - Absolute (`myapp.utils`, no leading dot): the package root varies per
      repo (src/, backend/, the repo root itself, ...) and isn't knowable
      without a project config this parser doesn't read - so instead of
      guessing a root, match the dotted path as a path *suffix* against every
      file in the repo, and take the shortest match (same "shortest match
      wins" heuristic _find_settings_source already uses for exactly this
      kind of ambiguity).
    - Relative (`.serializers`, `..accounts.models`, ...): Python defines
      exactly what these mean relative to the importing file, so there's no
      guessing to do - one leading dot means "the package containing
      importing_path" (i.e. its own directory), each additional dot walks up
      one more directory level, and the remainder after the dots is a
      dotted path from there. Resolved as an exact match, not a suffix
      match, since the anchor removes the ambiguity the absolute case has.
    """
    dot_count = len(raw_import) - len(raw_import.lstrip('.'))
    name_part = raw_import[dot_count:]

    if dot_count == 0:
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

    if not name_part:
        # A bare run of dots with nothing after it never reaches here in
        # practice - extract_imports() expands `from . import x` into one
        # '.x' specifier per imported name (see its docstring) before this
        # function ever sees it. Nothing to resolve to a single file here.
        return None

    base_dir = os.path.dirname(importing_path)
    for _ in range(dot_count - 1):
        base_dir = os.path.dirname(base_dir)

    module_path = name_part.replace('.', '/')
    base = f'{base_dir}/{module_path}' if base_dir else module_path
    for candidate in (f'{base}.py', f'{base}/__init__.py'):
        if candidate in all_paths:
            return candidate
    return None


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
