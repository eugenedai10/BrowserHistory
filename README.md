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
| `discord_post` | channel info, length, text (your own posts only; `discord.logPostText` in `config.js`), confirmed |

Every event has `ts` (local time with offset), `type`, and, when it came from a tab, `tab` and `incognito`.

`dur_active_s` counts time when the tab was in front, in the focused window, and you weren't idle
(60s), or the tab was playing audio.

## Config
`extension/config.js`: `blocklist` (hostnames never logged), `stripQuery`, idle threshold, flush cadence,
`discord.logPostText`. Reload the extension after editing. The toolbar popup has a pause switch.

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
