# Chrome History Logger

Chrome extension (Manifest V3) + native messaging host. Logs your browsing to
`~/chrome-history/YYYY-MM-DD.jsonl` (one JSON event per line, files 0600).

## Install
```bash
./native-host/install.sh          # registers the host with Chrome (and Brave/Chromium/Edge if present)
```
1. Open `chrome://extensions`, enable **Developer mode**, **Load unpacked**, pick the `extension/` folder.
   The extension ID is pinned to `nbjpnjcebdfdinfjmiegjeokjkkibfcn` (from the `key` in `manifest.json`).
2. Click the toolbar icon: it should say **Native host: connected**.
3. Optional: to also log incognito, tick **Allow in Incognito** on the extension's details page. Those events carry `incognito: true`.

Uninstall: `./native-host/uninstall.sh` and remove the extension. Logs stay in `~/chrome-history`.

## What is logged
| type | fields |
|---|---|
| `visit` | url, title, referrer, opener_url, start, end, dur_total_s, dur_active_s, max_scroll_pct, words, est_read_min, reason |
| `click` | page_url, href, text, new_tab, external, (YouTube: yt_source, yt_video_id) |
| `search` | engine, query, url |
| `yt_start` `yt_pause` `yt_resume` `yt_seek` `yt_end` | video_id, kind (video/short), title, channel, length_s, position_s (seek: from_s/to_s) |
| `yt_session` | watched_s, max_position_s, pct_watched, finished, pauses, seeks, loops, ads, avg_speed, quality, captions, liked, subscribed, source (home/sidebar/search/autoplay/shorts_swipe/...), partial |
| `discord_channel` | guild/channel ids + names, dm, enter, leave, dur_active_s, messages_seen (content is NOT logged) |
| `host_start` `host_stop` | written by the native host when the extension connects/disconnects. `host_stop.cause` is `browser_exit` (Chrome itself went away within 5s: quit or crash) or `extension_stopped` (Chrome still running: extension reloaded, disabled, removed, or its worker restarted) |
| `logger_start` | extension worker started (cause: browser_start / extension_reload / worker_restart / install / enabled) |
| (all lines) | `seq`, `prev`, `h`: hash-chain fields added by the writer (see Protecting the logs) |
| `heartbeat` | every 30s (`heartbeatSeconds` in config.js): tracked_tabs, queue, idle, focused, host_ok, incognito_allowed |
| `monitor_start` `monitor_heartbeat` | written by the external monitor (below): its start (cause login/restart) and a 60s liveness beat with `chrome_running`, `extension_connected` |
| `browser_start` `browser_stop` | external monitor: Chrome's main process appeared/disappeared (`chrome_pid`, `observed_late`, `ran_s`) |
| `extension_missing` `extension_present` | external monitor: Chrome is running but the logger's native host is not connected for >15s (extension disabled/removed), and when it returns (`missing_s`) |
| `system_sleep` `system_wake` | external monitor: the Mac slept (inferred from a wall-clock vs uptime-clock jump; `asleep_s`) |
| `discord_post` | channel info, length, text (your own posts only; `discord.logPostText` in `config.js`), confirmed |

Every event has `ts` (local time with offset), `type`, and, when it came from a tab, `tab` and `incognito`.

`dur_active_s` counts time when the tab was in front, in the focused window, and you weren't idle
(60s), or the tab was playing audio.

## External monitor (optional, recommended)
Chrome starts the native host, so the host alone cannot tell you when Chrome was closed, when the Mac slept,
or that Chrome is open with the extension disabled. `monitor/monitor.py` is a separate launchd agent that watches
Chrome from outside and writes those facts into the same log files.
```bash
./monitor/install.sh        # LaunchAgent: starts at login, restarted if it dies
./monitor/uninstall.sh
python3 monitor/test_monitor.py
```
`tools/summary.py` then labels each gap as **machine asleep**, **browser closed**, or **Chrome running, logger extension
not connected**, and prints a "Browser sessions" block with how long Chrome was open but not logged. Without monitor
data it falls back to the host-based labels below.

Limits: it detects but cannot prevent quitting Chrome or disabling the extension; a clean quit and a crash look the same
from outside; the repo path is baked into the plist, so re-run `install.sh` if you move the repo. Chrome only by default
(edit `CHROME_RE` in `monitor/monitor.py` for Brave/Edge).

## Protecting the logs
Two layers make the logs hard to change by accident and make tampering visible:
1. **Append-only flags.** Every day file is marked `uappend` (`chflags`) as soon as it is created. Editors, `rm`, `mv`,
   `>` and `sed -i` fail with "Operation not permitted"; the logger can still append.
2. **Hash chain.** Every line carries `seq`, `prev` and `h` (a hash of the line and the previous hash) across all days,
   with the head in `~/chrome-history/.head`. `python3 tools/verify.py` reports edited, deleted, reordered or
   truncated lines, missing files, and legacy (pre-chain) files whose content changed. `tools/summary.py` prints the
   result as "Log integrity" on every run. Logs from before this feature are covered by a digest recorded in the
   first `chain_start` line.

Archive by copying (`cp`). Deleting or editing a day file is reported as tampering on purpose, so to prune old logs you
must first `chflags nouappend FILE`, and `verify` will keep flagging the gap.

**Limits.** This is not a lock. As an admin you (or anyone with your password) can clear the flag with
`chflags nouappend`, and a script that rewrites every later line, `.head` included, defeats the chain. For stronger
guarantees use a root-owned logging daemon or an off-machine append-only copy. Run `python3 native-host/test_chainlog.py`
for the tests.

## Config
`extension/config.js`: `blocklist` (hostnames never logged), `stripQuery`, idle threshold, flush cadence,
`discord.logPostText`. Reload the extension after editing.

There is deliberately no pause switch: logging is always on.

## Always on
The extension keeps one persistent native-messaging port to the host, which also keeps the MV3 service
worker from sleeping. If the port drops it reconnects with backoff; events stay queued until the host
acknowledges them. The icon shows a red `!` while the host is unreachable.

It cannot log while Chrome is closed, and you can still disable it at `chrome://extensions`. Both show up as
gaps: `tools/summary.py` lists silences over 1 min (`GAP_SECONDS` env var to change) and says why:
- **browser closed**: `host_stop` with cause `browser_exit`, or the next start was `browser_start`.
- **extension stopped, browser still running**: cause `extension_stopped`, refined by how it came back
  (reloaded / disabled then re-enabled / re-installed / worker restarted).
- **browser closed uncleanly**: no `host_stop` but the next start was `browser_start` (crash, kill, power loss or sleep).
- **UNEXPECTED**: no `host_stop` and no browser restart (machine sleep, or the extension/host died).

The same applies to the end of the log: the last `host_stop` is reported as browser closed or extension stopped.

## Summary
```bash
python3 tools/summary.py            # today
python3 tools/summary.py 2026-11-13
```

## Notes
- Defaults log everything, including full URLs (which can contain tokens). Use `blocklist`/`stripQuery` if that matters.
- If the native host is unavailable, events queue in extension storage (max 20k) and are delivered later; the icon shows `!`.
- YouTube/Discord DOM selectors are at the top of each content script; if the sites change, titles/names may come back empty while visits and clicks keep working.
- Discord: web app only (not the desktop client).
