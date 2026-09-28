#!/usr/bin/env bash
# Generate self-signed certificates for local HTTPS/WSS development
# Usage: ./generate-certs.sh

set -e

CERT_DIR="$(dirname "$0")/frontend/certs"
mkdir -p "$CERT_DIR"

CERT_FILE="$CERT_DIR/localhost.crt"
KEY_FILE="$CERT_DIR/localhost.key"

if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
    echo "Certificates already exist at $CERT_DIR"
    exit 0
fi

echo "Generating self-signed certificate for localhost..."

# Generate private key
openssl genrsa -out "$KEY_FILE" 2048

# Generate self-signed certificate with SAN for localhost
openssl req -new -x509 -key "$KEY_FILE" -out "$CERT_FILE" -days 365 \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

# Set appropriate permissions
chmod 644 "$CERT_FILE"
chmod 600 "$KEY_FILE"

echo "Certificates generated:"
echo "  Cert: $CERT_FILE"
echo "  Key:  $KEY_FILE"
echo ""
echo "For browser trust, add $CERT_FILE to your system/browser trust store."
echo "On Windows: certutil -addstore -f \"ROOT\" \"$CERT_FILE\""
echo "On macOS:  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain \"$CERT_FILE\""
echo "On Linux:  sudo cp \"$CERT_FILE\" /usr/local/share/ca-certificates/ && sudo update-ca-certificates"