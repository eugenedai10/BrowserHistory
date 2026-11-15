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
  discord: {
    // Log the text of messages YOU post (never other people's messages)
    logPostText: true,
  },
};
