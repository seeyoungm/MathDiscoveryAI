#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname "$0")/.." && pwd -P)"
test "$#" = 3
abc="$(realpath -- "$1")"
cohort_sha="$2"
prereg_sha="$3"
test -f "$abc" && test -x "$abc"
[[ "$cohort_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$prereg_sha" =~ ^[0-9a-f]{64}$ ]]
test ! -e "$root/evidence/nextstage_synthesis"
stage="$root/evidence/.nextstage_synthesis_stage"
mkdir -- "$stage"
set +e
bwrap --unshare-user --die-with-parent --clearenv --setenv PATH /usr/bin --setenv HOME /tmp \
  --setenv PYTHONDONTWRITEBYTECODE 1 --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --dev /dev --tmpfs /tmp --dir /work --ro-bind "$root/mdai" /work/mdai --ro-bind "$root/tools" /work/tools \
  --bind "$stage" /work/evidence --ro-bind "$root/evidence/nextstage_preregister" /work/evidence/nextstage_preregister \
  --ro-bind "$abc" /abc --chdir /work /usr/bin/python3 \
  -m mdai.nextstage_synthesis --abc /abc --cohort-sha256 "$cohort_sha" --prereg-sha256 "$prereg_sha"
status="$?"
set -e
if [ -d "$stage/nextstage_synthesis" ]; then
  test ! -e "$root/evidence/nextstage_synthesis"
  mv -T -- "$stage/nextstage_synthesis" "$root/evidence/nextstage_synthesis"
fi
exit "$status"
