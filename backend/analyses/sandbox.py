"""Best-effort sandboxed execution of submitted Python code, to catch runtime
errors (IndexError, ZeroDivisionError, etc.) that static analysis fundamentally
cannot predict without actually running the code.

Two independent, platform-specific implementations, dispatched by
`platform.system()` - each is real, kernel-enforced isolation for its own
platform, not a shared abstraction papering over different guarantees:

- **macOS** (dev machines): `sandbox-exec` (Seatbelt) - unchanged from before.
- **Linux** (production - Render, and this repo's Docker images): Landlock
  (`_landlock.py`, filesystem) + seccomp-bpf (`_seccomp.py`, network) + POSIX
  rlimits, all self-applied by the child process with no elevated privilege,
  no added Docker capability, no privileged/cap_add/docker.sock anywhere in
  this repo - verified empirically against the real `backend/Dockerfile`
  image, unprivileged and non-root, not assumed from documentation. See
  `.claude/memory/analysis-engine.md` for how this was verified and what was
  deliberately traded off (documented per-mechanism below and in
  `_landlock.py`'s module docstring).

Both platforms share the same contract:
  - network access is denied for the whole process tree, including a
    subprocess/fork/multiprocessing-worker spawned by the submitted code, not
    just the top-level interpreter (verified for both platforms).
  - filesystem writes are denied except a per-run scratch directory.
  - this Django project's own directory (secrets-adjacent: settings.py,
    encryption keys) and the invoking user's home directory are never
    readable by the sandboxed process.
  - stdin is closed, so input() raises EOFError immediately.
  - CPU time, process count, and core dumps are capped via POSIX rlimits; a
    wall-clock timeout (applied to the whole process *group*, not just the
    direct child, so a forked grandchild can't outlive it) is the backstop.
  - a genuine, kernel-enforced memory cap exists on Linux (RLIMIT_AS - a
    no-op on macOS, see below).

Neither implementation ever falls back to running submitted code without
these protections - if the platform-appropriate sandboxing primitive isn't
available or fails to initialize, `run_python()` returns
`{'status': 'unavailable'}` and the caller (`analyses.engine`) surfaces that
as a zero-penalty informational issue, never silent unsandboxed execution.
"""
from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 5
# Deliberately > TIMEOUT_SECONDS: RLIMIT_CPU is second-line insurance, not the
# primary backstop - the wall-clock timeout should reliably fire first for an
# ordinary CPU-bound infinite loop. Equal values raced nondeterministically
# under load (observed: RLIMIT_CPU's SIGKILL occasionally beat the wall-clock
# timeout to it, which is still a correct kill, just a less predictable one -
# see _interpret_result's ResourceLimitExceeded handling for that case).
CPU_TIME_SECONDS = TIMEOUT_SECONDS + 3
MAX_PROCESSES = 16  # macOS only - unchanged from before Linux support existed.
# Linux's RLIMIT_NPROC is counted per real UID *system-wide* (not scoped to
# one sandbox run's process tree) - and in this container appuser also owns
# the gunicorn/manage.py process itself plus its baseline threads, which eats
# into a shared-with-macOS value before a submission gets to fork/subprocess
# at all (measured: 16 wasn't enough headroom for even one legitimate nested
# fork in this exact image - see .claude/memory/analysis-engine.md). A fork
# bomb is still bounded by the hard wall-clock timeout + RLIMIT_CPU regardless
# of this ceiling, so raising it doesn't weaken that defense.
MAX_PROCESSES_LINUX = 64
MAX_OUTPUT_FILE_BYTES = 10 * 1024 * 1024
# Linux only - RLIMIT_AS is a genuine, kernel-enforced cap there (empirically
# verified: a legitimate, moderately memory-heavy script - sorting/JSON/regex
# over 50k records - peaks under 50MB; a deliberate 500MB allocation attempt
# is blocked). It's a documented no-op on macOS (see _limit_resources_darwin).
MAX_MEMORY_BYTES = 256 * 1024 * 1024

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_DIR = str(Path.home())
SYSTEM_PYTHON = '/usr/bin/python3'  # macOS only - see _run_python_darwin.
# Two levels above sys.executable's *resolved* path (following symlinks, so a
# venv still resolves to the real interpreter) - covers wherever Python was
# actually installed even when that differs from _landlock.py's hardcoded
# /usr/local: this repo's own Docker image puts it there, but e.g. GitHub
# Actions' actions/setup-python installs under
# /opt/hostedtoolcache/Python/<ver>/<arch>/bin/python instead. Landlock must
# grant read+execute on whatever this resolves to, or the sandboxed
# interpreter can't even exec itself - see _run_python_linux.
INTERPRETER_ROOT_LINUX = str(Path(sys.executable).resolve().parent.parent)

_PLATFORM = platform.system()

_landlock = None
_seccomp = None
if _PLATFORM == 'Linux':
    try:
        from . import _landlock
    except Exception:
        logger.exception('sandbox.linux_landlock_import_failed')
    try:
        from . import _seccomp
    except Exception:
        logger.exception('sandbox.linux_seccomp_import_failed')


def is_available():
    """True only when we have a real, kernel-enforced sandboxing primitive on
    this exact host. Refuses to run at all otherwise, rather than falling
    back to unsandboxed execution."""
    if _PLATFORM == 'Darwin':
        return shutil.which('sandbox-exec') is not None and Path(SYSTEM_PYTHON).exists()
    if _PLATFORM == 'Linux':
        return _linux_is_available()
    return False


_linux_available_cache = None


def _linux_is_available():
    # Kernel/toolchain support doesn't change while the process is running -
    # compute once. A per-call preexec_fn failure (see _run_python_linux) is
    # handled separately and doesn't invalidate this cache; it's a narrower,
    # call-specific safety net on top of this host-capability check.
    global _linux_available_cache
    if _linux_available_cache is None:
        _linux_available_cache = bool(
            _landlock is not None
            and _seccomp is not None
            and _landlock.is_supported() is not None
            and Path(sys.executable).exists()
        )
    return _linux_available_cache


# ---------------------------------------------------------------------------
# Shared: process-group timeout, traceback parsing, result interpretation
# ---------------------------------------------------------------------------

def _run_with_group_timeout(argv, env, cwd, preexec_fn, timeout):
    """subprocess.run(timeout=...) only kills the *direct* child on timeout -
    a forked grandchild survives past the deadline. Starting a new session
    (process group) and killing the whole group closes that gap, for both
    platforms.

    Returns a CompletedProcess, or None on timeout. Raises whatever Popen()
    raises (notably subprocess.SubprocessError if preexec_fn itself failed -
    e.g. Landlock/seccomp setup - the child never got to exec at all; callers
    must treat that as sandbox-unavailable, not retry unsandboxed)."""
    proc = subprocess.Popen(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=cwd, preexec_fn=preexec_fn, start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        return None
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


_TRACEBACK_LAST_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)')


def _parse_traceback(stderr_text):
    """Best-effort extraction of (exception_type, message, line) from a
    Python traceback written to stderr. Falls back to raw stderr if the
    shape doesn't match (e.g. a segfault or an interpreter-level crash)."""
    lines = [l for l in stderr_text.strip().splitlines() if l.strip()]
    if not lines:
        return None

    last_line = lines[-1]
    match = re.match(r'^(\w+(?:\.\w+)*Error|^\w+Warning|^\w+Exception)(?::\s*(.*))?$', last_line)
    if not match:
        # Not a recognizable Python exception line - just return the tail of stderr.
        return {'exception_type': 'Error', 'message': last_line[:300], 'line': None}

    exception_type, message = match.group(1), (match.group(2) or '').strip()
    line = None
    frame_matches = list(_TRACEBACK_LAST_FRAME.finditer(stderr_text))
    if frame_matches:
        last_frame = frame_matches[-1]
        if last_frame.group('file') == '<string>' or last_frame.group('file').endswith('submission.py'):
            line = int(last_frame.group('line'))

    return {'exception_type': exception_type, 'message': message[:300], 'line': line}


_IMPORT_ERROR_TYPES = {'ModuleNotFoundError', 'ImportError'}


def _interpret_result(returncode, stderr_bytes):
    if returncode == 0:
        return {'status': 'ok'}

    parsed = _parse_traceback(stderr_bytes.decode('utf-8', errors='replace'))
    if parsed is None:
        if returncode < 0:
            # Killed by signal with no parseable traceback (empty stderr) -
            # e.g. SIGKILL from hitting RLIMIT_AS/RLIMIT_CPU, or an OOM kill.
            # Previously this fell through to {'status': 'ok'}, silently
            # reporting a resource-limit kill as "no issues found" - a real
            # bug, and one that would make the new memory limit invisible.
            try:
                signal_name = signal.Signals(-returncode).name
            except ValueError:
                signal_name = str(-returncode)
            return {
                'status': 'error', 'exception_type': 'ResourceLimitExceeded',
                'message': f'Process was terminated by signal {signal_name} (likely a memory or resource limit violation).',
                'line': None,
            }
        return {'status': 'ok'}

    if parsed['exception_type'] in _IMPORT_ERROR_TYPES:
        # Neither sandbox runs with this project's own dependencies (see each
        # platform's implementation below) - a failed import says nothing
        # about whether the submitted code itself is correct.
        return {'status': 'import_error', **parsed}
    return {'status': 'error', **parsed}


def run_python(code):
    """Runs `code` in the platform-appropriate sandbox. Returns a dict:
      {'status': 'ok'}                                        - exited cleanly
      {'status': 'error', 'exception_type', 'message', 'line'} - uncaught exception
      {'status': 'import_error', ...}                          - failed on a missing third-party
                                                                  package, not a real bug (see below)
      {'status': 'timeout'}                                    - exceeded the wall-clock timeout
      {'status': 'unavailable'}                                - no sandboxing primitive on this host
    """
    if not is_available():
        return {'status': 'unavailable'}
    if _PLATFORM == 'Darwin':
        return _run_python_darwin(code)
    return _run_python_linux(code)


# ---------------------------------------------------------------------------
# macOS: sandbox-exec (Seatbelt) - unchanged behavior from before this file
# gained Linux support.
# ---------------------------------------------------------------------------

def _build_profile(scratch_dir):
    return f'''(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow file-read*)
(deny file-read* (subpath "{PROJECT_ROOT}"))
(deny file-read* (subpath "{HOME_DIR}"))
(allow file-read* file-write* (subpath "{scratch_dir}"))
(deny file-write*)
(deny network*)
(allow file-read-metadata)
(allow sysctl-read)
(allow mach-lookup)
(allow iokit-open)
'''


def _limit_resources_darwin():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_TIME_SECONDS, CPU_TIME_SECONDS))
    resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES, MAX_OUTPUT_FILE_BYTES))
    # No RLIMIT_AS/RLIMIT_DATA here - confirmed empirically these cannot be
    # lowered on macOS at all, so the wall-clock timeout is the only real
    # bound on memory exhaustion on this platform, not a hard memory cap.


def _run_python_darwin(code):
    with tempfile.TemporaryDirectory() as raw_scratch_dir:
        # macOS's tempdir (/var/folders/...) is a symlink to /private/var/folders/...,
        # and Seatbelt matches subpaths against the resolved realpath - resolve it up
        # front so the profile's rules actually match what the OS sees, instead of
        # silently denying access to the scratch dir the script is supposed to use.
        scratch_dir = str(Path(raw_scratch_dir).resolve())
        script_path = Path(scratch_dir) / 'submission.py'
        script_path.write_text(code)
        profile_path = Path(scratch_dir) / 'profile.sb'
        profile_path.write_text(_build_profile(scratch_dir))

        argv = ['sandbox-exec', '-f', str(profile_path), SYSTEM_PYTHON, '-B', str(script_path)]
        try:
            result = _run_with_group_timeout(
                argv, env={'PATH': '/usr/bin:/bin'}, cwd=scratch_dir,
                preexec_fn=_limit_resources_darwin, timeout=TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError):
            # OSError (not just SubprocessError) matters here: a preexec_fn
            # failure is reported as SubprocessError, but a failed exec()
            # itself - e.g. permission denied on the target binary - surfaces
            # as a plain OSError subclass (PermissionError, FileNotFoundError,
            # ...) raised directly from subprocess's child-error-reporting
            # pipe, uncaught by SubprocessError alone (see the Linux sibling's
            # comment for how this was actually hit).
            logger.exception('sandbox.darwin_preexec_failed')
            return {'status': 'unavailable'}
        if result is None:
            return {'status': 'timeout'}
        return _interpret_result(result.returncode, result.stderr)


# ---------------------------------------------------------------------------
# Linux: Landlock (filesystem) + seccomp-bpf (network) + rlimits (CPU/procs/
# memory), all self-applied unprivileged - see _landlock.py/_seccomp.py for
# the mechanisms and how they were verified.
# ---------------------------------------------------------------------------

def _limit_resources_linux():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_TIME_SECONDS, CPU_TIME_SECONDS))
    resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES_LINUX, MAX_PROCESSES_LINUX))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES, MAX_OUTPUT_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))


def _run_python_linux(code):
    with tempfile.TemporaryDirectory() as raw_scratch_dir:
        scratch_dir = str(Path(raw_scratch_dir).resolve())
        script_path = Path(scratch_dir) / 'submission.py'
        script_path.write_text(code)

        def _preexec():
            # Order matters: rlimits first, then filesystem (Landlock), then
            # network (seccomp) last - most-restrictive-last, and any failure
            # here aborts before exec (see _run_with_group_timeout's docstring)
            # rather than exec'ing with a partial/weaker restriction.
            _limit_resources_linux()
            _landlock.restrict_to(scratch_dir, extra_read_exec_paths=(INTERPRETER_ROOT_LINUX,))
            _seccomp.apply_network_deny()

        # -I (isolated mode) + -S (skip all site init, including global
        # site-packages) stop the sandboxed script from trivially `import
        # django`/any pip-installed package via the *normal* startup path -
        # there's no separate bare-system-Python on Linux the way macOS has
        # /usr/bin/python3 distinct from this project's own interpreter, so
        # without this the sandboxed script would otherwise share the exact
        # same importable package set as the running Django process.
        #
        # This is explicitly defense-in-depth, not the security boundary: a
        # script can still defeat it (sys.path.insert back to the
        # Landlock-granted /usr/local tree, which must stay readable for the
        # interpreter itself to start - see _landlock.py's docstring on why
        # a narrower per-entry grant that excluded site-packages was tried
        # and rejected). Verified empirically that even a successful `import
        # requests`/`import django` after such a bypass still can't reach the
        # network (seccomp) or read anything under this project's own
        # directory (Landlock never grants it) - the kernel-level layers are
        # what actually hold, regardless of what's importable.
        argv = [sys.executable, '-I', '-S', '-B', str(script_path)]
        try:
            result = _run_with_group_timeout(
                argv, env={'PATH': '/usr/bin:/bin'}, cwd=scratch_dir,
                preexec_fn=_preexec, timeout=TIMEOUT_SECONDS,
            )
        except (subprocess.SubprocessError, OSError):
            # OSError, not just SubprocessError: a preexec_fn failure (e.g.
            # Landlock/seccomp setup itself raising) is reported as
            # SubprocessError, but a failed exec() of the interpreter binary
            # - e.g. Landlock denying execute on a path outside the granted
            # set - surfaces as a plain OSError subclass (PermissionError)
            # raised directly from subprocess's child-error-reporting pipe.
            # Hit for real in CI: GitHub Actions' actions/setup-python
            # installs Python under /opt/hostedtoolcache/..., which wasn't
            # covered before INTERPRETER_ROOT_LINUX was added above, and this
            # except clause didn't catch the resulting PermissionError -
            # it propagated uncaught instead of degrading to 'unavailable'.
            logger.exception('sandbox.linux_preexec_failed')
            return {'status': 'unavailable'}
        if result is None:
            return {'status': 'timeout'}
        return _interpret_result(result.returncode, result.stderr)
