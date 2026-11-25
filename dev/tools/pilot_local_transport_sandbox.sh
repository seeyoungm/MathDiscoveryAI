#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname "$0")/.." && pwd -P)"
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
umask 077
filter="$root/tools/synthesis_no_network.py"
echo "2948da397557489c74047a0448d5bb8ffd03ddf0dc6f2895fc61c6932161d30f  $filter" | sha256sum -c --quiet
/usr/bin/python3 -I -B "$filter" > "$stage/deny_net.bpf"
exec 3< "$stage/deny_net.bpf"
rm -- "$stage/deny_net.bpf"
args=(--unshare-user --seccomp 3 --die-with-parent --clearenv --setenv PATH /usr/bin --setenv HOME /tmp
  --setenv PYTHONDONTWRITEBYTECODE 1 --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64
  --dev /dev --tmpfs /tmp --dir /proj --dir /proj/dev --dir /proj/records --dir /proj/records/week2
  --dir /proj/records/week2/week2_smoke --ro-bind "$root/mdai" /proj/dev/mdai --ro-bind "$root/tools" /proj/dev/tools
  --ro-bind "$root/tests" /proj/dev/tests
  --bind "$stage" /proj/dev/evidence --dir /proj/dev/evidence/week3
  --ro-bind "$root/evidence/week3/model_pilot" /proj/dev/evidence/week3/model_pilot
  --ro-bind "$root/../records/week2/week2_smoke/dataset_manifest.jsonl" /proj/records/week2/week2_smoke/dataset_manifest.jsonl
  --chdir /proj/dev /usr/bin/python3)
if [[ "${1:-}" == "--test" && "$#" == 1 ]]; then
  bwrap "${args[@]}" -m unittest discover -s tests -p test_pilot_local_transport.py -v
elif [[ "$#" == 0 ]]; then
  test ! -e "$root/evidence/pilot_local_transport"
  bwrap "${args[@]}" -m mdai.pilot_local_transport --out evidence/pilot_local_transport
  test ! -e "$root/evidence/pilot_local_transport"
  mv -T -- "$stage/pilot_local_transport" "$root/evidence/pilot_local_transport"
else
  echo 'use no arguments for evidence or --test' >&2
  exit 2
fi
