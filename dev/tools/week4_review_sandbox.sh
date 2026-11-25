#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname "$0")/.." && pwd -P)"
manifest="$root/../records/week2/week2_smoke/dataset_manifest.jsonl"
case "${1:-}" in
tests)
  test "$#" = 1
  cmd=(-m unittest discover -s tests -v) ;;
readiness|headroom)
  test "$#" = 2 && [[ "$2" =~ ^[a-f0-9]{64}$ ]]
  mod=w4review
  if [[ "$1" = headroom ]]; then mod=w4headroom; fi
  cmd=(-m "mdai.$mod" --expansion-summary-sha256 "$2") ;;
*) echo "usage: $0 tests | readiness SHA256 | headroom SHA256" >&2; exit 2 ;;
esac
exec bwrap --unshare-user --die-with-parent --clearenv --setenv PATH /usr/bin --setenv HOME /tmp   --setenv PYTHONDONTWRITEBYTECODE 1 --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64   --dev /dev --tmpfs /tmp --ro-bind "$root" /bundle/dev   --ro-bind "$manifest" /bundle/records/week2/week2_smoke/dataset_manifest.jsonl   --chdir /bundle/dev /usr/bin/python3 "${cmd[@]}"
