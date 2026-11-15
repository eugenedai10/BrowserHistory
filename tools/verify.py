#!/usr/bin/env python3
"""Verify the integrity of ~/chrome-history (hash chain + append-only flags).

Usage: verify.py [LOG_DIR]        exit status 1 if tampering evidence is found

Reports edited lines, deleted/reordered lines, missing files, a truncated tail, legacy files that no longer
match the digest recorded at migration, unchained lines slipped into the chained region, and day files
that lack the append-only flag. It cannot detect an admin who removes the flags and rewrites the whole
chain including .head (see README).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "native-host"))
import chainlog  # noqa: E402

DEFAULT_DIR = os.environ.get("CHROME_HISTORY_DIR") or os.path.expanduser("~/chrome-history")


class Result:
    def __init__(self):
        self.errors = []     # tampering evidence
        self.warnings = []   # worth knowing, not proof of tampering
        self.lines = 0
        self.files = 0

    @property
    def ok(self):
        return not self.errors and not self.warnings

    def status(self):
        if self.errors:
            return f"TAMPERING DETECTED: {len(self.errors)} problem(s); run tools/verify.py"
        if self.warnings:
            return f"OK with {len(self.warnings)} warning(s) ({self.lines} lines); run tools/verify.py"
        return f"OK ({self.lines} lines in {self.files} files, hash chain intact, files append-only)"


def verify(log_dir=DEFAULT_DIR):
    r = Result()
    names = chainlog.day_files(log_dir)
    r.files = len(names)
    entries = []         # chained lines: (seq, file, lineno, obj)
    legacy_end = {}      # file -> byte offset where the unchained legacy region ends
    legacy_digest = {}   # file -> (bytes, sha256) recorded by chain_start

    for name in names:
        path = os.path.join(log_dir, name)
        if not chainlog.is_append_only(path):
            r.warnings.append(f"{name}: append-only flag is not set (chflags uappend)")
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as e:
            r.errors.append(f"{name}: cannot read ({e})")
            continue
        offset = 0
        for lineno, raw in enumerate(data.splitlines(keepends=True), 1):
            start, offset = offset, offset + len(raw)
            if not raw.strip():
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                r.errors.append(f"{name}:{lineno}: malformed line (not JSON)")
                continue
            if isinstance(d, dict) and all(k in d for k in ("seq", "prev", "h")):
                entries.append((d["seq"], name, lineno, d, start))
            else:
                # unchained: only legal inside the legacy region recorded by chain_start (checked below)
                legacy_end.setdefault(name, []).append((lineno, offset))

    # chain_start records which byte ranges are legacy (unchained) and their digests
    for seq, name, lineno, d, _start in entries:
        if d.get("type") == "chain_start":
            for item in d.get("legacy", []):
                legacy_digest[item["file"]] = (item["bytes"], item["sha256"])
        if d.get("type") == "chain_recovered":
            r.warnings.append(f"{name}:{lineno}: chain_recovered ({d.get('reason')}): the .head file had been "
                              f"deleted or damaged at {d.get('ts')}")

    for name, lines in legacy_end.items():
        allowed = legacy_digest.get(name, (0, None))[0]
        for lineno, end in lines:
            if end > allowed:
                r.errors.append(f"{name}:{lineno}: unchained line outside the legacy region (inserted or stripped of its hash)")

    for name, (nbytes, digest) in legacy_digest.items():
        path = os.path.join(log_dir, name)
        if not os.path.exists(path):
            r.errors.append(f"{name}: legacy file is missing")
        elif os.stat(path).st_size < nbytes or chainlog.sha256_prefix(path, nbytes) != digest:
            r.errors.append(f"{name}: legacy content changed since the chain started (first {nbytes} bytes differ)")

    # Within one file, appends always add increasing seq, so any decrease means lines were reordered.
    # (Across files order is free: a late event can land in an earlier day's file.)
    last_in_file = {}
    for seq, name, lineno, _d, _start in entries:   # entries are still in physical order here
        if name in last_in_file and seq <= last_in_file[name][0]:
            r.errors.append(f"{name}:{lineno}: lines out of order within the file (reordered): "
                            f"seq {seq} follows seq {last_in_file[name][0]}")
        last_in_file[name] = (seq, lineno)

    # ----- chain checks over all chained lines, ordered by seq (late events may land in an earlier day file)
    entries.sort(key=lambda e: e[0])
    r.lines += len(entries)
    prev_seq, prev_h = None, None
    for seq, name, lineno, d, _start in entries:
        loc = f"{name}:{lineno}"
        body = {k: v for k, v in d.items() if k != "h"}
        if chainlog.link_hash(d["prev"], body) != d["h"]:
            r.errors.append(f"{loc}: line was edited (seq {seq}: hash does not match its content)")
        if prev_seq is None:
            if seq != 1 or d["prev"] != chainlog.GENESIS:
                r.errors.append(f"{loc}: chain does not start at the beginning "
                                f"(first line is seq {seq}; earlier lines were deleted)")
        elif seq == prev_seq:
            r.errors.append(f"{loc}: duplicate seq {seq}")
        elif seq != prev_seq + 1:
            r.errors.append(f"{loc}: {seq - prev_seq - 1} line(s) missing before seq {seq} (deleted)")
        elif d["prev"] != prev_h:
            r.errors.append(f"{loc}: seq {seq} does not link to the previous line (tampered)")
        prev_seq, prev_h = seq, d["h"]

    # ----- head: catches a truncated tail or a deleted last file
    head = chainlog.load_head(log_dir)
    if not entries:
        if head and head["seq"] > 0:
            r.errors.append(f"head says seq {head['seq']} but no chained lines exist (all logs deleted?)")
    elif head is None:
        r.warnings.append("head file (.head) is missing or unreadable: a truncated tail cannot be detected")
    else:
        last_seq = entries[-1][0]
        match = next((e for e in entries if e[0] == head["seq"]), None)
        if head["seq"] > last_seq:
            r.errors.append(f"tail removed: head records seq {head['seq']}, logs end at seq {last_seq}")
        elif match is None or match[3]["h"] != head["h"]:
            r.errors.append(f"head does not match line seq {head['seq']} (tampered)")
    return r


def main():
    log_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    r = verify(log_dir)
    for e in r.errors:
        print("ERROR  ", e)
    for w in r.warnings:
        print("WARNING", w)
    print(r.status())
    sys.exit(1 if r.errors else 0)


if __name__ == "__main__":
    main()
