#!/bin/bash
# Removes the native host registration. Your logs in ~/chrome-history are left untouched.
set -euo pipefail
HOST_NAME="com.chrome_history.logger"
for base in \
  "$HOME/Library/Application Support/Google/Chrome" \
  "$HOME/Library/Application Support/Chromium" \
  "$HOME/Library/Application Support/BraveSoftware/Brave-Browser" \
  "$HOME/Library/Application Support/Microsoft Edge"; do
  f="$base/NativeMessagingHosts/$HOST_NAME.json"
  [ -f "$f" ] && rm -v "$f"
done
rm -f "$(cd "$(dirname "$0")" && pwd)/host_launcher.sh"
echo "Done. Remove the extension at chrome://extensions; logs remain in ~/chrome-history."
