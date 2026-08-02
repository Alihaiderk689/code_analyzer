"""Best-effort sandboxed execution of submitted Python code, to catch runtime
errors (IndexError, ZeroDivisionError, etc.) that static analysis fundamentally
cannot predict without actually running the code.

This is NOT container-grade isolation - there is no Docker on this host, so it
uses macOS's Seatbelt sandbox (`sandbox-exec`) instead:
  - all network access is denied, for the whole process tree (verified this
    also blocks subprocesses/shell-outs spawned by the submitted code, not
    just the top-level interpreter)
  - all filesystem writes are denied except a per-run scratch directory
  - reads of this Django project's own directory are denied (so submitted
    code can't read .env / secrets), general filesystem reads are otherwise
    allowed (a real, disclosed limitation - see module docstring below)
  - stdin is closed, so input() raises EOFError immediately instead of
    hanging - the same thing that would happen running the script
    non-interactively (e.g. `python script.py < /dev/null`)
  - CPU time, process count, and core dumps are capped via POSIX rlimits
  - a wall-clock timeout is the backstop for anything rlimits don't catch

Known gaps, disclosed rather than silently ignored:
  - `sandbox-exec` is macOS-only. On any other platform (i.e. most real Linux
    production servers) this refuses to execute rather than silently running
    unsandboxed - see `is_available()`. Callers surface this to the user
    (see analyses.engine._python_runtime_issues) instead of degrading quietly.
  - Memory limits (RLIMIT_AS / RLIMIT_DATA) cannot be lowered at all on macOS
    - confirmed empirically, not an oversight - so the wall-clock timeout is
      the only real bound on memory exhaustion, not a hard memory cap.
    RLIMIT_FSIZE *does* work, though, and is set below to stop a submission
    from filling the disk with output before the timeout fires.
  - General file reads outside this project are allowed (Python's stdlib
    needs to read broadly just to start up), so a submission could still
    read other world-readable files on the host. What's specifically and
    deliberately blocked is this project's own directory (.env/secrets) and
    the invoking user's home directory (SSH keys, cloud credentials, shell
    history, etc.) - the two places real secrets are actually likely to be.
  - process-exec is intentionally left unrestricted (tried narrowing it to
    just SYSTEM_PYTHON and reverted - on this and presumably many macOS hosts
    /usr/bin/python3 is itself an Xcode Command Line Tools stub that re-execs
    into a *different* real binary path, so a literal-path allowlist broke
    the sandbox's own interpreter startup). Contained instead by the
    network-deny and scratch-dir-only-write rules applying to the whole
    process tree, same as before.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TIMEOUT_SECONDS = 5
CPU_TIME_SECONDS = 5
MAX_PROCESSES = 16
MAX_OUTPUT_FILE_BYTES = 10 * 1024 * 1024

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOME_DIR = str(Path.home())
SYSTEM_PYTHON = '/usr/bin/python3'


def is_available():
    """True only when we have a real sandboxing primitive to use. Refuses to
    run at all otherwise, rather than falling back to unsandboxed execution."""
    return shutil.which('sandbox-exec') is not None and Path(SYSTEM_PYTHON).exists()


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


def _limit_resources():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_TIME_SECONDS, CPU_TIME_SECONDS))
    resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_FILE_BYTES, MAX_OUTPUT_FILE_BYTES))


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


def run_python(code):
    """Runs `code` in the sandbox described above. Returns a dict:
      {'status': 'ok'}                                        - exited cleanly
      {'status': 'error', 'exception_type', 'message', 'line'} - uncaught exception
      {'status': 'import_error', ...}                          - failed on a missing third-party
                                                                  package, not a real bug (see below)
      {'status': 'timeout'}                                    - exceeded the wall-clock timeout
      {'status': 'unavailable'}                                - no sandboxing primitive on this host
    """
    if not is_available():
        return {'status': 'unavailable'}

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

        try:
            result = subprocess.run(
                ['sandbox-exec', '-f', str(profile_path), SYSTEM_PYTHON, '-B', str(script_path)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                env={'PATH': '/usr/bin:/bin'},
                # cwd is the scratch dir too, so a script writing a relative-path output
                # file (a normal, non-buggy thing to do) succeeds there instead of being
                # flagged as a false-positive PermissionError - it's still confined to
                # this ephemeral, auto-deleted directory either way.
                cwd=scratch_dir,
                preexec_fn=_limit_resources,
            )
        except subprocess.TimeoutExpired:
            return {'status': 'timeout'}

        if result.returncode == 0:
            return {'status': 'ok'}

        parsed = _parse_traceback(result.stderr.decode('utf-8', errors='replace'))
        if parsed is None:
            return {'status': 'ok'}
        if parsed['exception_type'] in _IMPORT_ERROR_TYPES:
            # The sandbox deliberately runs with a bare system Python, not this
            # project's venv or the analyzed code's own environment - it has
            # none of Django/requests/numpy/whatever third-party packages the
            # real code depends on. A failed import here says nothing about
            # whether the code itself is correct, so it must not be reported
            # as a bug (see analyses.engine._python_runtime_issues).
            return {'status': 'import_error', **parsed}
        return {'status': 'error', **parsed}
