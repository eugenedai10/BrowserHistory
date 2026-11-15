#!/bin/bash
# Stops and removes the monitor launch agent. Logs in ~/chrome-history are left untouched.
set -euo pipefail
LABEL="com.chrome_history.monitor"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -fv "$PLIST"
echo "Monitor removed."
