#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "$0")/.." && pwd -P)"
cd "$root"
args=(--unshare-user --die-with-parent --clearenv --setenv PATH /usr/bin --setenv HOME /tmp
      --setenv PYTHONDONTWRITEBYTECODE 1 --ro-bind /usr /usr --symlink usr/lib /lib
      --symlink usr/lib64 /lib64 --dev /dev --tmpfs /tmp --ro-bind "$root" /work --chdir /work)
case "$1" in
probe)
  test "$#" = 2 && test -f "$2"
  bwrap "${args[@]}" /usr/bin/python3 -c '
from pathlib import Path
import sys
assert Path("/work/protocol.yaml").is_file()
try:
    with Path(sys.argv[1]).open("rb"): pass
except (PermissionError, FileNotFoundError):
    print("PASS: evaluation path not readable; development protocol readable")
else:
    raise SystemExit("FAIL: evaluation file readable")' "$2"
  ;;
tests)
  bwrap "${args[@]}" /usr/bin/python3 -m unittest discover -s tests -v
  ;;
smoke)
  test "$#" = 2
  args+=(--bind "$root/evidence" /work/evidence)
  abc=()
  if [[ "$2" != "-" ]]; then
    test -f "$2" && test -x "$2"
    args+=(--ro-bind "$2" /abc)
    abc=(--abc /abc)
  fi
  bwrap "${args[@]}" /usr/bin/python3 -m mdai.integration "${abc[@]}"
  ;;
*) echo 'usage: dev_sandbox.sh probe ABS_EVAL_PATH | tests | smoke ABS_ABC_PATH_OR_DASH' >&2; exit 2 ;;
esac
