#!/bin/bash
# Double-click this file in Finder to launch the Sales Coach overlay.
# macOS treats *.command files as runnable shell scripts via Terminal.

set -e

# Locate the script's own directory regardless of where it's launched from
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# macOS launches .command files in a non-interactive shell that does NOT
# source ~/.zshrc by default. Pull `export FOO=...` lines from ~/.zshrc
# (and ~/.zshenv / ~/.zprofile) so the API keys you set there are available.
for rc in "$HOME/.zshenv" "$HOME/.zprofile" "$HOME/.zshrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    if [ -f "$rc" ]; then
        set -a
        # shellcheck disable=SC1090
        eval "$(grep -E '^[[:space:]]*export [A-Za-z_][A-Za-z0-9_]*=' "$rc" || true)"
        set +a
    fi
done

# Also honor a local .env if present (overrides shell rc files on conflict)
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
fi

# Sanity check the required env vars
missing=()
[ -z "$ELEVENLABS_API_KEY" ] && missing+=("ELEVENLABS_API_KEY")
[ -z "$ANTHROPIC_API_KEY" ] && missing+=("ANTHROPIC_API_KEY")
if [ ${#missing[@]} -ne 0 ]; then
    echo ""
    echo "❌ Missing required env vars: ${missing[*]}"
    echo ""
    echo "Create a .env file in $SCRIPT_DIR with the keys filled in."
    echo "See .env.example for the template."
    echo ""
    echo "Press any key to exit..."
    read -n 1 -s
    exit 1
fi

# Optional vars — warn if absent, but don't block
[ -z "$GMAIL_APP_PASSWORD" ] && echo "ℹ️  GMAIL_APP_PASSWORD not set — post-call email will be skipped."
[ -z "$HUBSPOT_ACCESS_TOKEN" ] && echo "ℹ️  HUBSPOT_ACCESS_TOKEN not set — HubSpot note will be skipped."

# Launch the coach. Use unbuffered python so we see prints as they happen.
echo ""
echo "🎯 Launching Sales Coach overlay..."
echo "    (Close the overlay window or hit Ctrl-C to quit.)"
echo ""
exec python3 -u coach.py
