#!/usr/bin/env python3
"""Small, process-isolated SMT-LIB bridge to the installed official Z3 C API."""
import ctypes as C
import ctypes.util
import sys

def main():
    lib = C.CDLL(ctypes.util.find_library("z3") or "libz3.so.4")
    ptr = C.c_void_p
    callback = C.CFUNCTYPE(None, ptr, C.c_int)
    for name, args, result in [
        ("Z3_get_full_version", [], C.c_char_p),
        ("Z3_mk_config", [], ptr),
        ("Z3_del_config", [ptr], None),
        ("Z3_mk_context", [ptr], ptr),
        ("Z3_del_context", [ptr], None),
        ("Z3_set_error_handler", [ptr, callback], None),
        ("Z3_eval_smtlib2_string", [ptr, C.c_char_p], C.c_char_p),
    ]:
        fn = getattr(lib, name)
        fn.argtypes, fn.restype = args, result
    if sys.argv[1:] in (["-version"], ["--version"]):
        print("Z3 " + lib.Z3_get_full_version().decode("ascii"))
        return 0
    if sorted(sys.argv[1:]) != ["-in", "-smt2"]:
        print("expected -in -smt2", file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024 or b"\0" in data:
        print("SMT input rejected", file=sys.stderr)
        return 2
    cfg = lib.Z3_mk_config()
    ctx = lib.Z3_mk_context(cfg)
    lib.Z3_del_config(cfg)
    errors = []
    cb = callback(lambda _, code: errors.append(code))
    try:
        lib.Z3_set_error_handler(ctx, cb)
        result = lib.Z3_eval_smtlib2_string(ctx, data)
        if result:
            sys.stdout.buffer.write(result)
        return 1 if errors else 0
    finally:
        lib.Z3_del_context(ctx)

if __name__ == "__main__":
    raise SystemExit(main())
