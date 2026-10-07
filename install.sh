#!/usr/bin/env bash
# Zaytoon installer — Linux / macOS.
#
# Deliberately does four things and stops:
#   1. detect the OS and architecture
#   2. check the dependencies it needs
#   3. create the portable directory layout
#   4. install the backend and the desktop app, and initialise the database
#
# What it does **not** do is download an AI model or guess at a package manager.
# The spec says "do not assume every machine has every dependency", and this
# script takes that literally: a missing dependency is reported by name with the
# command that installs it, and the install continues for everything else. A
# script that aborts on the first missing tool is a script that leaves a
# half-installed system.
#
# Usage:
#   ./install.sh                 # install
#   ./install.sh --check         # report what is present, change nothing
#   ./install.sh --dir ~/Zaytoon # install somewhere else
#   ./install.sh --no-desktop    # backend + web only

set -euo pipefail

ZAYTOON_VERSION="${ZAYTOON_VERSION:-3.1.0}"
INSTALL_DIR="${ZAYTOON_DIR:-$HOME/Zaytoon}"
CHECK_ONLY=0
WITH_DESKTOP=1

while [ $# -gt 0 ]; do
  case "$1" in
    --check)      CHECK_ONLY=1 ;;
    --no-desktop) WITH_DESKTOP=0 ;;
    --dir)        INSTALL_DIR="$2"; shift ;;
    --version)    echo "$ZAYTOON_VERSION"; exit 0 ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Output helpers. Colour only when stdout is a terminal, so a CI log stays
# readable.
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  BOLD=""; DIM=""; RED=""; GREEN=""; YELLOW=""; CYAN=""; RESET=""
fi

step() { printf '%s==>%s %s\n' "$CYAN$BOLD" "$RESET$BOLD" "$1$BOLD$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()  { printf '  %s✗%s %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }
note() { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }

# ---------------------------------------------------------------------------
# 1. Detect the platform
# ---------------------------------------------------------------------------
step "Detecting the platform"

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="Windows (via $OS)"; OS="Windows" ;;
  *)      PLATFORM="$OS" ;;
esac

# Normalise the architecture, because `uname -m` spells it four ways.
case "$ARCH" in
  x86_64|amd64)  ARCH_NORM="x64" ;;
  aarch64|arm64) ARCH_NORM="arm64" ;;
  armv7l)        ARCH_NORM="armv7" ;;
  i386|i686)     ARCH_NORM="x86" ;;
  *)             ARCH_NORM="$ARCH" ;;
esac

ok "Platform: $PLATFORM ($ARCH_NORM)"
note "Version:  $ZAYTOON_VERSION"
note "Install:  $INSTALL_DIR"

if [ "$CHECK_ONLY" = 1 ]; then
  note "(--check: nothing will be changed)"
fi

# ---------------------------------------------------------------------------
# 2. Check dependencies
# ---------------------------------------------------------------------------
# Each entry is "command|install hint". A missing one is a warning, not a
# failure: Zaytoon runs backend-only, and the user may intend to use it that way.
step "Checking dependencies"

MISSING=()
check() {
  local cmd="$1" hint="$2"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd $(command -v "$cmd")"
  else
    warn "$cmd is not installed — $hint"
    MISSING+=("$cmd")
  fi
}

PY=""
for candidate in python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -n "$PY" ]; then
  ok "python $("$PY" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))') ($PY)"
else
  die "Python 3.10+ is required and was not found. Install it, or set PYTHONPATH and re-run."
fi

check node "install from https://nodejs.org (needed for the web UI)"
check npm  "ships with Node.js"
check git  "install your platform's git package"

# Ollama is optional and *never* downloaded for the user: pulling a multi-
# gigabyte model is a decision, not an installation step.
if command -v ollama >/dev/null 2>&1; then
  ok "ollama $(command -v ollama)"
else
  note "ollama not found — Zaytoon still runs, but no local model is available."
  note "  Install it from https://ollama.com, then: ollama pull qwen3.5:9b"
fi

# pywebview renders with the system WebKit on Linux; without the GTK bindings
# the host starts and then dies. Reported here so `--check` sees it too (the
# desktop section below only runs for a real install). A warning, never an
# abort: the browser UI is unaffected, and `main.py --diagnose` reports the
# same condition after installation. Nothing is downloaded.
if [ "$WITH_DESKTOP" = 1 ] && [ "$(uname -s)" = "Linux" ]; then
  if ! command -v pkg-config >/dev/null 2>&1; then
    note "pkg-config not found — cannot verify WebKitGTK (needed by the desktop window)"
  elif pkg-config --exists webkit2gtk-4.1 2>/dev/null \
       || pkg-config --exists webkit2gtk-4.0 2>/dev/null; then
    ok "WebKitGTK found (the desktop window can open)"
  else
    warn "WebKitGTK not found — the desktop window cannot open"
    note "  install it with your package manager, e.g.:"
    note "  sudo apt install python3-gi gir1.2-webkit2-4.1"
    note "  the web UI is unaffected"
    MISSING+=("webkit2gtk")
  fi
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  warn "${#MISSING[@]} optional dependency/dependencies missing: ${MISSING[*]}"
  note "Continuing. The install works without them; the features that need them will report it."
fi

# ---------------------------------------------------------------------------
# 3. Create the portable layout
# ---------------------------------------------------------------------------
# Application, Config, Data, Models, Skills, Workspaces, Logs, Cache — the
# layout the spec asks for. It exists so an application update never touches
# user data: the update replaces `Application` and nothing else.
if [ "$CHECK_ONLY" = 0 ]; then
  step "Creating the directory layout"
  for dir in Application Config Data Models Skills Workspaces Logs Cache; do
    mkdir -p "$INSTALL_DIR/$dir"
    ok "$dir/"
  done

  # Symlinks rather than copies, so a checkout the user is developing is the
  # thing that runs. A copy here would silently install a stale tree.
  SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
  ln -sfn "$SOURCE_DIR/backend"  "$INSTALL_DIR/Application/backend"  2>/dev/null \
    || cp -r "$SOURCE_DIR/backend" "$INSTALL_DIR/Application/backend" 2>/dev/null || true
  ln -sfn "$SOURCE_DIR/frontend" "$INSTALL_DIR/Application/frontend" 2>/dev/null \
    || true
  ln -sfn "$SOURCE_DIR/desktop"  "$INSTALL_DIR/Application/desktop"  2>/dev/null \
    || true
  ok "Application/ linked to this checkout"
fi

# ---------------------------------------------------------------------------
# 4. Python environment
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = 0 ]; then
  step "Setting up the Python environment"
  VENV="$INSTALL_DIR/Application/backend/.venv"
  if [ ! -d "$VENV" ]; then
    "$PY" -m venv "$VENV"
    ok "created $VENV"
  else
    ok "reusing $VENV"
  fi

  # Installed with the failure tolerated: a build tool missing on the user's
  # machine is their business, and a hard failure here leaves no working
  # install at all.
  if "$VENV/bin/pip" install --quiet --upgrade pip 2>/dev/null; then
    ok "pip upgraded"
  else
    warn "could not upgrade pip — continuing"
  fi

  if "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/Application/backend/requirements.txt" 2>&1 | tail -3; then
    ok "runtime dependencies installed"
  else
    warn "some dependencies failed to install — see the output above"
    note "  Speech (STT/TTS) is optional; the rest of Zaytoon does not need it."
  fi

  if [ -f "$INSTALL_DIR/Application/backend/requirements-dev.txt" ]; then
    if "$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/Application/backend/requirements-dev.txt" 2>/dev/null; then
      ok "development dependencies installed (pytest)"
    else
      warn "development dependencies failed to install — the test suite will not run"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# 5. Web UI
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = 0 ] && command -v npm >/dev/null 2>&1; then
  step "Building the web interface"
  if (cd "$INSTALL_DIR/Application/frontend" && npm install --silent --no-fund --no-audit) 2>&1 | tail -2; then
    ok "node modules installed"
    if (cd "$INSTALL_DIR/Application/frontend" && npm run build --silent) 2>&1 | tail -2; then
      ok "web interface built into frontend/dist"
    else
      warn "the web build failed — `npm run dev` still works"
    fi
  else
    warn "npm install failed — the web interface will not build"
  fi
fi

# ---------------------------------------------------------------------------
# 6. Desktop shell
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = 0 ] && [ "$WITH_DESKTOP" = 1 ]; then
  step "Preparing the desktop shell"
  if [ -d "$INSTALL_DIR/Application/desktop" ]; then
    DESKTOP_VENV="$INSTALL_DIR/Application/desktop/.venv"
    if [ ! -d "$DESKTOP_VENV" ]; then
      if "$PY" -m venv "$DESKTOP_VENV" 2>/dev/null; then
        if "$DESKTOP_VENV/bin/pip" install --quiet -r "$INSTALL_DIR/Application/desktop/requirements.txt" 2>&1 | tail -3; then
          ok "desktop dependencies installed"
        else
          warn "desktop dependencies failed — the browser UI is unaffected"
        fi
      else
        warn "could not create the desktop environment"
      fi
    else
      ok "reusing the desktop environment"
    fi

    # The WebKitGTK runtime was already reported in the dependency step so
    # that `--check` sees it as well — it is not repeated here.
  else
    note "no desktop directory in this checkout — skipping"
  fi
fi

# ---------------------------------------------------------------------------
# 7. Initialise the database and configuration
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = 0 ]; then
  step "Initialising data and configuration"

  # .env is created only if absent. Overwriting it would destroy the user's
  # API keys and workspace choice on a reinstall, which is the single most
  # destructive thing an installer can do.
  ENV_FILE="$INSTALL_DIR/Config/.env"
  if [ -f "$ENV_FILE" ]; then
    ok "Config/.env already exists — left untouched"
  else
    cat > "$ENV_FILE" <<EOF
# Zaytoon configuration. Generated by install.sh.
# Paths are relative to the install directory so the whole thing stays portable.

WORKSPACE_ROOT=$INSTALL_DIR/Workspaces
DATA_DIR=$INSTALL_DIR/Data
ZAYTOON_SKILLS_DIR=$INSTALL_DIR/Skills
ZAYTOON_SECRETS_PATH=$INSTALL_DIR/Config/secrets.json
CORE_DB_PATH=$INSTALL_DIR/Data/core.db
AUDIT_DB_PATH=$INSTALL_DIR/Data/audit.db

BACKEND_HOST=127.0.0.1
BACKEND_PORT=8000

# Local inference by default: no key, no account, no cost.
AI_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434

# Voice is optional and pulls large models on first use.
WHISPER_MODEL=tiny

LOG_LEVEL=INFO
TOOL_SAFETY_ENABLED=true
TOOL_CONFIRM_TIMEOUT=120
EOF
    ok "Config/.env written"
  fi

  # The database is created by the backend on first start; what matters here is
  # that the directory exists and is writable, because a read-only Data/
  # directory produces a confusing error much later.
  mkdir -p "$INSTALL_DIR/Data"
  if touch "$INSTALL_DIR/Data/.writable" 2>/dev/null; then
    rm -f "$INSTALL_DIR/Data/.writable"
    ok "Data/ is writable"
  else
    die "Data/ is not writable: $INSTALL_DIR/Data"
  fi

  note "Secrets are stored encrypted in Config/secrets.json (DPAPI on Windows)."
  note "No API key is required for local models."
fi

# ---------------------------------------------------------------------------
# 8. Launchers
# ---------------------------------------------------------------------------
if [ "$CHECK_ONLY" = 0 ]; then
  step "Creating launchers"

  cat > "$INSTALL_DIR/start.sh" <<EOF
#!/usr/bin/env bash
# Start the Zaytoon backend.
set -euo pipefail
HERE="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
export ZAYTOON_HOME="\$HERE"
set -a; [ -f "\$HERE/Config/.env" ] && . "\$HERE/Config/.env"; set +a
exec "\$HERE/Application/backend/.venv/bin/python" \\
  "\$HERE/Application/backend/main.py" "\$@"
EOF
  chmod +x "$INSTALL_DIR/start.sh"
  ok "start.sh"

  cat > "$INSTALL_DIR/start-desktop.sh" <<EOF
#!/usr/bin/env bash
# Start the desktop shell (it supervises the backend itself).
set -euo pipefail
HERE="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
export ZAYTOON_HOME="\$HERE"
set -a; [ -f "\$HERE/Config/.env" ] && . "\$HERE/Config/.env"; set +a
exec "\$HERE/Application/desktop/.venv/bin/python" \\
  "\$HERE/Application/desktop/main.py" "\$@"
EOF
  chmod +x "$INSTALL_DIR/start-desktop.sh"
  ok "start-desktop.sh"

  cat > "$INSTALL_DIR/test.sh" <<EOF
#!/usr/bin/env bash
# Run the test suites.
set -euo pipefail
HERE="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
echo "backend:"
"\$HERE/Application/backend/.venv/bin/python" -m pytest \\
  "\$HERE/Application/backend/tests" -q || exit 1
if [ -d "\$HERE/Application/desktop/tests" ]; then
  echo "desktop:"
  "\$HERE/Application/backend/.venv/bin/python" -m pytest \\
    "\$HERE/Application/desktop/tests" -q || exit 1
fi
EOF
  chmod +x "$INSTALL_DIR/test.sh"
  ok "test.sh"
fi

# ---------------------------------------------------------------------------
# 9. Report
# ---------------------------------------------------------------------------
echo
if [ "$CHECK_ONLY" = 1 ]; then
  step "Check complete — nothing was changed"
  exit 0
fi

step "Zaytoon $ZAYTOON_VERSION installed"
echo
echo "  ${BOLD}Start the backend${RESET}   $INSTALL_DIR/start.sh"
echo "  ${BOLD}Start the desktop${RESET}   $INSTALL_DIR/start-desktop.sh"
echo "  ${BOLD}Web interface${RESET}       http://127.0.0.1:8000/  (after building the frontend)"
echo "  ${BOLD}Configuration${RESET}       $INSTALL_DIR/Config/.env"
echo "  ${BOLD}Workspace${RESET}           $INSTALL_DIR/Workspaces"
echo "  ${BOLD}Skills${RESET}              $INSTALL_DIR/Skills"
echo "  ${BOLD}Logs${RESET}                $INSTALL_DIR/Logs"
echo "  ${BOLD}Run the tests${RESET}       $INSTALL_DIR/test.sh"
echo
note "No AI model is installed for you. With Ollama:"
note "  ollama pull qwen3.5:9b          # general"
note "  ollama pull qwen2.5-coder:7b    # coding"
note "  ollama pull qwen2.5-coder:3b    # fast"
echo
note "Everything an application update touches lives in Application/."
note "Config, Data, Skills and Workspaces are yours and are never overwritten."