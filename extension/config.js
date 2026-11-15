// Loaded by background.js via importScripts. Defaults: log everything.
const CONFIG = {
  // Hostname substrings to never log, e.g. ["mybank.com", "mail.google.com"]
  blocklist: [],
  // true => strip ?query and #hash from logged URLs (search queries are still logged separately)
  stripQuery: false,
  // Seconds without input before chrome.idle reports "idle"
  idleSeconds: 60,
  // Native-host flush cadence
  flushIntervalMs: 5000,
  flushBatchSize: 25,
  // Liveness marker cadence (chrome.alarms cannot fire faster than every 30s)
  heartbeatSeconds: 30,
  // YouTube reminder (popup + notification) on today's watch time per kind: first at firstMinutes,
  // then again every everyMinutes after that.
  ytReminder: {
    video: { firstMinutes: 20, everyMinutes: 5 }, // long videos: 20, 25, 30, ...
    short: { firstMinutes: 10, everyMinutes: 2 }, // Shorts: 10, 12, 14, ...
  },
  discord: {
    // Log the text of messages YOU post (never other people's messages)
    logPostText: true,
  },
};
