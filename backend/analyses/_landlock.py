"""Hand-rolled ctypes bindings for Linux Landlock (no PyPI package exists for
this - verified). Landlock is a kernel LSM specifically designed to let an
*unprivileged* process restrict its own filesystem access (no CAP_SYS_ADMIN, no
new namespace) - available since Linux 5.13, syscall numbers 444/445/446, which
have been stable/consistent across x86_64 and arm64 since introduction (both use
the "generic" post-4.17 syscall numbering table for new syscalls).

The read-only grant list (/lib, /usr/lib, /usr/local) was arrived at by
`strace`-driven measurement of what a plain interpreter startup + common stdlib
imports actually opens - not a guess. An initial narrower design (grant only
the specific non-site-packages entries of the stdlib dir, to keep pip-installed
packages unreadable) was tried and rejected: it broke a *subprocess re-exec of
python3* under the restriction (CPython's early bootstrap - before `encodings`
is even loadable - needs directory-level traversal on the stdlib dir itself,
not just leaf grants on the entries within it, confirmed via PYTHONVERBOSE
tracing). Granting /usr/local wholesale (read+execute only) does mean
site-packages becomes readable - accepted as within the intended threat model:
those are just installed pip packages (not secret), and the actual boundary
(network denied by seccomp, `/app`'s settings/secrets never granted at all)
holds regardless of what a script can `import` - verified empirically (see the
adversarial test suite) rather than assumed.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform

_SUPPORTED_MACHINES = {'x86_64', 'aarch64', 'arm64'}

_SYS_landlock_create_ruleset = 444
_SYS_landlock_add_rule = 445
_SYS_landlock_restrict_self = 446

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

# ABI v1 access-fs rights (the original, universally-supported set - Landlock
# has been mainline since 5.13 with exactly these 13 bits; newer kernels add
# more (LANDLOCK_ACCESS_FS_REFER etc in v2+) but restricting only the v1 set is
# always valid on a newer kernel too, so there's no version-branching needed).
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12

ABI_V1_HANDLED_FS = (
    FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_READ_DIR | FS_REMOVE_DIR
    | FS_REMOVE_FILE | FS_MAKE_CHAR | FS_MAKE_DIR | FS_MAKE_REG | FS_MAKE_SOCK
    | FS_MAKE_FIFO | FS_MAKE_BLOCK | FS_MAKE_SYM
)

READ_ONLY_RIGHTS = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
FULL_RIGHTS = ABI_V1_HANDLED_FS


class _RulesetAttr(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


class LandlockUnsupported(Exception):
    """The running kernel/arch definitively doesn't support Landlock (ENOSYS/
    EOPNOTSUPP querying support, or an unsupported CPU architecture) - the
    caller should treat the sandbox as unavailable, not proceed with a
    partial/no-op restriction."""


class LandlockSetupError(Exception):
    """A Landlock call failed for a reason *other* than "not supported" (a bad
    path, a real OS error, etc). Never silently ignored - always aborts sandbox
    setup, never a silently-weaker ruleset."""


_PR_SET_NO_NEW_PRIVS = 38


def _libc():
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    return lib


def _set_no_new_privs(lib) -> None:
    # landlock_restrict_self (like an unprivileged seccomp filter) requires
    # either CAP_SYS_ADMIN or PR_SET_NO_NEW_PRIVS - this process has neither
    # capability nor root, so it must set the latter itself.
    ret = lib.prctl(ctypes.c_int(_PR_SET_NO_NEW_PRIVS), ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
    if ret != 0:
        errno = ctypes.get_errno()
        raise LandlockSetupError(f'prctl(PR_SET_NO_NEW_PRIVS) failed (errno={errno}): {os.strerror(errno)}')


def is_supported() -> int | None:
    """Returns the kernel's Landlock ABI version if supported, else None.
    Pure query - does NOT restrict the calling process (flags carries only
    LANDLOCK_CREATE_RULESET_VERSION, attr/size are ignored by the kernel in
    that mode). Safe to call from the Django server process itself."""
    if platform.machine() not in _SUPPORTED_MACHINES:
        return None
    lib = _libc()
    ret = lib.syscall(_SYS_landlock_create_ruleset, None, ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    if ret < 0:
        return None
    return ret


def _create_ruleset(lib) -> int:
    attr = _RulesetAttr(handled_access_fs=ABI_V1_HANDLED_FS)
    ret = lib.syscall(_SYS_landlock_create_ruleset, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if ret < 0:
        errno = ctypes.get_errno()
        if errno in (38, 95):  # ENOSYS, EOPNOTSUPP
            raise LandlockUnsupported(f'landlock_create_ruleset unsupported (errno={errno})')
        raise LandlockSetupError(f'landlock_create_ruleset failed (errno={errno}): {os.strerror(errno)}')
    return ret


_FILE_VALID_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE


def _add_rule(lib, ruleset_fd: int, path: str, access: int) -> None:
    if not os.path.exists(path):
        # Not every candidate path exists on every build (e.g. no /lib64 on
        # some architectures) - skipping a path that genuinely isn't there is
        # not "silently weakening a rule", there's nothing to restrict.
        return
    # The kernel rejects (EINVAL) any directory-only right (READ_DIR,
    # MAKE_DIR, ...) attached to a rule whose target is a regular file - mask
    # down to the file-compatible subset for those, full `access` for dirs.
    effective_access = access if os.path.isdir(path) else (access & _FILE_VALID_RIGHTS)
    if effective_access == 0:
        return
    try:
        parent_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise LandlockSetupError(f'failed to open {path!r} for a Landlock rule: {exc}') from exc
    try:
        rule = _PathBeneathAttr(allowed_access=effective_access, parent_fd=parent_fd)
        ret = lib.syscall(_SYS_landlock_add_rule, ctypes.c_int(ruleset_fd), ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH), ctypes.byref(rule), ctypes.c_uint32(0))
        if ret < 0:
            errno = ctypes.get_errno()
            raise LandlockSetupError(f'landlock_add_rule failed for {path!r} (errno={errno}): {os.strerror(errno)}')
    finally:
        os.close(parent_fd)


def restrict_to(scratch_dir: str, extra_read_exec_paths=()) -> None:
    """Restricts the CURRENT process (meant to be called from a preexec_fn,
    i.e. after fork, before exec) to:
      - read+write+create/remove everything under scratch_dir
      - read+execute the specific system/interpreter paths actually needed to
        start Python and import stdlib modules (measured via strace, not
        guessed - see module docstring), plus any caller-supplied
        `extra_read_exec_paths` - the interpreter Python actually runs as
        isn't always under one of the hardcoded paths below (e.g. GitHub
        Actions' `actions/setup-python` installs under
        /opt/hostedtoolcache/..., not /usr/local like this repo's own Docker
        image) - see sandbox.py's caller for how that path is derived.
      - nothing else at all (default-deny once any rule is installed)

    Raises LandlockUnsupported/LandlockSetupError rather than degrading
    quietly - subprocess.Popen propagates an exception raised here to the
    *parent* process (the child never execs), and callers must treat that as
    "sandbox unavailable", never "run anyway"."""
    if is_supported() is None:
        raise LandlockUnsupported('Landlock not supported on this kernel/architecture')

    lib = _libc()
    ruleset_fd = _create_ruleset(lib)
    try:
        _add_rule(lib, ruleset_fd, scratch_dir, FULL_RIGHTS)

        for path in ('/etc/ld.so.cache', '/etc/localtime'):
            _add_rule(lib, ruleset_fd, path, FS_READ_FILE)

        for path in ('/lib', '/lib64', '/usr/lib', '/usr/local', *extra_read_exec_paths):
            _add_rule(lib, ruleset_fd, path, READ_ONLY_RIGHTS)

        _set_no_new_privs(lib)
        ret = lib.syscall(_SYS_landlock_restrict_self, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
        if ret < 0:
            errno = ctypes.get_errno()
            raise LandlockSetupError(f'landlock_restrict_self failed (errno={errno}): {os.strerror(errno)}')
    finally:
        os.close(ruleset_fd)
