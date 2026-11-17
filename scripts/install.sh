#!/usr/bin/env bash
# Local install (#3 install macOS / Linux / Windows-WSL, #71 local machine)
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${HERMES_VENV:-$HOME/.hermes-venv}"
PY="${PYTHON:-python3}"

"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
"$PY" -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -e "$REPO_DIR${HERMES_EXTRAS:-}"

BIN="$HOME/.local/bin"
mkdir -p "$BIN"
ln -sf "$VENV/bin/hermes" "$BIN/hermes"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "Add $BIN to your PATH (e.g. in ~/.bashrc or ~/.zshrc)";; esac

echo "Installed. Next steps:"
echo "  hermes setup          # or: hermes setup --portal"
echo "  hermes doctor"
echo "  hermes                # start chatting"
