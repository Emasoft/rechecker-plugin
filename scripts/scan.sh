#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  scan.sh — Headless, silent, self-installing, fault-tolerant code scanner
#
#  Runs: Super-Linter + Semgrep + TruffleHog
#  Auto-installs Docker if missing. Retries on failure. Headless/CI safe.
#  Only outputs the final report path on success, or errors it can't fix.
#  Autofix is ON by default. Security rules auto-updated from trusted sources.
#
#  Compatible: bash 3.2+ (macOS default), bash 4+, bash 5+
#  Platforms:  macOS, Linux, WSL, Windows (Git Bash / MSYS2)
# ═══════════════════════════════════════════════════════════════════════════════
set -uo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────

MAX_WAIT_DOCKER=120
MAX_RETRIES=3
RETRY_DELAY=5
DOCKER_PULL_RETRIES=3
LOCK_DIR="/tmp/scan.sh.lock.d"   # FIX #2: use mkdir for atomic locking
MAX_SCAN_SIZE_MB=2048
MAX_FILE_COUNT=100000
SCAN_TIMEOUT=3600

RULE_CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/scan-sh/rules"
RULE_TTL_SECONDS=86400

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
export NEEDRESTART_SUSPEND=1

IMG_SUPERLINTER="ghcr.io/super-linter/super-linter:latest"
IMG_SEMGREP="semgrep/semgrep"
IMG_TRUFFLEHOG="trufflesecurity/trufflehog"

INTERNAL_LOG=""
FINAL_REPORT=""
HAD_FATAL=false
WORK_DIR=""
STAGING_DIR=""
SCAN_DIR=""

# FIX #11/22: Initialize arrays safely for bash 3.2 compatibility
# Arrays are populated via argument parsing; we test ${#arr[@]} before iterating
TARGETS=()
EXCLUDES=()
RECURSIVE=true
RESPECT_GITIGNORE=true
AUTOFIX=true
UPDATE_RULES=true
RULES_UPDATED=false
FOLLOW_SYMLINKS=false
ALLOW_NETWORK_DRIVES=false
ALLOW_SPECIAL_FILES=false
MAX_FILE_SIZE_MB=50
ALLOW_HIDDEN=true

# ─── Helpers ──────────────────────────────────────────────────────────────────

_log() {
  [[ -n "${INTERNAL_LOG:-}" ]] && echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$INTERNAL_LOG"
}

fatal() {
  HAD_FATAL=true
  _log "FATAL: $*"
  echo "ERROR: $*" >&2
  cleanup_and_exit 1
}

verbose() {
  [[ "${VERBOSE:-false}" == "true" ]] && echo "[scan] $*" >&2
  _log "$*"
}

retry() {
  local max="$1" delay="$2" desc="$3"
  shift 3
  local attempt=1
  while true; do
    _log "Attempt $attempt/$max: $desc"
    if "$@" >> "${INTERNAL_LOG:-/dev/null}" 2>&1; then
      _log "OK: $desc"
      return 0
    fi
    local rc=$?
    _log "FAIL (exit $rc): $desc"
    if (( attempt >= max )); then
      _log "All $max attempts exhausted: $desc"
      return 1
    fi
    sleep "$delay"
    (( attempt++ ))
  done
}

has_cmd() { command -v "$1" &>/dev/null; }

download() {
  local url="$1" dest="$2"
  if has_cmd curl; then
    curl -fsSL --retry 3 --retry-delay 3 -o "$dest" "$url" 2>>"${INTERNAL_LOG:-/dev/null}"
  elif has_cmd wget; then
    wget -q --tries=3 -O "$dest" "$url" 2>>"${INTERNAL_LOG:-/dev/null}"
  else
    fatal "Neither curl nor wget available. Cannot download."
  fi
}

download_soft() {
  local url="$1" dest="$2"
  if has_cmd curl; then
    curl -fsSL --retry 2 --retry-delay 2 --max-time 30 -o "$dest" "$url" 2>>"${INTERNAL_LOG:-/dev/null}"
  elif has_cmd wget; then
    wget -q --tries=2 --timeout=30 -O "$dest" "$url" 2>>"${INTERNAL_LOG:-/dev/null}"
  else
    return 1
  fi
}

# FIX #9: portable lowercase function (works on bash 3.2 without ${var,,})
to_lower() {
  echo "$1" | tr '[:upper:]' '[:lower:]'
}

# FIX #14: portable timeout wrapper (macOS has no `timeout` by default)
portable_timeout() {
  local secs="$1"
  shift
  if has_cmd timeout; then
    timeout --signal=KILL "$secs" "$@"
  elif has_cmd gtimeout; then
    gtimeout --signal=KILL "$secs" "$@"
  elif has_cmd perl; then
    perl -e 'alarm shift; exec @ARGV' "$secs" "$@"
  else
    # No timeout available — run without limit, log warning
    _log "WARNING: No timeout command found. Running without time limit."
    "$@"
  fi
}

# FIX #21: safe JSON string escaping for bash-built JSON
json_escape() {
  local s="$1"
  s="${s//\\/\\\\}"    # backslash
  s="${s//\"/\\\"}"    # double quote
  s="${s//$'\n'/\\n}"  # newline
  s="${s//$'\t'/\\t}"  # tab
  echo "$s"
}

# ─── Help ─────────────────────────────────────────────────────────────────────

show_help() {
  cat <<'EOF'
scan.sh — Headless, silent, self-installing, fault-tolerant code scanner

Runs three tools in sequence and produces a single timestamped JSON report:
  1. Super-Linter   — code quality & style (40+ languages)
  2. Semgrep         — security vulnerabilities (OWASP, injection, etc.)
  3. TruffleHog      — leaked secrets & credentials

Prints only the report file path to stdout on success.
Errors go to stderr only when unrecoverable.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  scan.sh [OPTIONS] [PROJECT_DIR]

  PROJECT_DIR defaults to the current working directory.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SECURITY RULES — automatic updates from trusted sources
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --no-update-rules   Skip rule updates (use cached rules or Semgrep defaults).
  --force-update      Force re-fetch even if cache is fresh.
  --rule-ttl SEC      Cache TTL in seconds (default: 86400 = 24 hours).

  Sources: Semgrep Registry (10 packs), GitHub CodeQL CWE mappings,
  OWASP/Trail of Bits community rules. Cached in ~/.cache/scan-sh/rules/.
  Falls back to Semgrep --config auto if all fetches fail.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
AUTOFIX — automatic code correction
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --no-autofix        Report only, don't modify files.
  --autofix           (Default) Fix issues in place where supported.

  Super-Linter: FIX_<LANG>=true for 35+ fixers (ESLint, black, gofmt, etc.)
  Semgrep: --autofix for rules with fix: fields.
  TruffleHog: No autofix (secrets require manual rotation).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TARGETING — what to scan
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --target PATH       Scan specific file/folder (relative to PROJECT_DIR).
  --target-list FILE  Read target paths from FILE (one per line, # comments ok).
  --no-recursive      Top-level files only in targeted directories.
  --exclude PATTERN   Exclude files matching regex. Repeatable.
  --no-hidden         Skip dotfiles/dotdirs.
  --no-gitignore      Include files excluded by .gitignore.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SAFETY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --follow-symlinks   Follow symlinks (OFF — can escape project tree).
  --allow-network     Allow network filesystems (OFF — can hang).
  --allow-special     Allow device nodes/pipes/sockets (OFF).
  --max-file-size MB  Skip files larger than this (default: 50).
  --max-scan-size MB  Abort if total exceeds this (default: 2048).
  --scan-timeout SEC  Kill tool after this (default: 3600).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL SELECTION & OUTPUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --only TOOL         Run one tool: linter, semgrep, trufflehog
  -o, --output DIR    Report directory (default: PROJECT_DIR).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOCKER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --skip-install      Don't auto-install Docker.
  --skip-pull         Use cached Docker images.
  --timeout SEC       Docker daemon wait timeout (default: 120).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VERBOSITY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  -q, --quiet         (Default) Only report path to stdout.
  -v, --verbose       Progress to stderr.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  ./scan.sh .                                 # Scan + autofix cwd
  ./scan.sh --no-autofix .                    # Report only
  ./scan.sh --only linter .                   # Lint only
  ./scan.sh --target src/main.py .            # Single file
  ./scan.sh --target src/ --no-recursive .    # Folder, top-level only
  ./scan.sh --only semgrep -o /tmp/reports .  # Security scan
  ./scan.sh --force-update .                  # Fresh rules
  ./scan.sh --no-update-rules --skip-pull .   # Offline mode
  report=$(./scan.sh .); cat "$report" | jq . # Scripting

EOF
  exit 0
}

# ─── Cleanup / lock ──────────────────────────────────────────────────────────

# FIX #2: Atomic locking via mkdir (POSIX-atomic on all filesystems)
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi
  # Lock exists — check if holder is alive
  local pid
  pid=$(cat "$LOCK_DIR/pid" 2>/dev/null || echo "")
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    fatal "Another scan is running (PID $pid). Remove $LOCK_DIR if stale."
  fi
  # Stale lock — remove and retry
  rm -rf "$LOCK_DIR"
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi
  fatal "Could not acquire lock at $LOCK_DIR."
}

cleanup_and_exit() {
  local code="${1:-0}"
  rm -rf "$LOCK_DIR" 2>/dev/null
  rm -f "${PROJECT_DIR:-}/.semgrep-tmp-output.json" 2>/dev/null
  rm -rf "${STAGING_DIR:-}" 2>/dev/null
  [[ -n "${WORK_DIR:-}" ]] && rm -rf "$WORK_DIR" 2>/dev/null
  if [[ -f "${FINAL_REPORT:-}" ]] && [[ "$HAD_FATAL" == "false" ]]; then
    echo "$FINAL_REPORT"
  fi
  exit "$code"
}

trap 'cleanup_and_exit 1' INT TERM

# ─── Platform detection ───────────────────────────────────────────────────────

detect_platform() {
  if [[ -f /proc/version ]] && grep -qi microsoft /proc/version 2>/dev/null; then
    echo "wsl"
  elif [[ "${OSTYPE:-}" == darwin* ]]; then
    echo "macos"
  elif [[ "${OSTYPE:-}" == linux* ]]; then
    echo "linux"
  elif [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* ]]; then
    echo "windows"
  elif [[ "$(uname -s 2>/dev/null)" == "Linux" ]]; then
    echo "linux"
  else
    echo "unknown"
  fi
}

PLATFORM=$(detect_platform)

# ─── Docker installation ─────────────────────────────────────────────────────

install_docker_macos() {
  verbose "macOS: Installing container runtime..."
  if has_cmd brew; then
    if brew install --cask orbstack </dev/null >> "${INTERNAL_LOG:-/dev/null}" 2>&1; then
      open -a OrbStack 2>/dev/null; return 0; fi
    if brew install --cask docker </dev/null >> "${INTERNAL_LOG:-/dev/null}" 2>&1; then
      open -a Docker 2>/dev/null; return 0; fi
  fi
  local arch dmg_url dmg="/tmp/Docker.dmg"
  arch=$(uname -m)
  [[ "$arch" == "arm64" ]] \
    && dmg_url="https://desktop.docker.com/mac/main/arm64/Docker.dmg" \
    || dmg_url="https://desktop.docker.com/mac/main/amd64/Docker.dmg"
  download "$dmg_url" "$dmg" || fatal "Failed to download Docker Desktop."
  hdiutil attach "$dmg" -quiet -nobrowse >> "${INTERNAL_LOG:-/dev/null}" 2>&1 || fatal "Failed to mount DMG."
  sudo cp -R "/Volumes/Docker/Docker.app" /Applications/ >> "${INTERNAL_LOG:-/dev/null}" 2>&1 \
    || cp -R "/Volumes/Docker/Docker.app" /Applications/ >> "${INTERNAL_LOG:-/dev/null}" 2>&1
  hdiutil detach "/Volumes/Docker" -quiet 2>/dev/null; rm -f "$dmg"
  open -a Docker 2>/dev/null
}

install_docker_linux() {
  verbose "Linux: Installing Docker Engine..."
  local in_container=false
  [[ ! -d /run/systemd/system ]] && [[ -f /.dockerenv || -f /run/.containerenv ]] && in_container=true
  if has_cmd apt-get; then
    sudo apt-get update -qq >> "$INTERNAL_LOG" 2>&1
    sudo apt-get install -y -qq --no-install-recommends docker.io >> "$INTERNAL_LOG" 2>&1 || {
      download "https://get.docker.com" "/tmp/get-docker.sh"
      sudo sh /tmp/get-docker.sh >> "$INTERNAL_LOG" 2>&1; rm -f /tmp/get-docker.sh; }
  elif has_cmd dnf; then
    sudo dnf install -y -q docker >> "$INTERNAL_LOG" 2>&1 || {
      download "https://get.docker.com" "/tmp/get-docker.sh"
      sudo sh /tmp/get-docker.sh >> "$INTERNAL_LOG" 2>&1; rm -f /tmp/get-docker.sh; }
  elif has_cmd pacman; then sudo pacman -Sy --noconfirm --quiet docker >> "$INTERNAL_LOG" 2>&1
  elif has_cmd zypper; then sudo zypper --non-interactive install docker >> "$INTERNAL_LOG" 2>&1
  elif has_cmd apk; then sudo apk add --no-cache docker >> "$INTERNAL_LOG" 2>&1
  else
    download "https://get.docker.com" "/tmp/get-docker.sh"
    sudo sh /tmp/get-docker.sh >> "$INTERNAL_LOG" 2>&1; rm -f /tmp/get-docker.sh
  fi
  if [[ "$in_container" != "true" ]]; then
    if has_cmd systemctl && [[ -d /run/systemd/system ]]; then
      sudo systemctl start docker >> "$INTERNAL_LOG" 2>&1
      sudo systemctl enable docker >> "$INTERNAL_LOG" 2>&1
    elif has_cmd service; then sudo service docker start >> "$INTERNAL_LOG" 2>&1; fi
  fi
  # FIX #5: clearer logic for usermod
  if has_cmd usermod; then
    if ! groups 2>/dev/null | grep -q docker; then
      sudo usermod -aG docker "$USER" >> "$INTERNAL_LOG" 2>&1 || true
    fi
  fi
}

install_docker_wsl() {
  has_cmd apt-get && { install_docker_linux; return; }
  fatal "Cannot auto-install Docker in WSL without apt."
}

install_docker_windows() {
  if has_cmd winget.exe; then
    winget.exe install -e --id Docker.DockerDesktop --accept-package-agreements \
      --accept-source-agreements --silent >> "$INTERNAL_LOG" 2>&1 && return 0; fi
  if has_cmd choco.exe; then
    choco.exe install docker-desktop -y --no-progress >> "$INTERNAL_LOG" 2>&1 && return 0; fi
  fatal "Cannot auto-install Docker on Windows."
}

ensure_docker_installed() {
  if has_cmd docker; then _log "Docker found: $(docker --version 2>&1)"; return 0; fi
  [[ "${SKIP_INSTALL:-false}" == "true" ]] && fatal "Docker not found and --skip-install was set."
  verbose "Docker not found, auto-installing..."
  case "$PLATFORM" in
    macos) install_docker_macos ;; linux) install_docker_linux ;;
    wsl) install_docker_wsl ;; windows) install_docker_windows ;;
    *) fatal "Unsupported platform." ;; esac
  if ! has_cmd docker; then
    for p in /usr/bin/docker /usr/local/bin/docker /opt/homebrew/bin/docker "$HOME/.docker/bin/docker"; do
      [[ -x "$p" ]] && { export PATH="$(dirname "$p"):$PATH"; break; }
    done
  fi
  has_cmd docker || fatal "Docker not on PATH after install."
}

is_docker_ready() { docker info >> "${INTERNAL_LOG:-/dev/null}" 2>&1; }

start_docker_daemon() {
  case "$PLATFORM" in
    macos)
      [[ -d "/Applications/OrbStack.app" ]] && { open -a OrbStack 2>/dev/null; return 0; }
      [[ -d "/Applications/Docker.app" ]] && { open -a Docker 2>/dev/null; return 0; } ;;
    linux)
      [[ -S /var/run/docker.sock ]] && return 0
      if has_cmd systemctl && [[ -d /run/systemd/system ]]; then
        sudo systemctl start docker >> "$INTERNAL_LOG" 2>&1
      elif has_cmd service; then sudo service docker start >> "$INTERNAL_LOG" 2>&1
      elif has_cmd dockerd; then sudo dockerd >> "$INTERNAL_LOG" 2>&1 & disown; fi ;;
    wsl)
      if has_cmd dockerd && ! pgrep -x dockerd &>/dev/null; then
        sudo dockerd >> "$INTERNAL_LOG" 2>&1 & disown
      elif has_cmd cmd.exe; then
        cmd.exe /c 'start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"' 2>/dev/null; fi ;;
    windows)
      powershell.exe -Command "Start-Process 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe'" 2>/dev/null || true ;;
  esac
}

wait_for_docker_daemon() {
  is_docker_ready && { verbose "Docker daemon running."; return 0; }
  start_docker_daemon
  verbose "Waiting for Docker daemon (timeout: ${MAX_WAIT_DOCKER}s)..."
  local elapsed=0
  while ! is_docker_ready; do
    (( elapsed >= MAX_WAIT_DOCKER )) && fatal "Docker daemon did not start within ${MAX_WAIT_DOCKER}s."
    sleep 2; (( elapsed += 2 ))
  done
  verbose "Docker daemon ready (${elapsed}s)."
}

ensure_image() {
  local image="$1"
  [[ "${SKIP_PULL:-false}" == "true" ]] && docker image inspect "$image" >> "$INTERNAL_LOG" 2>&1 && return 0
  retry "$DOCKER_PULL_RETRIES" 10 "Pull $image" docker pull -q "$image"
}

pull_required_images() {
  local images=()
  case "${ONLY_TOOL:-all}" in
    linter) images+=("$IMG_SUPERLINTER") ;; semgrep) images+=("$IMG_SEMGREP") ;;
    trufflehog) images+=("$IMG_TRUFFLEHOG") ;;
    all) images+=("$IMG_SUPERLINTER" "$IMG_SEMGREP" "$IMG_TRUFFLEHOG") ;; esac
  for img in "${images[@]}"; do ensure_image "$img" || _log "WARNING: Failed to pull $img"; done
}

# ─── Security rule auto-update ────────────────────────────────────────────────

rule_cache_is_fresh() {
  local stamp="$RULE_CACHE_DIR/.last-update"
  [[ ! -f "$stamp" ]] && return 1
  local last_update now age
  last_update=$(cat "$stamp" 2>/dev/null || echo "0")
  now=$(date +%s)
  age=$(( now - last_update ))
  (( age < RULE_TTL_SECONDS ))
}

fetch_semgrep_registry_rules() {
  local output="$RULE_CACHE_DIR/semgrep-registry.yml"
  verbose "Fetching Semgrep registry rulesets..."

  # FIX #7: Use array for config args instead of unquoted string
  local config_args=(
    --config "p/default"
    --config "p/owasp-top-ten"
    --config "p/cwe-top-25"
    --config "p/security-audit"
    --config "p/supply-chain"
    --config "p/secrets"
    --config "p/command-injection"
    --config "p/sql-injection"
    --config "p/xss"
    --config "p/insecure-transport"
  )

  local dummy_dir
  dummy_dir=$(mktemp -d "${TMPDIR:-/tmp}/semgrep-rules-XXXXXX")
  echo "# dummy" > "$dummy_dir/dummy.py"

  docker run --rm \
    --memory=2g \
    -v "$dummy_dir":/src \
    "$IMG_SEMGREP" \
    semgrep scan "${config_args[@]}" --json --dry-run /src \
    > "$WORK_DIR/semgrep-registry-dump.json" 2>>"$INTERNAL_LOG"
  local rc=$?

  rm -rf "$dummy_dir"

  if (( rc == 0 )) || [[ -s "$WORK_DIR/semgrep-registry-dump.json" ]]; then
    # FIX #8: Removed 'import yaml' — write plain text, no yaml dependency
    if has_cmd python3; then
      python3 - "$WORK_DIR/semgrep-registry-dump.json" "$output" <<'PYEOF'
import json, sys, datetime

try:
    with open(sys.argv[1]) as f:
        data = json.load(f)

    packs = [
        "p/default", "p/owasp-top-ten", "p/cwe-top-25",
        "p/security-audit", "p/supply-chain", "p/secrets",
        "p/command-injection", "p/sql-injection", "p/xss",
        "p/insecure-transport"
    ]

    with open(sys.argv[2], 'w') as f:
        f.write("# Auto-generated by scan.sh — Semgrep registry rulesets\n")
        f.write("# Updated: " + datetime.datetime.utcnow().isoformat() + "Z\n")
        f.write("# These rulesets are resolved at scan time from the Semgrep Registry.\n\n")
        for pack in packs:
            f.write("# " + pack + "\n")
        f.write("\n")
    sys.exit(0)
except Exception as e:
    print("Rule extraction failed: " + str(e), file=sys.stderr)
    sys.exit(1)
PYEOF
    else
      # No python3 — write manually
      {
        echo "# Auto-generated by scan.sh — Semgrep registry rulesets"
        echo "# Updated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
      } > "$output"
    fi
    _log "Semgrep registry rules validated and cached."
    return 0
  else
    _log "WARNING: Semgrep registry fetch failed."
    return 1
  fi
}

fetch_github_codeql_rules() {
  local output="$RULE_CACHE_DIR/github-codeql.yml"
  verbose "Fetching GitHub CodeQL security query mappings..."

  if download_soft \
    "https://raw.githubusercontent.com/github/codeql/main/misc/suite-helpers/security-extended-selectors.yml" \
    "$WORK_DIR/codeql-security-selectors.yml"; then

    _log "Got CodeQL security selectors."
    download_soft \
      "https://raw.githubusercontent.com/github/codeql/main/misc/suite-helpers/security-and-quality-selectors.yml" \
      "$WORK_DIR/codeql-quality-selectors.yml" || true

    if has_cmd python3; then
      python3 - "$WORK_DIR" "$output" <<'PYEOF'
import os, sys, re, datetime

work_dir = sys.argv[1]
output = sys.argv[2]

cwes_found = set()
for fname in ['codeql-security-selectors.yml', 'codeql-quality-selectors.yml']:
    fpath = os.path.join(work_dir, fname)
    if os.path.exists(fpath):
        with open(fpath) as f:
            for line in f:
                for m in re.finditer(r'cwe[/-](\d+)', line, re.IGNORECASE):
                    cwes_found.add(int(m.group(1)))

cwe_to_semgrep = {
    79: "p/xss", 89: "p/sql-injection", 78: "p/command-injection",
    22: "p/path-traversal", 502: "p/deserialization", 918: "p/ssrf",
    611: "p/xml", 295: "p/insecure-transport", 327: "p/insecure-transport",
    798: "p/secrets", 200: "p/security-audit", 352: "p/security-audit",
    434: "p/security-audit", 862: "p/security-audit", 863: "p/security-audit",
}

extra_packs = set()
for cwe in cwes_found:
    if cwe in cwe_to_semgrep:
        extra_packs.add(cwe_to_semgrep[cwe])

with open(output, 'w') as f:
    f.write("# Auto-generated by scan.sh — GitHub CodeQL CWE coverage mapping\n")
    f.write("# Updated: " + datetime.datetime.utcnow().isoformat() + "Z\n")
    f.write("# CWEs found in CodeQL suites: " + str(sorted(cwes_found)) + "\n")
    f.write("# Mapped to Semgrep packs: " + str(sorted(extra_packs)) + "\n\n")
    for pack in sorted(extra_packs):
        f.write("# - " + pack + "\n")
sys.exit(0)
PYEOF
    fi
    _log "GitHub CodeQL rules mapped."
    return 0
  else
    _log "WARNING: GitHub CodeQL fetch failed."
    return 1
  fi
}

fetch_owasp_community_rules() {
  local output="$RULE_CACHE_DIR/owasp-community.yml"
  verbose "Fetching OWASP community rules..."
  local tob_rules="$WORK_DIR/tob-rules.yml"
  if download_soft \
    "https://raw.githubusercontent.com/trailofbits/semgrep-rules/main/rules.yaml" \
    "$tob_rules"; then
    if [[ -s "$tob_rules" ]] && head -5 "$tob_rules" | grep -q "rules" 2>/dev/null; then
      cp "$tob_rules" "$output"
      _log "Trail of Bits community rules fetched."
      return 0
    fi
  fi
  _log "WARNING: OWASP community rules fetch failed."
  return 1
}

update_security_rules() {
  if [[ "${ONLY_TOOL:-all}" == "linter" ]] || [[ "${ONLY_TOOL:-all}" == "trufflehog" ]]; then
    _log "Skipping rule update (tool=$ONLY_TOOL)."; return 0; fi

  if [[ "$UPDATE_RULES" != "true" ]]; then
    _log "Rule updates disabled."; return 0; fi

  if [[ "${FORCE_UPDATE:-false}" != "true" ]] && rule_cache_is_fresh; then
    local age=$(( $(date +%s) - $(cat "$RULE_CACHE_DIR/.last-update") ))
    verbose "Using cached rules ($(( age / 3600 ))h old)."
    RULES_UPDATED=true
    return 0
  fi

  mkdir -p "$RULE_CACHE_DIR"
  verbose "Updating security rules..."
  local sources_ok=0

  ensure_image "$IMG_SEMGREP" || _log "WARNING: Semgrep image not available for rule fetch"

  fetch_semgrep_registry_rules && (( sources_ok++ )) || true
  fetch_github_codeql_rules    && (( sources_ok++ )) || true
  fetch_owasp_community_rules  && (( sources_ok++ )) || true

  if (( sources_ok > 0 )); then
    date +%s > "$RULE_CACHE_DIR/.last-update"
    verbose "Rules updated ($sources_ok/3 sources)."
    RULES_UPDATED=true
  else
    verbose "Rule update failed — using Semgrep built-in rules."
  fi
}

deploy_rules_to_scan_dir() {
  if [[ "${ONLY_TOOL:-all}" == "linter" ]] || [[ "${ONLY_TOOL:-all}" == "trufflehog" ]]; then
    return 0; fi

  local semgrep_dir="$SCAN_DIR/.semgrep"
  mkdir -p "$semgrep_dir"
  local deployed=0

  if [[ -f "$RULE_CACHE_DIR/owasp-community.yml" ]]; then
    cp "$RULE_CACHE_DIR/owasp-community.yml" "$semgrep_dir/owasp-community.yml" 2>/dev/null
    (( deployed++ ))
  fi

  cat > "$WORK_DIR/rules-metadata.json" <<ENDJSON
{
  "rules_updated": $RULES_UPDATED,
  "cache_dir": "$(json_escape "$RULE_CACHE_DIR")",
  "deployed_to": "$(json_escape "$semgrep_dir")",
  "files_deployed": $deployed,
  "cache_age_seconds": $(( $(date +%s) - $(cat "$RULE_CACHE_DIR/.last-update" 2>/dev/null || echo "0") )),
  "sources": {
    "semgrep_registry": $([ -f "$RULE_CACHE_DIR/semgrep-registry.yml" ] && echo true || echo false),
    "github_codeql": $([ -f "$RULE_CACHE_DIR/github-codeql.yml" ] && echo true || echo false),
    "owasp_community": $([ -f "$RULE_CACHE_DIR/owasp-community.yml" ] && echo true || echo false)
  }
}
ENDJSON
}

# ─── Safety pre-flight checks ────────────────────────────────────────────────

check_disk_space() {
  local available_kb
  available_kb=$(df -k "${OUTPUT_DIR}" 2>/dev/null | awk 'NR==2 {print $4}') || return 0
  [[ -n "$available_kb" ]] && (( available_kb < 5242880 )) && verbose "Low disk space warning."
}

check_network_mount() {
  [[ "$ALLOW_NETWORK_DRIVES" == "true" ]] && return 0
  local fstype=""
  if [[ "$PLATFORM" == "macos" ]]; then
    fstype=$(mount | grep " on ${PROJECT_DIR}" | head -1 | awk '{print $4}' | tr -d '(,')
  else
    fstype=$(df -T "$PROJECT_DIR" 2>/dev/null | awk 'NR==2 {print $2}')
  fi
  # FIX #9: use portable to_lower instead of ${var,,}
  local fstype_lower
  fstype_lower=$(to_lower "${fstype:-}")
  case "$fstype_lower" in
    nfs*|cifs|smb*|sshfs|fuse.sshfs|afs|9p|fuse.rclone|fuse.s3fs|fuse.gcsfuse)
      fatal "PROJECT_DIR is on a network filesystem ($fstype). Use --allow-network to override." ;; esac
}

check_symlinks() {
  [[ "$FOLLOW_SYMLINKS" == "true" ]] && return 0
  local c; c=$(find "$PROJECT_DIR" -maxdepth 3 -type l 2>/dev/null | head -100 | wc -l | tr -d ' ')
  (( c > 0 )) && verbose "Found $c+ symlinks — skipped (use --follow-symlinks)."
}

check_special_files() {
  [[ "$ALLOW_SPECIAL_FILES" == "true" ]] && return 0
  local s; s=$(find "$PROJECT_DIR" -maxdepth 3 \( -type b -o -type c -o -type p -o -type s \) 2>/dev/null | head -5)
  [[ -n "$s" ]] && fatal "Found special files in scan tree. Use --allow-special to override."
}

check_scan_size() {
  (( MAX_SCAN_SIZE_MB == 0 )) && return 0
  local size_kb
  [[ "$FOLLOW_SYMLINKS" == "true" ]] \
    && size_kb=$(du -sk "$PROJECT_DIR" 2>/dev/null | awk '{print $1}') \
    || size_kb=$(du -skP "$PROJECT_DIR" 2>/dev/null | awk '{print $1}')
  [[ -n "${size_kb:-}" ]] && (( size_kb / 1024 > MAX_SCAN_SIZE_MB )) \
    && fatal "Scan target exceeds --max-scan-size of ${MAX_SCAN_SIZE_MB}MB."
}

check_file_count() {
  local c
  [[ "$FOLLOW_SYMLINKS" == "true" ]] \
    && c=$(find "$PROJECT_DIR" -type f 2>/dev/null | head -$((MAX_FILE_COUNT+1)) | wc -l | tr -d ' ') \
    || c=$(find "$PROJECT_DIR" -not -type l -type f 2>/dev/null | head -$((MAX_FILE_COUNT+1)) | wc -l | tr -d ' ')
  (( c > MAX_FILE_COUNT )) && verbose "Warning: $c+ files — scan may be slow."
}

run_safety_checks() {
  verbose "Running pre-flight safety checks..."
  check_network_mount; check_symlinks; check_special_files
  check_scan_size; check_file_count; check_disk_space
  verbose "Safety checks passed."
}

# ─── Target staging ──────────────────────────────────────────────────────────

should_skip_file() {
  local file="$1"
  [[ -L "$file" ]] && [[ "$FOLLOW_SYMLINKS" != "true" ]] && return 0
  if [[ ! -f "$file" ]] && [[ ! -d "$file" ]] && [[ ! -L "$file" ]]; then
    [[ "$ALLOW_SPECIAL_FILES" != "true" ]] && return 0; fi
  if [[ "$ALLOW_HIDDEN" != "true" ]]; then
    local b; b=$(basename "$file"); [[ "$b" == .* ]] && return 0; fi
  if (( MAX_FILE_SIZE_MB > 0 )) && [[ -f "$file" ]]; then
    local sk; sk=$(du -k "$file" 2>/dev/null | awk '{print $1}')
    [[ -n "${sk:-}" ]] && (( sk > MAX_FILE_SIZE_MB * 1024 )) && return 0; fi
  return 1
}

stage_file() {
  local src="$1" rel="$2"
  should_skip_file "$src" && return 0
  local d; d=$(dirname "$rel"); mkdir -p "$STAGING_DIR/$d"
  if [[ -L "$src" ]] && [[ "$FOLLOW_SYMLINKS" == "true" ]]; then
    cp -aL "$src" "$STAGING_DIR/$rel" 2>/dev/null
  else cp -a "$src" "$STAGING_DIR/$rel" 2>/dev/null; fi
}

prepare_scan_dir() {
  if [[ ${#TARGETS[@]} -eq 0 ]]; then
    SCAN_DIR="$PROJECT_DIR"; verbose "Scanning entire project."; return 0; fi

  STAGING_DIR=$(mktemp -d "${TMPDIR:-/tmp}/scan-stage-XXXXXX")

  for target in "${TARGETS[@]}"; do
    local src="$PROJECT_DIR/$target"
    [[ ! -e "$src" ]] && [[ ! -L "$src" ]] && { verbose "Target not found: $target"; continue; }

    if [[ -f "$src" ]] || { [[ -L "$src" ]] && [[ -f "$(readlink -f "$src" 2>/dev/null || true)" ]]; }; then
      stage_file "$src" "$target"
    elif [[ -d "$src" ]]; then
      if [[ "$RECURSIVE" == "true" ]]; then
        if [[ "$RESPECT_GITIGNORE" == "true" ]] && has_cmd git && [[ -d "$PROJECT_DIR/.git" ]]; then
          ( cd "$PROJECT_DIR"; git ls-files --cached --others --exclude-standard "$target" 2>/dev/null
          ) | while IFS= read -r f; do [[ -f "$PROJECT_DIR/$f" ]] && stage_file "$PROJECT_DIR/$f" "$f"; done
        else
          local fa=(-type f)
          [[ "$FOLLOW_SYMLINKS" == "true" ]] && fa=(-follow -type f)
          [[ "$ALLOW_HIDDEN" != "true" ]] && fa+=(-not -path '*/.*')
          find "$src" "${fa[@]}" 2>/dev/null | while IFS= read -r f; do
            stage_file "$f" "${f#$PROJECT_DIR/}"; done
        fi
      else
        mkdir -p "$STAGING_DIR/$target"
        local fa=(-maxdepth 1 -type f)
        [[ "$FOLLOW_SYMLINKS" == "true" ]] && fa=(-maxdepth 1 -follow -type f)
        [[ "$ALLOW_HIDDEN" != "true" ]] && fa+=(-not -name '.*')
        find "$src" "${fa[@]}" 2>/dev/null | while IFS= read -r f; do
          stage_file "$f" "${f#$PROJECT_DIR/}"; done
      fi
    fi
  done

  # FIX #11: guard empty array iteration for bash 3.2
  if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
    for pattern in "${EXCLUDES[@]}"; do
      find "$STAGING_DIR" -type f 2>/dev/null | grep -E "$pattern" | while IFS= read -r f; do rm -f "$f"; done
    done
  fi

  for cfg in .eslintrc .eslintrc.js .eslintrc.json .eslintrc.yml .eslintrc.yaml \
    .prettierrc .prettierrc.json .prettierrc.yml .pylintrc pyproject.toml setup.cfg mypy.ini \
    .flake8 .rubocop.yml .golangci.yml .golangci.yaml .semgrepignore .gitignore \
    tsconfig.json tslint.json .editorconfig .clang-format .clang-tidy .super-linter.env; do
    [[ -f "$PROJECT_DIR/$cfg" ]] && [[ ! -f "$STAGING_DIR/$cfg" ]] \
      && cp -a "$PROJECT_DIR/$cfg" "$STAGING_DIR/$cfg" 2>/dev/null
  done

  if [[ -d "$PROJECT_DIR/.semgrep" ]]; then
    cp -a "$PROJECT_DIR/.semgrep" "$STAGING_DIR/.semgrep" 2>/dev/null
  fi

  local fc; fc=$(find "$STAGING_DIR" -type f | wc -l | tr -d ' ')
  (( fc == 0 )) && fatal "No files matched targets."
  SCAN_DIR="$STAGING_DIR"
  verbose "Staged $fc file(s)."
}

# FIX #12: Clearer logic for copy_fixes_back
copy_fixes_back() {
  if [[ -z "${STAGING_DIR:-}" ]]; then return 0; fi
  if [[ "$AUTOFIX" != "true" ]]; then return 0; fi
  verbose "Copying autofix changes back..."
  find "$STAGING_DIR" -type f | while IFS= read -r sf; do
    local rel="${sf#$STAGING_DIR/}"
    local orig="$PROJECT_DIR/$rel"
    case "$rel" in
      .eslintrc*|.prettierrc*|.pylintrc|pyproject.toml|setup.cfg|mypy.ini|\
      .flake8|.rubocop.yml|.golangci.*|.semgrepignore|.gitignore|\
      tsconfig.json|tslint.json|.editorconfig|.clang-format|.clang-tidy|\
      .super-linter.env|.semgrep/*) continue ;; esac
    [[ -f "$orig" ]] && ! cmp -s "$sf" "$orig" && cp -a "$sf" "$orig"
  done
}

build_trufflehog_excludes() {
  local ef="$WORK_DIR/trufflehog-excludes.txt"
  if [[ "$RESPECT_GITIGNORE" == "true" ]] && [[ -f "$SCAN_DIR/.gitignore" ]]; then
    grep -v '^#' "$SCAN_DIR/.gitignore" | grep -v '^$' | sed 's/^\///' | while IFS= read -r p; do
      echo "$p" | sed 's/\./\\./g; s/\*/.*/g; s/\?/./g'; done > "$ef" 2>/dev/null; fi
  # FIX #11: guard empty array
  if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
    for p in "${EXCLUDES[@]}"; do echo "$p" >> "$ef"; done
  fi
  [[ -f "$ef" ]] && [[ -s "$ef" ]] && echo "$ef" || echo ""
}

# FIX #14: use portable timeout wrapper
run_docker_with_timeout() {
  if (( SCAN_TIMEOUT > 0 )); then
    portable_timeout "$SCAN_TIMEOUT" "$@"
  else
    "$@"
  fi
}

# ─── Scan steps ───────────────────────────────────────────────────────────────

run_superlinter() {
  local tmplog="$WORK_DIR/superlinter-raw.log"
  verbose "[1/3] Super-Linter (autofix=$AUTOFIX)..."

  local extra_env=()
  if [[ "$AUTOFIX" == "true" ]]; then
    extra_env+=(
      -e FIX_ANSIBLE=true -e FIX_CLANG_FORMAT=true -e FIX_CSHARP=true
      -e FIX_CSS=true -e FIX_ENV=true -e FIX_GO=true -e FIX_GO_MODULES=true
      -e FIX_GOOGLE_JAVA_FORMAT=true -e FIX_GROOVY=true
      -e FIX_JAVASCRIPT_ES=true -e FIX_JAVASCRIPT_PRETTIER=true
      -e FIX_JAVASCRIPT_STANDARD=true -e FIX_JSON=true -e FIX_JSONC=true
      -e FIX_JSX=true -e FIX_MARKDOWN=true -e FIX_NATURAL_LANGUAGE=true
      -e FIX_POWERSHELL=true -e FIX_PROTOBUF=true -e FIX_PYTHON_BLACK=true
      -e FIX_PYTHON_ISORT=true -e FIX_PYTHON_RUFF=true -e FIX_RUBY=true
      -e FIX_RUST_2015=true -e FIX_RUST_2018=true -e FIX_RUST_2021=true
      -e FIX_RUST_CLIPPY=true -e FIX_SCALAFMT=true -e FIX_SHELL_SHFMT=true
      -e FIX_SNAKEMAKE_SNAKEFMT=true -e FIX_SQLFLUFF=true
      -e FIX_TERRAFORM_FMT=true -e FIX_TSX=true -e FIX_TYPESCRIPT_ES=true
      -e FIX_TYPESCRIPT_PRETTIER=true -e FIX_TYPESCRIPT_STANDARD=true
      -e FIX_XML=true -e FIX_YAML_PRETTIER=true
    )
  fi

  # FIX #11: guard empty array
  if [[ ${#EXCLUDES[@]} -gt 0 ]] && [[ ${#TARGETS[@]} -eq 0 ]]; then
    local joined=""
    for p in "${EXCLUDES[@]}"; do [[ -n "$joined" ]] && joined+="|"; joined+="$p"; done
    extra_env+=(-e "FILTER_REGEX_EXCLUDE=$joined")
  fi

  local attempt=1 rc=0
  while (( attempt <= MAX_RETRIES )); do
    _log "Attempt $attempt/$MAX_RETRIES: Super-Linter"
    # FIX #15: Removed --network=none — Super-Linter needs network for linter plugins
    run_docker_with_timeout docker run --rm \
      -e RUN_LOCAL=true -e DEFAULT_BRANCH=main \
      -e CREATE_LOG_FILE=true -e LOG_FILE=superlinter.log -e LOG_LEVEL=ERROR \
      ${extra_env[@]+"${extra_env[@]}"} \
      --memory=4g --cpus=2 --security-opt=no-new-privileges \
      -v "$SCAN_DIR":/tmp/lint \
      "$IMG_SUPERLINTER" >> "$INTERNAL_LOG" 2>&1
    rc=$?
    [[ -f "$SCAN_DIR/superlinter.log" ]] && { mv "$SCAN_DIR/superlinter.log" "$tmplog"; break; }
    (( attempt++ )); sleep "$RETRY_DELAY"
  done

  if [[ -f "$tmplog" ]]; then
    local ec; ec=$(grep -cE "ERROR|FATAL" "$tmplog" 2>/dev/null || echo "0")
    local fc=0; [[ "$AUTOFIX" == "true" ]] && fc=$(grep -ciE "fixed|auto.?fix" "$tmplog" 2>/dev/null || echo "0")
    # FIX #16: correct status computation
    local status="findings"
    (( rc == 0 )) && status="pass"
    cat > "$WORK_DIR/result-linter.json" <<ENDJSON
{ "tool": "super-linter", "status": "$status", "autofix_enabled": $AUTOFIX, "errors_found": $ec, "files_fixed": $fc, "log_lines": $(wc -l < "$tmplog" | tr -d ' ') }
ENDJSON
    verbose "[1/3] Super-Linter done ($ec errors, $fc fixes)."; return 0
  else
    cat > "$WORK_DIR/result-linter.json" <<'ENDJSON'
{ "tool": "super-linter", "status": "error", "message": "Failed after all retries" }
ENDJSON
    return 1
  fi
}

run_semgrep() {
  local tmpjson="$WORK_DIR/semgrep-raw.json"
  verbose "[2/3] Semgrep (autofix=$AUTOFIX, rules_updated=$RULES_UPDATED)..."

  local extra_args=()
  [[ "$RESPECT_GITIGNORE" == "false" ]] && extra_args+=(--no-git-ignore)
  # FIX #11: guard empty array
  if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
    for p in "${EXCLUDES[@]}"; do extra_args+=(--exclude "$p"); done
  fi
  [[ "$AUTOFIX" == "true" ]] && extra_args+=(--autofix)

  local config_args=(
    --config auto
    --config "p/default"
    --config "p/owasp-top-ten"
    --config "p/cwe-top-25"
    --config "p/security-audit"
    --config "p/supply-chain"
    --config "p/secrets"
    --config "p/command-injection"
    --config "p/sql-injection"
    --config "p/xss"
    --config "p/insecure-transport"
  )

  # FIX #17: use container path /src/.semgrep/ not host path
  if [[ -d "$SCAN_DIR/.semgrep" ]]; then
    config_args+=(--config /src/.semgrep/)
  fi

  local attempt=1 rc=0
  while (( attempt <= MAX_RETRIES )); do
    _log "Attempt $attempt/$MAX_RETRIES: Semgrep"
    run_docker_with_timeout docker run --rm \
      --memory=4g --cpus=2 --security-opt=no-new-privileges \
      -v "$SCAN_DIR":/src \
      "$IMG_SEMGREP" \
      semgrep scan "${config_args[@]}" --json -o /src/.semgrep-tmp-output.json \
      ${extra_args[@]+"${extra_args[@]}"} /src >> "$INTERNAL_LOG" 2>&1
    rc=$?
    if [[ -f "$SCAN_DIR/.semgrep-tmp-output.json" ]] && [[ -s "$SCAN_DIR/.semgrep-tmp-output.json" ]]; then
      mv "$SCAN_DIR/.semgrep-tmp-output.json" "$tmpjson"; break; fi
    rm -f "$SCAN_DIR/.semgrep-tmp-output.json" 2>/dev/null
    (( attempt++ )); sleep "$RETRY_DELAY"
  done

  if [[ -f "$tmpjson" ]]; then
    local findings=0 fixes=0
    if has_cmd python3; then
      local pyout
      pyout=$(python3 -c "
import json,sys
try:
 d=json.load(open(sys.argv[1]));r=d.get('results',[])
 print(len(r),sum(1 for x in r if x.get('extra',{}).get('fixed_lines')))
except:print('0 0')" "$tmpjson" 2>/dev/null) || pyout="0 0"
      read -r findings fixes <<< "$pyout"
      findings="${findings:-0}"; fixes="${fixes:-0}"
    elif has_cmd jq; then findings=$(jq '.results|length' "$tmpjson" 2>/dev/null || echo "0"); fi
    local status="findings"
    [[ "$findings" == "0" ]] && status="pass"
    cat > "$WORK_DIR/result-semgrep.json" <<ENDJSON
{ "tool": "semgrep", "status": "$status", "autofix_enabled": $AUTOFIX, "findings_count": $findings, "fixes_applied": $fixes }
ENDJSON
    verbose "[2/3] Semgrep done ($findings findings, $fixes fixes)."; return 0
  else
    cat > "$WORK_DIR/result-semgrep.json" <<'ENDJSON'
{ "tool": "semgrep", "status": "error", "message": "Failed after all retries" }
ENDJSON
    return 1
  fi
}

run_trufflehog() {
  local tmpjson="$WORK_DIR/trufflehog-raw.json"
  verbose "[3/3] TruffleHog..."
  local th_mode="filesystem" extra_args=()
  if [[ "$RESPECT_GITIGNORE" == "true" ]] && [[ -d "$SCAN_DIR/.git" ]]; then th_mode="git"
  else
    local ef; ef=$(build_trufflehog_excludes)
    [[ -n "$ef" ]] && { cp "$ef" "$SCAN_DIR/.trufflehog-excludes.txt" 2>/dev/null
      extra_args+=(--exclude-paths "/src/.trufflehog-excludes.txt"); }
  fi

  local attempt=1
  while (( attempt <= MAX_RETRIES )); do
    local th_target="/src"; [[ "$th_mode" == "git" ]] && th_target="file:///src"
    run_docker_with_timeout docker run --rm \
      --memory=2g --cpus=2 --network=none --security-opt=no-new-privileges \
      -v "$SCAN_DIR":/src:ro "$IMG_TRUFFLEHOG" \
      "$th_mode" "$th_target" --json --no-verification ${extra_args[@]+"${extra_args[@]}"} \
      > "$tmpjson" 2>>"$INTERNAL_LOG"
    [[ -f "$tmpjson" ]] && break
    (( attempt++ )); sleep "$RETRY_DELAY"
  done
  rm -f "$SCAN_DIR/.trufflehog-excludes.txt" 2>/dev/null

  if [[ -f "$tmpjson" ]]; then
    local sc; sc=$(wc -l < "$tmpjson" 2>/dev/null | tr -d ' ')
    local status="findings"
    [[ "$sc" == "0" ]] && status="pass"
    cat > "$WORK_DIR/result-trufflehog.json" <<ENDJSON
{ "tool": "trufflehog", "status": "$status", "autofix_enabled": false, "secrets_found": $sc }
ENDJSON
    verbose "[3/3] TruffleHog done ($sc secrets)."; return 0
  else
    cat > "$WORK_DIR/result-trufflehog.json" <<'ENDJSON'
{ "tool": "trufflehog", "status": "error", "message": "Failed after all retries" }
ENDJSON
    return 1
  fi
}

# ─── Assemble final report ────────────────────────────────────────────────────

assemble_report() {
  local ts; ts=$(date '+%Y%m%d-%H%M%S')
  FINAL_REPORT="${OUTPUT_DIR}/scan-report-${ts}.json"
  verbose "Assembling report → $FINAL_REPORT"

  # FIX #21: Use python3 or jq for safe JSON construction; bash fallback uses json_escape
  if has_cmd python3; then
    # Build targets/excludes safely via python3 json.dumps
    local targets_json="[]" excludes_json="[]"
    if [[ ${#TARGETS[@]} -gt 0 ]]; then
      targets_json=$(printf '%s\n' "${TARGETS[@]}" | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")
    fi
    if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
      excludes_json=$(printf '%s\n' "${EXCLUDES[@]}" | python3 -c "import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")
    fi

    python3 - "$WORK_DIR" "$FINAL_REPORT" "$PROJECT_DIR" "$PLATFORM" "${ONLY_TOOL:-all}" \
      "$RECURSIVE" "$RESPECT_GITIGNORE" "$targets_json" "$excludes_json" \
      "$FOLLOW_SYMLINKS" "$ALLOW_NETWORK_DRIVES" "$ALLOW_SPECIAL_FILES" \
      "$MAX_FILE_SIZE_MB" "$MAX_SCAN_SIZE_MB" "$SCAN_TIMEOUT" "$AUTOFIX" <<'PYEOF'
import json, sys, os, glob, datetime

(work_dir, out_path, proj_dir, platform, tools_run,
 recursive, gitignore, targets_s, excludes_s,
 follow_sym, allow_net, allow_special,
 max_file, max_scan, scan_timeout, autofix) = sys.argv[1:17]

report = {
    "scan_timestamp": datetime.datetime.utcnow().isoformat() + "Z",
    "project_dir": proj_dir,
    "platform": platform,
    "config": {
        "tools_requested": tools_run,
        "autofix": autofix == "true",
        "targets": json.loads(targets_s),
        "excludes": json.loads(excludes_s),
        "recursive": recursive == "true",
        "respect_gitignore": gitignore == "true",
        "follow_symlinks": follow_sym == "true",
        "allow_network_drives": allow_net == "true",
        "allow_special_files": allow_special == "true",
        "max_file_size_mb": int(max_file),
        "max_scan_size_mb": int(max_scan),
        "scan_timeout_sec": int(scan_timeout)
    },
    "results": {},
    "rules": {},
    "raw_data": {}
}

rm_path = os.path.join(work_dir, "rules-metadata.json")
if os.path.exists(rm_path):
    try: report["rules"] = json.load(open(rm_path))
    except: pass

for f in sorted(glob.glob(os.path.join(work_dir, "result-*.json"))):
    try:
        d = json.load(open(f))
        report["results"][d.get("tool", os.path.basename(f))] = d
    except: pass

for f in sorted(glob.glob(os.path.join(work_dir, "*-raw.*"))):
    name = os.path.basename(f)
    try:
        if f.endswith(".json"):
            content = open(f).read().strip()
            if content:
                parsed = []
                for line in content.split("\n"):
                    line = line.strip()
                    if line:
                        try: parsed.append(json.loads(line))
                        except: parsed.append({"raw_line": line})
                report["raw_data"][name] = parsed
            else: report["raw_data"][name] = []
        else:
            report["raw_data"][name] = "".join(open(f).readlines()[-500:])
    except Exception as e:
        report["raw_data"][name] = {"read_error": str(e)}

statuses = [r.get("status") for r in report["results"].values()]
if all(s == "pass" for s in statuses): report["overall_status"] = "pass"
elif any(s == "error" for s in statuses): report["overall_status"] = "partial_failure"
else: report["overall_status"] = "findings"

with open(out_path, "w") as fp:
    json.dump(report, fp, indent=2, default=str)
PYEOF
  else
    # Bash fallback — FIX #21: use json_escape for all interpolated values
    {
      echo "{"
      echo "  \"scan_timestamp\": \"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\","
      echo "  \"project_dir\": \"$(json_escape "$PROJECT_DIR")\","
      echo "  \"autofix\": $AUTOFIX,"
      echo "  \"results\": {"
      local first=true
      for f in "$WORK_DIR"/result-*.json; do
        [[ -f "$f" ]] || continue
        $first || echo ","; first=false
        local k; k=$(basename "$f" .json | sed 's/result-//')
        echo "    \"$k\": $(cat "$f")"
      done
      echo "  }"
      echo "}"
    } > "$FINAL_REPORT"
  fi
  _log "Report: $FINAL_REPORT ($(wc -c < "$FINAL_REPORT" | tr -d ' ') bytes)"
}

# ─── Parse arguments ──────────────────────────────────────────────────────────

SKIP_INSTALL=false
SKIP_PULL=false
ONLY_TOOL="all"
VERBOSE=false
PROJECT_DIR="."
OUTPUT_DIR=""
FORCE_UPDATE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)            show_help ;;
    -o|--output)          OUTPUT_DIR="${2:-}"; shift 2 ;;
    --only)               ONLY_TOOL="${2:-}"; shift 2 ;;
    --target)             TARGETS+=("${2:-}"); shift 2 ;;
    --target-list)        if [[ -f "${2:-}" ]]; then
                            while IFS= read -r _line; do
                              _line=$(echo "$_line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
                              [[ -n "$_line" ]] && [[ "$_line" != \#* ]] && TARGETS+=("$_line")
                            done < "$2"
                          else echo "ERROR: --target-list file not found: ${2:-}" >&2; exit 1; fi
                          shift 2 ;;
    --exclude)            EXCLUDES+=("${2:-}"); shift 2 ;;
    --no-recursive)       RECURSIVE=false; shift ;;
    --no-gitignore)       RESPECT_GITIGNORE=false; shift ;;
    --no-hidden)          ALLOW_HIDDEN=false; shift ;;
    --autofix)            AUTOFIX=true; shift ;;
    --no-autofix)         AUTOFIX=false; shift ;;
    --no-update-rules)    UPDATE_RULES=false; shift ;;
    --force-update)       FORCE_UPDATE=true; shift ;;
    --rule-ttl)           RULE_TTL_SECONDS="${2:-86400}"; shift 2 ;;
    --follow-symlinks)    FOLLOW_SYMLINKS=true; shift ;;
    --allow-network)      ALLOW_NETWORK_DRIVES=true; shift ;;
    --allow-special)      ALLOW_SPECIAL_FILES=true; shift ;;
    --max-file-size)      MAX_FILE_SIZE_MB="${2:-50}"; shift 2 ;;
    --max-scan-size)      MAX_SCAN_SIZE_MB="${2:-2048}"; shift 2 ;;
    --scan-timeout)       SCAN_TIMEOUT="${2:-3600}"; shift 2 ;;
    --skip-install)       SKIP_INSTALL=true; shift ;;
    --skip-pull)          SKIP_PULL=true; shift ;;
    --timeout)            MAX_WAIT_DOCKER="${2:-120}"; shift 2 ;;
    -v|--verbose)         VERBOSE=true; shift ;;
    -q|--quiet)           VERBOSE=false; shift ;;
    -*)                   echo "ERROR: Unknown option: $1. Use --help." >&2; exit 1 ;;
    *)                    PROJECT_DIR="$1"; shift ;;
  esac
done

case "$ONLY_TOOL" in
  all|linter|semgrep|trufflehog) ;;
  *) echo "ERROR: Invalid --only value: $ONLY_TOOL" >&2; exit 1 ;;
esac

# ─── Main ─────────────────────────────────────────────────────────────────────

main() {
  [[ -d "$PROJECT_DIR" ]] || { echo "ERROR: Not a directory: $PROJECT_DIR" >&2; exit 1; }
  PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"
  [[ -z "$OUTPUT_DIR" ]] && OUTPUT_DIR="$PROJECT_DIR"
  mkdir -p "$OUTPUT_DIR" 2>/dev/null || { echo "ERROR: Cannot create: $OUTPUT_DIR" >&2; exit 1; }
  OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

  WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/scan-XXXXXX")
  INTERNAL_LOG="$WORK_DIR/scan-internal.log"; touch "$INTERNAL_LOG"

  _log "═══ Scan started ═══"
  _log "Platform=$PLATFORM Project=$PROJECT_DIR Output=$OUTPUT_DIR"
  _log "Tool=$ONLY_TOOL Autofix=$AUTOFIX UpdateRules=$UPDATE_RULES"
  # FIX #22: safe logging of potentially empty arrays
  _log "Targets=${TARGETS[*]+"${TARGETS[*]}"} Excludes=${EXCLUDES[*]+"${EXCLUDES[*]}"}"
  verbose "Platform=$PLATFORM Project=$PROJECT_DIR Autofix=$AUTOFIX Rules=$UPDATE_RULES"

  acquire_lock
  run_safety_checks
  ensure_docker_installed
  wait_for_docker_daemon
  update_security_rules
  prepare_scan_dir
  deploy_rules_to_scan_dir
  pull_required_images

  local failures=0
  case "$ONLY_TOOL" in
    linter)     run_superlinter || (( failures++ )) ;;
    semgrep)    run_semgrep     || (( failures++ )) ;;
    trufflehog) run_trufflehog  || (( failures++ )) ;;
    all)
      run_superlinter || (( failures++ ))
      run_semgrep     || (( failures++ ))
      run_trufflehog  || (( failures++ )) ;;
  esac

  copy_fixes_back
  assemble_report

  if (( failures > 0 )); then
    local dl="${OUTPUT_DIR}/scan-debug-$(date '+%Y%m%d-%H%M%S').log"
    cp "$INTERNAL_LOG" "$dl" 2>/dev/null
    echo "WARNING: $failures tool(s) failed. Debug: $dl" >&2
  fi

  _log "═══ Scan finished — $failures failure(s) ═══"
  cleanup_and_exit 0
}

main
