#!/usr/bin/env bash
set -eu
root="$(cd "$(dirname "$0")/.." && pwd -P)"
test "$#" = 1 && test -x "$1"
exec bwrap --unshare-user --die-with-parent --clearenv --setenv PATH /usr/bin --setenv HOME /tmp \
  --setenv PYTHONDONTWRITEBYTECODE 1 --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --dev /dev --tmpfs /tmp --ro-bind "$root" /work --bind "$root/evidence" /work/evidence \
  --ro-bind "$1" /abc --chdir /work /usr/bin/python3 -m mdai.w3batch --abc /abc
