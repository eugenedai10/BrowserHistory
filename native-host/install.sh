#!/bin/bash
# Registers the native messaging host with Chrome (and other Chromium browsers found on this Mac).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
HOST_NAME="com.chrome_history.logger"
EXT_ID="nbjpnjcebdfdinfjmiegjeokjkkibfcn"   # derived from the "key" in extension/manifest.json
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found"; exit 1; }

# Chrome launches the host with a minimal PATH, so pin the absolute python path in a launcher.
LAUNCHER="$HERE/host_launcher.sh"
printf '#!/bin/bash\nexec "%s" "%s/host.py"\n' "$PY" "$HERE" > "$LAUNCHER"
chmod +x "$LAUNCHER" "$HERE/host.py"

mkdir -p "$HOME/chrome-history"
chmod 700 "$HOME/chrome-history"

installed=0
for base in \
  "$HOME/Library/Application Support/Google/Chrome" \
  "$HOME/Library/Application Support/Chromium" \
  "$HOME/Library/Application Support/BraveSoftware/Brave-Browser" \
  "$HOME/Library/Application Support/Microsoft Edge"; do
  [ -d "$base" ] || continue
  dir="$base/NativeMessagingHosts"
  mkdir -p "$dir"
  cat > "$dir/$HOST_NAME.json" <<EOF
{
  "name": "$HOST_NAME",
  "description": "Chrome History Logger host",
  "path": "$LAUNCHER",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/"]
}
EOF
  echo "Installed host manifest: $dir/$HOST_NAME.json"
  installed=1
done

[ "$installed" = 1 ] || { echo "No Chromium-based browser profile directory found."; exit 1; }
echo "Logs will be written to: $HOME/chrome-history/YYYY-MM-DD.jsonl"
echo "Next: chrome://extensions -> Developer mode -> Load unpacked -> $(cd "$HERE/.." && pwd)/extension"
echo "      (extension ID will be $EXT_ID)"
