#!/usr/bin/env bash
# sshelf installer / updater -- macOS and Linux
# Usage: bash install.sh
set -euo pipefail

REPO_URL="https://github.com/georgegozal/sshelf.git"
INSTALL_DIR="$HOME/.local/share/sshelf"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/sshelf"
DESKTOP_FILE="$HOME/.local/share/applications/sshelf.desktop"
ICON_SRC="$INSTALL_DIR/assets/screenshot.png"

# -- Colour helpers -----------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[sshelf]${NC} $*"; }
warn()  { echo -e "${YELLOW}[sshelf]${NC} $*"; }
error() { echo -e "${RED}[sshelf] ERROR:${NC} $*" >&2; exit 1; }

# -- Platform detection -------------------------------------------------------
PLATFORM=""
case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux)  PLATFORM="linux" ;;
  *)      error "Unsupported OS: $(uname -s)" ;;
esac

# -- Detect if running from inside a cloned repo ------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$SCRIPT_DIR/main.py" && -f "$SCRIPT_DIR/requirements.txt" ]]; then
  _IN_REPO=true
else
  _IN_REPO=false
fi

info "Installing sshelf on ${PLATFORM}..."

# -- Python check -------------------------------------------------------------
PYTHON=""
for py in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$py" &>/dev/null; then
    if "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
      PYTHON="$py"
      break
    fi
  fi
done
[[ -z "$PYTHON" ]] && error "Python 3.10+ is required. Install it and re-run."
info "Using $("${PYTHON}" --version)"

# -- Copy or clone source to INSTALL_DIR --------------------------------------
mkdir -p "$INSTALL_DIR"

if [[ "$_IN_REPO" == true ]]; then
  info "Syncing source to ${INSTALL_DIR} ..."
  rsync -a --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "$SCRIPT_DIR/" "$INSTALL_DIR/"
elif [[ -d "$INSTALL_DIR/.git" ]]; then
  command -v git &>/dev/null || error "git is required. Install it and re-run."
  info "Updating existing installation..."
  git -C "$INSTALL_DIR" pull --ff-only
else
  command -v git &>/dev/null || error "git is required. Install it and re-run."
  info "Cloning sshelf to ${INSTALL_DIR} ..."
  git clone --depth=1 "$REPO_URL" "$INSTALL_DIR"
fi

# -- Python virtual environment -----------------------------------------------
VENV="$INSTALL_DIR/.venv"
if [[ ! -d "$VENV" ]]; then
  info "Creating virtual environment..."
  "$PYTHON" -m venv "$VENV"
fi

info "Installing Python dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# Register the `sshelf` CLI entry point inside the venv.
# Using editable install (-e) so rsync updates are reflected without reinstalling.
info "Registering sshelf CLI entry point..."
"$VENV/bin/pip" install --quiet -e "$INSTALL_DIR"

# -- Linux: optional SecretService backend for keyring ------------------------
if [[ "$PLATFORM" == "linux" ]]; then
  if "$VENV/bin/python3" -c "import secretstorage" 2>/dev/null; then
    : # already installed
  else
    warn "secretstorage not found. Passwords will be stored in a local file instead of"
    warn "GNOME Keyring / KWallet. Install it for better security:"
    warn "  pip install secretstorage   (or: apt install python3-secretstorage)"
    "$VENV/bin/pip" install --quiet secretstorage 2>/dev/null || true
  fi
fi

# -- Shell launcher -----------------------------------------------------------
# The launcher delegates to the venv's `sshelf` entry point, which supports
# both CLI subcommands and `sshelf gui` for the GUI.
mkdir -p "$BIN_DIR"
cat > "$LAUNCHER" << LAUNCHER_EOF
#!/usr/bin/env bash
exec "$VENV/bin/sshelf" "\$@"
LAUNCHER_EOF
chmod +x "$LAUNCHER"
info "Shell launcher: ${LAUNCHER}"

# -- Linux: .desktop file -----------------------------------------------------
if [[ "$PLATFORM" == "linux" ]]; then
  mkdir -p "$(dirname "$DESKTOP_FILE")"

  if [[ -f "$ICON_SRC" ]]; then
    ICON_VALUE="$ICON_SRC"
  else
    ICON_VALUE="utilities-terminal"
  fi

  cat > "$DESKTOP_FILE" << DESKTOP_EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=sshelf
Comment=SSH connection manager for macOS and Linux
Exec=$LAUNCHER gui
Icon=$ICON_VALUE
Terminal=false
Categories=Network;RemoteAccess;
Keywords=ssh;terminal;remote;connection;
DESKTOP_EOF

  command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true

  info ".desktop file: ${DESKTOP_FILE}"
fi

# -- macOS: PATH setup + .app bundle ------------------------------------------
if [[ "$PLATFORM" == "macos" ]]; then
  _detect_shell_rc() {
    case "$(basename "${SHELL:-bash}")" in
      zsh)  echo "$HOME/.zshrc" ;;
      bash) echo "$HOME/.bashrc" ;;
      fish) echo "$HOME/.config/fish/config.fish" ;;
      *)    echo "$HOME/.profile" ;;
    esac
  }
  SHELL_RC="$(_detect_shell_rc)"

  if ! echo "$PATH" | tr ':' '\n' | grep -qx "$BIN_DIR"; then
    PATH_LINE="export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
    warn "$BIN_DIR is not in your PATH."
    read -r -p "    Add it to $SHELL_RC automatically? [Y/n] " yn
    if [[ "$yn" != [Nn]* ]]; then
      echo "" >> "$SHELL_RC"
      echo "# Added by sshelf installer" >> "$SHELL_RC"
      echo "$PATH_LINE" >> "$SHELL_RC"
      info "Added PATH entry to ${SHELL_RC}"
      export PATH="$BIN_DIR:$PATH"
    else
      warn "Skipped. Add this to your shell config manually:"
      warn "  $PATH_LINE"
    fi
  fi

  APP_BUNDLE="/Applications/sshelf.app"
  info "Creating ${APP_BUNDLE} ..."
  mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"

  cat > "$APP_BUNDLE/Contents/MacOS/sshelf" << APP_EOF
#!/bin/bash
# .app always opens the GUI
exec "$VENV/bin/sshelf" gui
APP_EOF
  chmod +x "$APP_BUNDLE/Contents/MacOS/sshelf"

  cat > "$APP_BUNDLE/Contents/Info.plist" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>sshelf</string>
    <key>CFBundleDisplayName</key>
    <string>sshelf</string>
    <key>CFBundleIdentifier</key>
    <string>com.keytype.sshelf</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>sshelf</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
</dict>
</plist>
PLIST_EOF

  xattr -rd com.apple.quarantine "$APP_BUNDLE" 2>/dev/null || true
  info ".app bundle: ${APP_BUNDLE}"
fi

# -- Done ---------------------------------------------------------------------
echo ""
info "sshelf installed successfully!"
info "Source lives at: ${INSTALL_DIR}"
echo ""
info "CLI usage:"
info "  sshelf list                   # list saved connections"
info "  sshelf add                    # add a connection interactively"
info "  sshelf connect <name>         # open an SSH session in this terminal"
info "  sshelf snippet list           # list saved commands"
info "  sshelf gui                    # launch the GUI"
info "  sshelf --help                 # full command reference"
if [[ "$PLATFORM" == "macos" ]]; then
  echo ""
  info "GUI: open from Applications / Dock / Spotlight, or run: sshelf gui"
  info "(Re-run install.sh at any time to update.)"
fi
