#!/bin/bash
# Installs the monitor as a per-user launchd agent (starts at login, restarted if it dies).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.chrome_history.monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found"; exit 1; }
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/chrome-history"
chmod 700 "$HOME/chrome-history"
chmod +x "$HERE/monitor.py"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$HERE/monitor.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardErrorPath</key><string>$HOME/chrome-history/monitor.err</string>
  <key>StandardOutPath</key><string>/dev/null</string>
</dict>
</plist>
PLISTEOF

# Reload if already loaded, then start.
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1 || true

echo "Installed $PLIST"
echo "Status:  launchctl print $DOMAIN/$LABEL | grep -E 'state|pid'"
echo "Logs:    ~/chrome-history/YYYY-MM-DD.jsonl  (errors: ~/chrome-history/monitor.err)"
echo "Remove:  $HERE/uninstall.sh"
