#!/bin/sh
# Self-signed TLS pair for `frontend/certs/`, referenced by the frontend
# Dockerfile (`COPY certs/ /etc/nginx/certs/`) and bind-mounted by
# docker-compose.yml. Before this script existed, the Dockerfile pointed at a
# file that was never in the repository, so `docker compose build frontend`
# failed with `COPY failed: folder not found` — the image could not be built at
# all, which is how this defect stayed invisible for as long as it did.
#
#   sh frontend/scripts/generate-certs.sh        # refuses to overwrite
#   FORCE=1 sh frontend/scripts/generate-certs.sh
#   DAYS=30 COMMON_NAME=localhost sh frontend/scripts/generate-certs.sh
#
# This is a development certificate. A browser will warn about it; that warning
# is the correct behaviour for a key that was generated on this machine by a
# script, and it is why Zaytoon's HTTPS surface exists for the microphone
# permission (getUserMedia requires a secure context) rather than for trust.
# Put a real certificate in this directory for anything else.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CERT_DIR=$(CDPATH= cd -- "$HERE/.." && pwd)/certs
COMMON_NAME=${COMMON_NAME:-localhost}
DAYS=${DAYS:-365}
CRT="$CERT_DIR/$COMMON_NAME.crt"
KEY="$CERT_DIR/$COMMON_NAME.key"

if ! command -v openssl >/dev/null 2>&1; then
    echo "generate-certs: openssl is required and was not on PATH." >&2
    exit 1
fi

mkdir -p "$CERT_DIR"

if [ -e "$CRT" ] || [ -e "$KEY" ]; then
    if [ "${FORCE:-0}" != "1" ]; then
        echo "generate-certs: $CERT_DIR already holds a certificate." >&2
        echo "generate-certs: re-run with FORCE=1 to replace it (nothing else reads it)." >&2
        exit 1
    fi
    rm -f "$CRT" "$KEY"
fi

# `subjectAltName` is not decoration: clients ignore CN and match the SAN list,
# so a certificate without it fails in every browser that matters here. The
# default name is already `localhost`, and a duplicated SAN entry — which is what
# a naive list produces — is the kind of detail a reviewer reads as carelessness.
SAN="DNS:localhost,IP:127.0.0.1,IP:::1"
if [ "$COMMON_NAME" != "localhost" ]; then
    SAN="DNS:$COMMON_NAME,$SAN"
fi

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$KEY" -out "$CRT" -days "$DAYS" \
    -subj "/CN=$COMMON_NAME/O=Zaytoon development only" \
    -addext "subjectAltName=$SAN"

# nginx reads the key as root, and the directory is a bind mount, so the mode is
# what keeps the key off the rest of the machine.
chmod 600 "$KEY"

echo "generate-certs: wrote $CRT"
echo "generate-certs: and $KEY (mode 600, valid for $DAYS days)"
echo "generate-certs: the compose file mounts this directory read-only;"
echo "generate-certs: rebuild once if the image was built before it existed."
