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
test ! -e "$root/evidence/milestone_synthesis"
stage="$root/evidence/.milestone_synthesis_stage"
mkdir -- "$stage"
umask 077
/usr/bin/python3 "$root/tools/synthesis_no_network.py" > "$stage/deny_net.bpf"
exec 3< "$stage/deny_net.bpf"
rm -- "$stage/deny_net.bpf"
set +e
bwrap --unshare-user --seccomp 3 --die-with-parent --clearenv --setenv PATH /usr/bin --setenv HOME /tmp \
  --setenv PYTHONDONTWRITEBYTECODE 1 --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --dev /dev --tmpfs /tmp --dir /work --ro-bind "$root/mdai" /work/mdai --ro-bind "$root/tools" /work/tools \
  --bind "$stage" /work/evidence --ro-bind "$root/evidence/milestone_preregister" /work/evidence/milestone_preregister \
  --ro-bind "$abc" /abc --chdir /work /usr/bin/python3 \
  -m mdai.milestone_synthesis --abc /abc --cohort-sha256 "$cohort_sha" --prereg-sha256 "$prereg_sha"
status="$?"
set -e
if [ -d "$stage/milestone_synthesis" ]; then
  test ! -e "$root/evidence/milestone_synthesis"
  mv -T -- "$stage/milestone_synthesis" "$root/evidence/milestone_synthesis"
fi
exit "$status"
