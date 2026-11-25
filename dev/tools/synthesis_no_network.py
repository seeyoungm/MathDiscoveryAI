"""Export a native deny-network seccomp filter for the local synthesis namespace."""
import ctypes as c
import errno
import sys

lib = c.CDLL("libseccomp.so.2")
lib.seccomp_init.argtypes = [c.c_uint32]
lib.seccomp_init.restype = c.c_void_p
lib.seccomp_syscall_resolve_name.argtypes = [c.c_char_p]
lib.seccomp_syscall_resolve_name.restype = c.c_int
lib.seccomp_rule_add.argtypes = [c.c_void_p, c.c_uint32, c.c_int, c.c_uint]
lib.seccomp_rule_add.restype = c.c_int
lib.seccomp_export_bpf.argtypes = [c.c_void_p, c.c_int]
lib.seccomp_export_bpf.restype = c.c_int
lib.seccomp_release.argtypes = [c.c_void_p]
ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
if not ctx:
    raise RuntimeError("seccomp initialization failed")
try:
    deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO(EPERM)
    for name in ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                 "sendto", "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
                 "io_uring_setup", "io_uring_enter", "io_uring_register"):
        no = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
        if no < 0 or lib.seccomp_rule_add(ctx, deny, no, 0):
            raise RuntimeError(f"could not deny {name}")
    if lib.seccomp_export_bpf(ctx,sys.stdout.fileno()):
        raise RuntimeError("could not export seccomp BPF")
finally:
    lib.seccomp_release(ctx)
