"""Network-deny seccomp-bpf filter for the Linux sandbox, via `pyseccomp` (a
ctypes binding over libseccomp - verified installable, unprivileged, non-root,
in the real production image; no extra apt package needed).

An unprivileged process can install a seccomp filter on *itself* without any
capability (this is the whole design point of seccomp - it's what Chrome's
sandboxed renderer, systemd's SystemCallFilter=, etc. all rely on), and the
filter survives fork/exec and cannot be removed by the restricted process or
any of its descendants - verified empirically (see the adversarial test
suite) across a subprocess re-exec, an os.fork() child, and a
multiprocessing('fork') worker, not just assumed from the seccomp design docs.
"""
from __future__ import annotations

import errno as _errno

import pyseccomp as seccomp

# Every syscall that can create/use any kind of socket, including Unix-domain
# (same `socket()` syscall as TCP/UDP - one rule covers both) and io_uring
# (a documented seccomp-bypass vector for socket I/O on newer kernels - not
# just a network syscall in the traditional sense, but network-*capable*).
DENIED_SYSCALLS = (
    'socket', 'socketpair', 'connect', 'accept', 'accept4', 'bind', 'listen',
    'sendto', 'sendmsg', 'sendmmsg', 'recvfrom', 'recvmsg', 'recvmmsg',
    'getsockname', 'getpeername', 'setsockopt', 'getsockopt', 'shutdown',
    'io_uring_setup', 'io_uring_enter', 'io_uring_register',
)

_EACCES = 13


class SeccompSetupError(Exception):
    """A seccomp call failed for a reason other than "this syscall name
    doesn't exist on this architecture" - never silently ignored, always
    aborts sandbox setup (see apply_network_deny's fail-closed contract)."""


def apply_network_deny() -> None:
    """Denies every syscall in DENIED_SYSCALLS with EACCES, default-allow
    otherwise (an allowlist covering "everything Python's stdlib/interpreter
    startup might need" is far riskier to get subtly wrong than a small,
    well-understood denylist of exactly the network-capable syscalls).

    Fail-closed: only a syscall name that is a genuine "not defined on this
    build/architecture" (EINVAL from pyseccomp's name resolution) is skipped -
    that syscall doesn't exist here, so there's nothing to deny. Any other
    failure aborts the whole filter setup by raising, rather than loading a
    silently-weaker filter; callers must treat that as sandbox-unavailable."""
    f = seccomp.SyscallFilter(defaction=seccomp.ALLOW)
    resolved = 0
    for name in DENIED_SYSCALLS:
        try:
            f.add_rule(seccomp.ERRNO(_EACCES), name)
            resolved += 1
        except OSError as exc:
            if exc.errno == _errno.EINVAL:
                continue
            raise SeccompSetupError(f'seccomp add_rule failed for {name!r}: {exc}') from exc

    if resolved == 0:
        raise SeccompSetupError('no network syscalls resolved on this build - refusing to load an empty filter')

    f.load()
