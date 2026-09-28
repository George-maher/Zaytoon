<#
.SYNOPSIS
    Generate self-signed certificates for local HTTPS/WSS development

.DESCRIPTION
    Creates a self-signed certificate for localhost with SAN extensions
    suitable for Chrome/Edge/Firefox trust.
#>

$CertDir = Join-Path (Split-Path $PSScriptPath) "frontend\certs"
$CertFile = Join-Path $CertDir "localhost.crt"
$KeyFile = Join-Path $CertDir "localhost.key"

if (Test-Path $CertFile -and Test-Path $KeyFile) {
    Write-Host "Certificates already exist at $CertDir"
    exit 0
}

Write-Host "Generating self-signed certificate for localhost..."

# Create directory if not exists
if (-not (Test-Path $CertDir)) {
    New-Item -ItemType Directory -Path $CertDir | Out-Null
}

# Generate self-signed certificate with SAN
$Cert = New-SelfSignedCertificate `
    -DnsName "localhost", "127.0.0.1" `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -NotAfter (Get-Date).AddDays(365) `
    -KeyAlgorithm RSA `
    -KeyLength 2048 `
    -FriendlyName "Zaytoon Localhost Dev Cert"

# Export certificate and private key
$Pwd = ConvertTo-SecureString -String "zaytoon-dev" -Force -AsPlainText
$Cert | Export-PfxCertificate -FilePath "$CertDir\localhost.pfx" -Password $Pwd

# Export public cert only
$Cert | Export-Certificate -FilePath $CertFile -Type CERT

# Extract private key using OpenSSL (if available)
if (Get-Command openssl -ErrorAction SilentlyContinue) {
    openssl pkcs12 -in "$CertDir\localhost.pfx" -nocerts -nodes -out $KeyFile -passin pass:zaytoon-dev
} else {
    Write-Warning "OpenSSL not found. Private key saved in PFX. Install OpenSSL to extract .key file."
}

Write-Host "Certificates generated:"
Write-Host "  Cert: $CertFile"
Write-Host "  Key:  $KeyFile"
Write-Host "  PFX:  $CertDir\localhost.pfx"
Write-Host ""
Write-Host "To trust in Windows:"
Write-Host "  Import-Certificate -FilePath \"$CertFile\" -CertStoreLocation Cert:\LocalMachine\Root"