"""Regression + adversarial tests for the Linux sandbox (Landlock + seccomp +
rlimits, see sandbox.py/_landlock.py/_seccomp.py).

These exercise the *real* sandboxing primitives - not mocks - through the
public `sandbox.run_python()` interface, the same way `analyses.engine` calls
it. They only run for real when this process is actually on Linux with a
working sandbox (`skipUnless` below): on this repo's macOS dev machines they
skip harmlessly, but they run for real in CI (ubuntu-latest, direct VM
execution - no Docker-imposed seccomp restriction on top) and were verified
during development via `docker build`/`docker run` of the actual
`backend/Dockerfile` image (see `.claude/memory/analysis-engine.md`).

Per the fail-closed contract: every "should be blocked" probe here either
lets the natural PermissionError/OSError propagate uncaught (becomes a
`status: error` result, the expected/passing outcome) or explicitly raises a
loud, distinguishable exception if the operation unexpectedly succeeded, so a
real regression shows up as a clearly-labelled failure, not a silent pass.
"""
import platform
import textwrap
from unittest import skipUnless

from django.test import SimpleTestCase

from analyses import sandbox

_LINUX_SANDBOX_AVAILABLE = platform.system() == 'Linux' and sandbox.is_available()
_SKIP_REASON = 'Linux sandbox (Landlock/seccomp) not available on this host'


class ParseTracebackTests(SimpleTestCase):
    """Pure-function tests - platform independent, run everywhere."""

    def test_message_truncated_regardless_of_length(self):
        stderr = 'Traceback (most recent call last):\n  File "submission.py", line 1\nValueError: ' + ('A' * 10_000)
        parsed = sandbox._parse_traceback(stderr)
        self.assertEqual(len(parsed['message']), 300)

    def test_multiline_crafted_message_still_truncated(self):
        payload = '\n'.join(f'line-{i}-secret-data' for i in range(500))
        stderr = f'Traceback (most recent call last):\n  File "submission.py", line 1\nRuntimeError: {payload}'
        parsed = sandbox._parse_traceback(stderr)
        self.assertLessEqual(len(parsed['message']), 300)

    def test_negative_returncode_with_no_traceback_is_not_reported_as_ok(self):
        # Regression: a process killed by signal (empty stderr) previously
        # fell through to {'status': 'ok'}, silently hiding a resource-limit
        # kill as "no issues found".
        result = sandbox._interpret_result(-9, b'')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'ResourceLimitExceeded')

    def test_clean_exit_is_ok(self):
        self.assertEqual(sandbox._interpret_result(0, b''), {'status': 'ok'})


@skipUnless(_LINUX_SANDBOX_AVAILABLE, _SKIP_REASON)
class LinuxSandboxHappyPathTests(SimpleTestCase):
    def test_clean_code_is_ok(self):
        self.assertEqual(sandbox.run_python('x = 1 + 1\n'), {'status': 'ok'})

    def test_uncaught_exception_reported(self):
        result = sandbox.run_python('def f():\n    return 1 / 0\nf()\n')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'ZeroDivisionError')
        self.assertEqual(result['line'], 2)

    def test_infinite_loop_times_out(self):
        result = sandbox.run_python('while True:\n    pass\n')
        # Normally the wall-clock timeout fires first ('timeout'); under heavy
        # host load RLIMIT_CPU's kill can occasionally win the race instead
        # (surfaced as 'error'/ResourceLimitExceeded) - both are a correct
        # kill, never 'ok'.
        if result['status'] == 'error':
            self.assertEqual(result['exception_type'], 'ResourceLimitExceeded')
        else:
            self.assertEqual(result['status'], 'timeout')

    def test_stdlib_imports_and_traceback_formatting_work(self):
        code = textwrap.dedent('''
            import json, math, re, collections, datetime
            data = json.dumps({'a': 1})
            assert json.loads(data)['a'] == 1
        ''')
        self.assertEqual(sandbox.run_python(code), {'status': 'ok'})


@skipUnless(_LINUX_SANDBOX_AVAILABLE, _SKIP_REASON)
class LinuxSandboxAdversarialTests(SimpleTestCase):
    """Each test actively tries to escape one specific isolation control."""

    def test_network_tcp_socket_blocked(self):
        code = textwrap.dedent('''
            import socket
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertIn(result['exception_type'], ('PermissionError',))

    def test_network_unix_domain_socket_blocked(self):
        code = textwrap.dedent('''
            import socket
            socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'PermissionError')

    def test_dns_resolution_blocked(self):
        code = "import socket\nsocket.getaddrinfo('example.com', 80)\n"
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')

    def test_read_project_root_denied_and_not_leaked(self):
        # The sandboxed process can't import this project's own code (that's
        # the whole point) - pass the path in as a literal instead.
        from analyses.sandbox import PROJECT_ROOT
        target = str(PROJECT_ROOT / 'config' / 'settings.py')
        code = f"with open({target!r}) as f:\n    print(f.read())\n"
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'PermissionError')
        self.assertNotIn('SECRET_KEY', result['message'])

    def test_proc_self_environ_denied(self):
        code = "open('/proc/self/environ').read()\n"
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'PermissionError')

    def test_proc_self_maps_denied(self):
        code = "open('/proc/self/maps').read()\n"
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'PermissionError')

    def test_environment_has_no_secrets(self):
        code = textwrap.dedent('''
            import os
            leaked = [k for k in os.environ if k in ('SECRET_KEY', 'DATABASE_URL_PROD', 'DATABASE_URL_DEV', 'GITHUB_TOKEN_ENCRYPTION_KEY', 'GITHUB_CLIENT_SECRET')]
            if leaked:
                raise AssertionError('LEAKED env vars: ' + ','.join(leaked))
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result, {'status': 'ok'})

    def test_symlink_to_forbidden_path_still_denied(self):
        from analyses.sandbox import PROJECT_ROOT
        target = str(PROJECT_ROOT / 'config' / 'settings.py')
        code = textwrap.dedent(f'''
            import os
            os.symlink({target!r}, 'evil_link')
            with open('evil_link') as f:
                f.read()
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'PermissionError')

    def test_working_directory_traversal_denied(self):
        code = textwrap.dedent('''
            import os
            os.chdir('../../../../etc')
            with open('passwd') as f:
                f.read()
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'PermissionError')

    def test_write_outside_scratch_denied(self):
        code = "open('/tmp/escaped.txt', 'w').write('x')\n"
        result = sandbox.run_python(code)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['exception_type'], 'PermissionError')

    def test_subprocess_child_inherits_network_deny(self):
        code = textwrap.dedent('''
            import subprocess, sys
            r = subprocess.run(
                [sys.executable, '-c', 'import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)'],
                capture_output=True,
            )
            if r.returncode == 0:
                raise AssertionError('LEAKED: subprocess created a socket')
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result, {'status': 'ok'})

    def test_forked_child_inherits_network_and_filesystem_deny(self):
        from analyses.sandbox import PROJECT_ROOT
        target = str(PROJECT_ROOT / 'config' / 'settings.py')
        code = textwrap.dedent(f'''
            import os, socket, sys

            pid = os.fork()
            if pid == 0:
                try:
                    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    os._exit(1)  # leaked
                except PermissionError:
                    pass
                try:
                    open({target!r}).read()
                    os._exit(1)  # leaked
                except PermissionError:
                    pass
                os._exit(0)
            else:
                _, status = os.waitpid(pid, 0)
                if status != 0:
                    raise AssertionError('LEAKED: forked child bypassed a restriction')
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result, {'status': 'ok'})

    def test_fork_bomb_contained_by_process_limit(self):
        code = textwrap.dedent('''
            import os
            created = 0
            kids = []
            try:
                for _ in range(200):
                    pid = os.fork()
                    if pid == 0:
                        os._exit(0)
                    kids.append(pid)
                    created += 1
            except (BlockingIOError, OSError):
                pass
            for k in kids:
                try:
                    os.waitpid(k, 0)
                except Exception:
                    pass
            if created > 150:
                raise AssertionError(f'fork bomb not contained: created {created}')
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result, {'status': 'ok'})

    def test_memory_bomb_blocked(self):
        code = "x = bytearray(500 * 1024 * 1024)\n"
        result = sandbox.run_python(code)
        # Either Python catches its own MemoryError (status 'error' with that
        # type) or the kernel SIGKILLs it for exceeding RLIMIT_AS (mapped to
        # ResourceLimitExceeded) - either way, never silently 'ok'.
        self.assertEqual(result['status'], 'error')
        self.assertIn(result['exception_type'], ('MemoryError', 'ResourceLimitExceeded'))

    def test_import_bypass_still_cannot_reach_network_or_forbidden_paths(self):
        """-I -S is defense-in-depth only (see sandbox.py's docstring on
        _run_python_linux) - even if a script defeats it and successfully
        imports a real installed package, the kernel-level layers must still
        hold. This deliberately imports `requests` (proving the bypass is
        real, not asserting -S is unbreakable) and then proves it still
        can't do anything with it."""
        import sysconfig
        from analyses.sandbox import PROJECT_ROOT
        target = str(PROJECT_ROOT / 'config' / 'settings.py')
        site_packages = sysconfig.get_paths()['purelib']
        code = textwrap.dedent(f'''
            import sys
            # The real bypass: -S keeps site-packages off sys.path by
            # default, but doesn't stop a script from putting it back itself.
            sys.path.insert(0, {site_packages!r})
            import requests

            try:
                requests.get('http://example.com', timeout=2)
                raise AssertionError('LEAKED: requests.get succeeded over the network')
            except AssertionError:
                raise
            except Exception:
                pass  # expected - network denied at the kernel level

            try:
                open({target!r}).read()
                raise AssertionError('LEAKED: read this project\\'s settings.py')
            except AssertionError:
                raise
            except PermissionError:
                pass  # expected
        ''')
        result = sandbox.run_python(code)
        self.assertEqual(result, {'status': 'ok'})
