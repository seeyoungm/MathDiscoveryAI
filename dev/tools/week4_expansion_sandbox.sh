#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname "$0")/.." && pwd -P)"
test "$#" = 2
abc="$(realpath -- "$1")"
sha="$2"
test -f "$abc" && test -x "$abc"
[[ "$sha" =~ ^[0-9a-f]{64}$ ]]
source="$(realpath -- "$root/../records/week2/week2_smoke/dataset_manifest.jsonl")"
test ! -e "$root/evidence/week4_validation_expansion"
exec bwrap --unshare-user --die-with-parent --clearenv --setenv PATH /usr/bin --setenv HOME /tmp \
  --setenv PYTHONDONTWRITEBYTECODE 1 --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --dev /dev --tmpfs /tmp --ro-bind "$root" /work --bind "$root/evidence" /work/evidence \
  --ro-bind "$source" /source.jsonl --ro-bind "$abc" /abc --chdir /work /usr/bin/python3 \
  -m mdai.w4expand --source /source.jsonl --abc /abc --abc-sha256 "$sha"
