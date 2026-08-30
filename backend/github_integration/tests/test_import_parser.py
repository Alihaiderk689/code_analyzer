from django.test import SimpleTestCase

from ..services.import_parser import extract_imports, resolve_import


class ExtractImportsPythonTests(SimpleTestCase):
    def test_plain_import(self):
        self.assertEqual(extract_imports('import os\n', 'Python'), ['os'])

    def test_dotted_import(self):
        self.assertEqual(extract_imports('import myapp.utils\n', 'Python'), ['myapp.utils'])

    def test_from_import(self):
        self.assertEqual(extract_imports('from myapp.utils import helper\n', 'Python'), ['myapp.utils'])

    def test_multiple_imports(self):
        content = 'import os\nfrom myapp.models import User\nimport sys\n'
        self.assertEqual(extract_imports(content, 'Python'), ['os', 'sys', 'myapp.models'])

    def test_indented_import_inside_function_still_matched(self):
        content = 'def f():\n    import json\n    return json\n'
        self.assertEqual(extract_imports(content, 'Python'), ['json'])

    def test_no_imports(self):
        self.assertEqual(extract_imports('x = 1\n', 'Python'), [])

    def test_single_dot_relative_from_import(self):
        self.assertEqual(extract_imports('from .serializers import UserSerializer\n', 'Python'), ['.serializers'])

    def test_double_dot_relative_from_import(self):
        self.assertEqual(extract_imports('from ..accounts.models import User\n', 'Python'), ['..accounts.models'])

    def test_bare_dot_import_expands_each_name(self):
        content = 'from . import serializers, models\n'
        self.assertEqual(extract_imports(content, 'Python'), ['.serializers', '.models'])

    def test_bare_double_dot_import_expands_name(self):
        self.assertEqual(extract_imports('from .. import accounts\n', 'Python'), ['..accounts'])

    def test_bare_dot_import_drops_alias(self):
        self.assertEqual(extract_imports('from . import serializers as s\n', 'Python'), ['.serializers'])

    def test_bare_dot_import_ignores_wildcard(self):
        self.assertEqual(extract_imports('from . import *\n', 'Python'), [])

    def test_bare_dot_import_with_parens(self):
        content = 'from . import (serializers, models)\n'
        self.assertEqual(extract_imports(content, 'Python'), ['.serializers', '.models'])


class ExtractImportsJSTests(SimpleTestCase):
    def test_esm_default_import(self):
        content = "import React from 'react'\n"
        self.assertEqual(extract_imports(content, 'JavaScript'), ['react'])

    def test_esm_named_relative_import(self):
        content = "import { helper } from './utils'\n"
        self.assertEqual(extract_imports(content, 'TypeScript'), ['./utils'])

    def test_commonjs_require(self):
        content = "const helper = require('../lib/helper')\n"
        self.assertEqual(extract_imports(content, 'JavaScript'), ['../lib/helper'])

    def test_export_from_reexport(self):
        content = "export { helper } from './utils'\n"
        self.assertEqual(extract_imports(content, 'JavaScript'), ['./utils'])

    def test_unsupported_language_returns_empty(self):
        self.assertEqual(extract_imports('import "fmt"\n', 'Go'), [])


class ResolvePythonImportTests(SimpleTestCase):
    def test_resolves_module_file(self):
        all_paths = {'myapp/utils.py', 'myapp/models.py'}
        self.assertEqual(resolve_import('myapp.utils', 'myapp/views.py', all_paths, 'Python'), 'myapp/utils.py')

    def test_resolves_package_init(self):
        all_paths = {'myapp/utils/__init__.py'}
        self.assertEqual(resolve_import('myapp.utils', 'myapp/views.py', all_paths, 'Python'), 'myapp/utils/__init__.py')

    def test_shortest_match_wins_for_ambiguous_suffix(self):
        all_paths = {'myapp/utils.py', 'vendor/myapp/utils.py'}
        self.assertEqual(resolve_import('myapp.utils', 'x.py', all_paths, 'Python'), 'myapp/utils.py')

    def test_third_party_import_does_not_resolve(self):
        all_paths = {'myapp/utils.py'}
        self.assertIsNone(resolve_import('requests', 'myapp/views.py', all_paths, 'Python'))


class ResolvePythonRelativeImportTests(SimpleTestCase):
    """Django-style relative imports (`from .serializers import X`,
    `from . import serializers`, `from ..app.models import X`) - the bug this
    covers: the old resolver mishandled every leading dot, so `views.py`
    never linked to its own `serializers.py`/`models.py` in the same app."""

    def test_single_dot_resolves_sibling_module(self):
        all_paths = {'accounts/views.py', 'accounts/serializers.py'}
        self.assertEqual(
            resolve_import('.serializers', 'accounts/views.py', all_paths, 'Python'), 'accounts/serializers.py',
        )

    def test_single_dot_resolves_sibling_package_init(self):
        all_paths = {'accounts/views.py', 'accounts/serializers/__init__.py'}
        self.assertEqual(
            resolve_import('.serializers', 'accounts/views.py', all_paths, 'Python'), 'accounts/serializers/__init__.py',
        )

    def test_single_dot_from_top_level_module(self):
        # importing_path has no directory component - base_dir is '' and the
        # relative target sits at the repo root alongside it.
        all_paths = {'views.py', 'serializers.py'}
        self.assertEqual(resolve_import('.serializers', 'views.py', all_paths, 'Python'), 'serializers.py')

    def test_double_dot_walks_up_to_sibling_app(self):
        all_paths = {'backend/orders/views.py', 'backend/accounts/models.py'}
        self.assertEqual(
            resolve_import('..accounts.models', 'backend/orders/views.py', all_paths, 'Python'),
            'backend/accounts/models.py',
        )

    def test_relative_import_with_no_matching_file_returns_none(self):
        all_paths = {'accounts/views.py'}
        self.assertIsNone(resolve_import('.serializers', 'accounts/views.py', all_paths, 'Python'))

    def test_relative_import_does_not_fall_back_to_suffix_match_elsewhere(self):
        # A same-named serializers.py living in a *different* package must
        # not be picked up for a relative import - unlike the absolute-import
        # case, the anchor here is exact, so no shortest-suffix guessing.
        all_paths = {'accounts/views.py', 'billing/serializers.py'}
        self.assertIsNone(resolve_import('.serializers', 'accounts/views.py', all_paths, 'Python'))

    def test_bare_dot_import_name_resolves_like_explicit_from_import(self):
        # extract_imports() turns `from . import serializers` into '.serializers'
        # - confirms resolve_import treats that identically to a normal
        # `from .serializers import X` specifier.
        all_paths = {'accounts/views.py', 'accounts/serializers.py'}
        self.assertEqual(
            resolve_import('.serializers', 'accounts/views.py', all_paths, 'Python'), 'accounts/serializers.py',
        )


class ResolveJSImportTests(SimpleTestCase):
    def test_resolves_relative_sibling_with_extension_added(self):
        all_paths = {'src/utils.js', 'src/App.jsx'}
        self.assertEqual(resolve_import('./utils', 'src/App.jsx', all_paths, 'JavaScript'), 'src/utils.js')

    def test_resolves_relative_index(self):
        all_paths = {'src/helpers/index.ts'}
        self.assertEqual(resolve_import('./helpers', 'src/App.tsx', all_paths, 'TypeScript'), 'src/helpers/index.ts')

    def test_resolves_parent_directory_relative_import(self):
        all_paths = {'src/lib/helper.js', 'src/pages/Page.jsx'}
        self.assertEqual(resolve_import('../lib/helper', 'src/pages/Page.jsx', all_paths, 'JavaScript'), 'src/lib/helper.js')

    def test_bare_specifier_never_resolves(self):
        all_paths = {'node_modules/react/index.js'}
        self.assertIsNone(resolve_import('react', 'src/App.jsx', all_paths, 'JavaScript'))

    def test_unresolvable_relative_import_returns_none(self):
        all_paths = {'src/App.jsx'}
        self.assertIsNone(resolve_import('./missing', 'src/App.jsx', all_paths, 'JavaScript'))
