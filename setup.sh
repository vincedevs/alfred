#!/usr/bin/env bash
# Sets Alfred up on a Raspberry Pi (or any Debian-like Linux) or a Mac. Safe to run again:
# it installs only what is missing and never overwrites your .env.
#
#   ./setup.sh               everything
#   ./setup.sh --no-office   without LibreOffice (only needed for old .doc, .ppt, .rtf files)
#   ./setup.sh --service     also start Alfred at boot (Linux: a systemd user service)
set -euo pipefail

cd "$(dirname "$0")"
office=yes
service=no
for arg in "$@"; do
  case "$arg" in
    --no-office) office=no ;;
    --service) service=yes ;;
    -h | --help) sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# 1. System packages: the agent's sandbox (bubblewrap, socat; built into macOS), git and
#    the GitHub CLI, PortAudio for the microphone (bundled on a Mac), LibreOffice.
case "$(uname -s)" in
  Linux)
    packages=(bubblewrap socat git gh portaudio19-dev curl)
    if [ "$office" = yes ]; then
      packages+=(libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress)
    fi
    missing=()
    for package in "${packages[@]}"; do
      dpkg -s "$package" >/dev/null 2>&1 || missing+=("$package")
    done
    if [ ${#missing[@]} -gt 0 ]; then
      say "Installing: ${missing[*]}"
      sudo apt-get update
      sudo apt-get install -y --no-install-recommends "${missing[@]}"
    else
      say "System packages: all there"
    fi
    ;;
  Darwin)
    if ! command -v brew >/dev/null; then
      echo "Homebrew is needed: https://brew.sh, then run this again." >&2
      exit 1
    fi
    command -v git >/dev/null || xcode-select --install || true
    command -v gh >/dev/null || { say "Installing the GitHub CLI"; brew install gh; }
    if [ "$office" = yes ] && [ ! -d /Applications/LibreOffice.app ]; then
      say "Installing LibreOffice"
      brew install --cask libreoffice
    fi
    ;;
  *)
    echo "Alfred runs on Linux (a Raspberry Pi) or a Mac." >&2
    exit 1
    ;;
esac

# 2. uv, and Alfred's Python packages.
if ! command -v uv >/dev/null; then
  say "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
say "Installing Alfred's Python packages"
uv sync

# 3. Settings. The keys are yours to fill in; this only checks they're there.
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  say "Made .env from .env.example: fill in your keys"
fi
filled() { grep -Eq "^$1=.+" .env; }
needed=()
filled OPENAI_API_KEY || needed+=("OPENAI_API_KEY")
filled CLAUDE_CODE_OAUTH_TOKEN || filled ANTHROPIC_API_KEY ||
  needed+=("CLAUDE_CODE_OAUTH_TOKEN (from: claude setup-token) or ANTHROPIC_API_KEY")

# 4. Optionally, start at boot.
if [ "$service" = yes ]; then
  if [ "$(uname -s)" != Linux ]; then
    echo "--service is for Linux; on a Mac, run uv run alfred yourself." >&2
  else
    say "Starting Alfred at boot (systemd user service)"
    mkdir -p "$HOME/.config/systemd/user"
    cat >"$HOME/.config/systemd/user/alfred.service" <<EOF
[Unit]
Description=Alfred
After=network-online.target sound.target

[Service]
WorkingDirectory=$PWD
ExecStart=$(command -v uv) run alfred
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable alfred
    sudo loginctl enable-linger "$USER"
    echo "Enabled. Start it now with: systemctl --user start alfred"
    echo "Follow its log with:        journalctl --user -u alfred -f"
  fi
fi

if [ ${#needed[@]} -gt 0 ]; then
  say "Almost ready. In .env, fill in:"
  printf '  - %s\n' "${needed[@]}"
  echo "Then: uv run alfred"
else
  say "Ready. Start Alfred with: uv run alfred"
fi
