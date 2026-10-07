<#
.SYNOPSIS
    Zaytoon installer — Windows.

.DESCRIPTION
    The PowerShell counterpart of install.sh. It does five things:

      1. detect the OS and architecture
      2. check the dependencies it needs
      3. create the portable directory layout
      4. install the backend and the desktop app
      5. create launchers and initialise configuration

    It deliberately does not download an AI model and does not guess at a
    package manager. A missing dependency is reported by name with the command
    that installs it, and the install continues for everything else — an
    installer that aborts on the first missing tool leaves a half-installed
    system.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Check
    .\install.ps1 -Directory C:\Zaytoon -NoDesktop
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoDesktop,
    [string]$Directory = (Join-Path $HOME "Zaytoon"),
    [string]$Version = "3.1.0"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------
# Output helpers. Colour only when the host can show it.
# ---------------------------------------------------------------------------
$useColor = $Host.UI.SupportsVirtualTerminal -or $env:NO_COLOR -eq $null
if ($useColor) {
    $script:Cyan = "`e[36m"; $script:Green = "`e[32m"
    $script:Yellow = "`e[33m"; $script:Red = "`e[31m"
    $script:Dim = "`e[2m"; $script:Reset = "`e[0m"
} else {
    $script:Cyan = $script:Green = $script:Yellow = $script:Red = ""
    $script:Dim = $script:Reset = ""
}

function Step { param($m) Write-Host "==> $m" -ForegroundColor Cyan }
function Ok   { param($m) Write-Host "  $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "  ! $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "  x $m" -ForegroundColor Red; exit 1 }
function Note { param($m) Write-Host "  $m" -ForegroundColor DarkGray }

# ---------------------------------------------------------------------------
# 1. Detect the platform
# ---------------------------------------------------------------------------
Step "Detecting the platform"

$arch = switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    "X64"   { "x64" }
    "Arm64" { "arm64" }
    "X86"   { "x86" }
    default { "unknown" }
}
$osVersion = [System.Environment]::OSVersion.VersionString
$isWindows11 = $PSVersionTable.PSVersion.Major -ge 6

Ok "Platform: Windows $osVersion ($arch)"
Note "PowerShell: $($PSVersionTable.PSVersion) (Desktop $($PSVersionTable.PSEdition))"
Note "Version:   $Version"
Note "Install:   $Directory"

if ($Check) { Note "(-Check: nothing will be changed)" }

# ---------------------------------------------------------------------------
# 2. Check dependencies
# ---------------------------------------------------------------------------
Step "Checking dependencies"
$missing = @()

function Find-Command($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

# WebView2 renders the desktop window. Detection mirrors desktop/app/host.py:
# the EdgeUpdate client key (pv > 0.0.0.0), then the fixed install path for
# machines whose registry entry is gone. Returns a version string or $null.
# The installer cannot fix a missing runtime - it is a separate Microsoft
# download - so callers warn with the URL rather than abort.
function Get-WebView2Version {
    foreach ($key in @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    )) {
        $prop = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
        if ($prop -and $prop.pv -and $prop.pv -ne "0.0.0.0") { return "$($prop.pv)" }
    }
    # Fixed-path installs leave the runtime on disk with no registry key.
    foreach ($dir in @(
        "${env:ProgramFiles(x86)}\Microsoft\EdgeWebView\Application",
        "$env:ProgramFiles\Microsoft\EdgeWebView\Application"
    )) {
        if ($dir -and (Test-Path $dir) -and @(Get-ChildItem $dir -ErrorAction SilentlyContinue).Count -gt 0) {
            return "present (fixed path)"
        }
    }
    return $null
}

# Python: 3.10 is the floor (the code uses `X | Y` type unions at runtime).
$python = $null
foreach ($candidate in @("python", "python3", "py")) {
    $path = Find-Command $candidate
    if (-not $path) { continue }
    try {
        $version = & $path -c "import sys; print(sys.version_info[0]*100 + sys.version_info[1])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [int]$version -ge 310) { $python = $path; break }
    } catch { continue }
}

if ($python) {
    $ver = & $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
    Ok "python $ver ($python)"
} else {
    Die "Python 3.10+ is required and was not found. Install it from https://python.org (tick 'Add to PATH')."
}

$node = Find-Command "node"
if ($node) { Ok "node $((& node --version)) ($node)" } else {
    Warn "node is not installed - needed for the web UI (https://nodejs.org)"
    $missing += "node"
}
$npm = Find-Command "npm"
if ($npm) { Ok "npm ($npm)" } else {
    Warn "npm is not installed - it ships with Node.js"
    $missing += "npm"
}

# Ollama is optional and never downloaded for the user: pulling a
# multi-gigabyte model is a decision, not an installation step.
$ollama = Find-Command "ollama"
if ($ollama) {
    Ok "ollama ($ollama)"
} else {
    Note "ollama not found - Zaytoon runs, but no local model is available."
    Note "  Install from https://ollama.com, then: ollama pull qwen3.5:9b"
}

# WebView2 (desktop window only - reported here so -Check sees it too;
# `desktop\main.py --diagnose` re-reports the same condition later).
if (-not $NoDesktop) {
    $wv2 = Get-WebView2Version
    if ($wv2) {
        Ok "WebView2 runtime ($wv2)"
    } else {
        Warn "WebView2 runtime missing - the desktop window cannot open"
        Note "  install it once: https://developer.microsoft.com/microsoft-edge/webview2/"
        Note "  the web UI is unaffected; re-check with: install.ps1 -Check"
        $missing += "webview2"
    }
}

if ($missing.Count -gt 0) {
    Warn "$($missing.Count) optional dependency/dependencies missing: $($missing -join ', ')"
    Note "Continuing. The install works without them; features that need them will report it."
}

# ---------------------------------------------------------------------------
# 3. The portable layout
# ---------------------------------------------------------------------------
# Application, Config, Data, Models, Skills, Workspaces, Logs, Cache. It exists
# so an application update replaces Application/ and nothing else: your
# configuration, data, skills and workspaces are never touched by an update.
if (-not $Check) {
    Step "Creating the directory layout"
    foreach ($dir in @("Application", "Config", "Data", "Models", "Skills", "Workspaces", "Logs", "Cache")) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Directory $dir) | Out-Null
        Ok "$dir/"
    }

    # Junctions, not copies: this is a development checkout and the user is
    # editing it. A copy would install a stale tree and look like a mystery bug.
    $source = Split-Path -Parent $MyInvocation.MyCommand.Path
    foreach ($name in @("backend", "frontend", "desktop")) {
        $from = Join-Path $source $name
        if (-not (Test-Path $from)) { continue }
        $to = Join-Path $Directory "Application\$name"
        if (Test-Path $to) { continue }
        try {
            New-Item -ItemType Junction -Path $to -Target $from -ErrorAction Stop | Out-Null
            Ok "Application/$name -> this checkout"
        } catch {
            Copy-Item -Recurse -Force $from $to
            Ok "Application/$name (copied)"
        }
    }
}

# ---------------------------------------------------------------------------
# 4. Python environment
# ---------------------------------------------------------------------------
if (-not $Check) {
    Step "Setting up the Python environment"
    $venv = Join-Path $Directory "Application\backend\.venv"

    if (-not (Test-Path $venv)) {
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { Die "Could not create the virtual environment at $venv" }
        Ok "created .venv"
    } else {
        Ok "reusing .venv"
    }

    $venvPy = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $venvPy)) { $venvPy = Join-Path $venv "bin\python" }

    & $venvPy -m pip install --quiet --upgrade pip 2>$null
    if ($LASTEXITCODE -eq 0) { Ok "pip upgraded" } else { Warn "could not upgrade pip - continuing" }

    $req = Join-Path $Directory "Application\backend\requirements.txt"
    & $venvPy -m pip install --quiet -r $req
    if ($LASTEXITCODE -eq 0) {
        Ok "runtime dependencies installed"
    } else {
        Warn "some dependencies failed to install - see the output above"
        Note "  Speech (STT/TTS) is optional; the rest of Zaytoon does not need it."
    }

    $reqDev = Join-Path $Directory "Application\backend\requirements-dev.txt"
    if (Test-Path $reqDev) {
        & $venvPy -m pip install --quiet -r $reqDev
        if ($LASTEXITCODE -eq 0) { Ok "development dependencies installed (pytest)" }
        else { Warn "development dependencies failed - the test suite will not run" }
    }
}

# ---------------------------------------------------------------------------
# 5. Web interface
# ---------------------------------------------------------------------------
if (-not $Check -and $npm) {
    Step "Building the web interface"
    Push-Location (Join-Path $Directory "Application\frontend")
    try {
        & npm install --silent --no-fund --no-audit
        if ($LASTEXITCODE -eq 0) {
            Ok "node modules installed"
            & npm run build --silent
            if ($LASTEXITCODE -eq 0) { Ok "web interface built into frontend/dist" }
            else { Warn "the web build failed - 'npm run dev' still works" }
        } else {
            Warn "npm install failed - the web interface will not build"
        }
    } finally { Pop-Location }
}

# ---------------------------------------------------------------------------
# 6. Desktop shell
# ---------------------------------------------------------------------------
if (-not $Check -and -not $NoDesktop) {
    Step "Preparing the desktop shell"
    $desktop = Join-Path $Directory "Application\desktop"
    if (Test-Path $desktop) {
        $dv = Join-Path $desktop ".venv"
        if (-not (Test-Path $dv)) {
            & $python -m venv $dv
            if ($LASTEXITCODE -eq 0) {
                $dpy = Join-Path $dv "Scripts\python.exe"
                & $dpy -m pip install --quiet -r (Join-Path $desktop "requirements.txt")
                if ($LASTEXITCODE -eq 0) { Ok "desktop dependencies installed" }
                else { Warn "desktop dependencies failed - the browser UI is unaffected" }
            } else {
                Warn "could not create the desktop environment"
            }
        } else {
            Ok "reusing the desktop environment"
        }

        # The WebView2 runtime was already reported in the dependency step
        # (Get-WebView2Version) so that -Check sees it as well - it is not
        # repeated here to avoid printing the same warning twice.
    } else {
        Note "no desktop directory in this checkout - skipping"
    }
}

# ---------------------------------------------------------------------------
# 7. Data and configuration
# ---------------------------------------------------------------------------
if (-not $Check) {
    Step "Initialising data and configuration"

    # Created only if absent. Overwriting it would destroy the user's API keys
    # and workspace choice on a reinstall - the single most destructive thing an
    # installer can do.
    $envFile = Join-Path $Directory "Config\.env"
    if (Test-Path $envFile) {
        Ok "Config\.env already exists - left untouched"
    } else {
        $content = @"
# Zaytoon configuration. Generated by install.ps1.

WORKSPACE_ROOT=$Directory\Workspaces
ZAYTOON_SKILLS_DIR=$Directory\Skills
ZAYTOON_SECRETS_PATH=$Directory\Config\secrets.json
CORE_DB_PATH=$Directory\Data\core.db
AUDIT_DB_PATH=$Directory\Data\audit.db

BACKEND_HOST=127.0.0.1
BACKEND_PORT=8000

# Local inference by default: no key, no account, no cost.
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434

WHISPER_MODEL=tiny

LOG_LEVEL=INFO
TOOL_SAFETY_ENABLED=true
TOOL_CONFIRM_TIMEOUT=120
"@
        Set-Content -Path $envFile -Value $content -Encoding UTF8
        Ok "Config\.env written"
    }

    $dataDir = Join-Path $Directory "Data"
    $probe = Join-Path $dataDir ".writable"
    try {
        New-Item -ItemType File -Force -Path $probe | Out-Null
        Remove-Item -Force $probe
        Ok "Data\ is writable"
    } catch {
        Die "Data\ is not writable: $dataDir"
    }

    # DPAPI-backed on Windows, so the file is only readable by this user.
    Note "Secrets are stored encrypted in Config\secrets.json (Windows DPAPI)."
    Note "No API key is required for local models."
}

# ---------------------------------------------------------------------------
# 8. Launchers and shortcuts
# ---------------------------------------------------------------------------
if (-not $Check) {
    Step "Creating launchers"

    $cmdPy = Join-Path $Directory "Application\backend\.venv\Scripts\python.exe"
    $startCmd = Join-Path $Directory "start.cmd"
    @"
@echo off
REM Start the Zaytoon backend.
setlocal
set ZAYTOON_HOME=%~dp0
if exist "%~dp0Config\.env" call "%~dp0Config\.env"
cd /d "%~dp0Application\backend"
"%~dp0$cmdPy" main.py %*
"@ | Set-Content -Path $startCmd -Encoding ASCII
    Ok "start.cmd"

    $deskPy = Join-Path $Directory "Application\desktop\.venv\Scripts\python.exe"
    $deskCmd = Join-Path $Directory "start-desktop.cmd"
    @"
@echo off
REM Start the desktop shell (it supervises the backend itself).
setlocal
set ZAYTOON_HOME=%~dp0
if exist "%~dp0Config\.env" call "%~dp0Config\.env"
cd /d "%~dp0Application\desktop"
"%~dp0$deskPy" main.py %*
"@ | Set-Content -Path $deskCmd -Encoding ASCII
    Ok "start-desktop.cmd"

    $testCmd = Join-Path $Directory "test.cmd"
    @"
@echo off
REM Run the test suites.
setlocal
"%~dp0$cmdPy" -m pytest "%~dp0Application\backend\tests" -q || exit /b 1
if exist "%~dp0Application\desktop\tests" (
  "%~dp0$cmdPy" -m pytest "%~dp0Application\desktop\tests" -q || exit /b 1
)
"@ | Set-Content -Path $testCmd -Encoding ASCII
    Ok "test.cmd"

    # A desktop shortcut is the difference between "installed" and "found".
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut(
            (Join-Path ([Environment]::GetFolderPath("Desktop")) "Zaytoon.cmd"))
        $link.TargetPath = $startCmd
        $link.WorkingDirectory = $Directory
        $link.Description = "Zaytoon AI workspace"
        $link.Save()
        Ok "desktop shortcut created"
    } catch {
        Note "could not create a desktop shortcut (permission?); use start.cmd"
    }
}

# ---------------------------------------------------------------------------
# 9. Report
# ---------------------------------------------------------------------------
Write-Host ""
if ($Check) { Step "Check complete - nothing was changed"; exit 0 }

Step "Zaytoon $Version installed"
Write-Host ""
Write-Host "  Start the backend    $Directory\start.cmd"
Write-Host "  Start the desktop    $Directory\start-desktop.cmd"
Write-Host "  Run the tests        $Directory\test.cmd"
Write-Host "  Configuration        $Directory\Config\.env"
Write-Host "  Workspace            $Directory\Workspaces"
Write-Host "  Skills               $Directory\Skills"
Write-Host "  Logs                 $Directory\Logs"
Write-Host ""
Note "No AI model is installed for you. With Ollama:"
Note "  ollama pull qwen3.5:9b          # general"
Note "  ollama pull qwen2.5-coder:7b    # coding"
Note "  ollama pull qwen2.5-coder:3b    # fast"
Write-Host ""
Note "Everything an application update touches lives in Application\."
Note "Config, Data, Skills and Workspaces are yours and are never overwritten."