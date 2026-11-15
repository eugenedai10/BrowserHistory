"""Tamper-evident, append-only log writer shared by the native host and the monitor.

Every line written gets three extra fields:
    seq   global sequence number (+1 per line, across all day files)
    prev  hash of the previous line
    h     sha256(prev + "\\n" + canonical_json(line without h))[:32]
so editing, deleting or reordering lines breaks the chain, and tools/verify.py reports it.
Each day file is also marked append-only (chflags uappend): editors, rm, mv and truncation fail,
appending still works. An admin can undo the flag and rewrite the whole chain; this raises the bar
and makes casual/partial tampering visible, it does not make tampering impossible.

All writers take an exclusive flock on <dir>/.lock, so host and monitor can append concurrently.
"""
import datetime
import fcntl
import hashlib
import json
import os
import re
import stat

DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
GENESIS = "0" * 32
HEAD = ".head"
LOCK = ".lock"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def link_hash(prev, obj):
    """Hash of a line (obj must not contain 'h')."""
    return hashlib.sha256((prev + "\n" + canonical(obj)).encode("utf-8")).hexdigest()[:32]


def day_files(log_dir):
    try:
        return sorted(n for n in os.listdir(log_dir) if DAY_FILE_RE.match(n))
    except FileNotFoundError:
        return []


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")


def set_append_only(path):
    """UF_APPEND, so the file can only grow. Never chmod/chown an append-only file (the OS refuses)."""
    try:
        os.chflags(path, os.stat(path).st_flags | stat.UF_APPEND)
    except (OSError, AttributeError):
        pass  # unsupported platform/filesystem: the hash chain still applies


def is_append_only(path):
    try:
        return bool(os.stat(path).st_flags & stat.UF_APPEND)
    except (OSError, AttributeError):
        return False


# ---------- head ----------
def load_head(log_dir):
    try:
        with open(os.path.join(log_dir, HEAD), encoding="utf-8") as f:
            h = json.load(f)
        if isinstance(h.get("seq"), int) and isinstance(h.get("h"), str):
            return h
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _save_head(log_dir, head):
    tmp = os.path.join(log_dir, HEAD + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(head, separators=(",", ":")).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(log_dir, HEAD))


# ---------- reading existing chained lines ----------
def read_chained(log_dir):
    """[(seq, h, file, lineno)] for every line that already carries chain fields."""
    out = []
    for name in day_files(log_dir):
        try:
            with open(os.path.join(log_dir, name), "rb") as f:
                for i, raw in enumerate(f, 1):
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(d, dict) and isinstance(d.get("seq"), int) and isinstance(d.get("h"), str):
                        out.append((d["seq"], d["h"], name, i))
        except OSError:
            continue
    return out


def sha256_prefix(path, nbytes):
    h = hashlib.sha256()
    left = nbytes
    with open(path, "rb") as f:
        while left > 0:
            chunk = f.read(min(1 << 20, left))
            if not chunk:
                break
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def _bootstrap(log_dir):
    """Called when .head is missing/unreadable. Returns (head, [marker events to chain first])."""
    chained = read_chained(log_dir)
    now = _now_iso()
    if chained:
        # Head file was deleted or damaged: continue from the highest line, and record that it happened.
        seq, h, _f, _l = max(chained)
        marker = {"ts": now, "type": "chain_recovered", "reason": "head_missing", "from_seq": seq}
        return {"seq": seq, "h": h, "day": now[:10]}, [marker]
    # First use (or upgrade from unchained logs): record a digest of everything that already exists,
    # so legacy history is covered too, and seal the existing files.
    legacy = []
    for name in day_files(log_dir):
        path = os.path.join(log_dir, name)
        size = os.stat(path).st_size
        legacy.append({"file": name, "bytes": size, "sha256": sha256_prefix(path, size)})
        set_append_only(path)
    marker = {"ts": now, "type": "chain_start", "legacy": legacy}
    return {"seq": 0, "h": GENESIS, "day": now[:10]}, [marker]


# ---------- append ----------
def _ends_with_newline(path):
    try:
        size = os.stat(path).st_size
        if size == 0:
            return True
        fd = os.open(path, os.O_RDONLY)
        try:
            return os.pread(fd, 1, size - 1) == b"\n"
        finally:
            os.close(fd)
    except OSError:
        return True


def append(log_dir, items):
    """Append [(day 'YYYY-MM-DD', event dict), ...] as chained lines. Returns the number written."""
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    lock_fd = os.open(os.path.join(log_dir, LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        head = load_head(log_dir)
        pre = []
        if head is None:
            head, markers = _bootstrap(log_dir)
            pre = [(m["ts"][:10], m) for m in markers]
        seq, prev = head["seq"], head["h"]
        by_day = {}
        order = []
        for day, ev in pre + list(items):
            ev = {k: v for k, v in ev.items() if k not in ("seq", "prev", "h")}
            seq += 1
            ev["seq"], ev["prev"] = seq, prev
            prev = link_hash(prev, ev)
            ev["h"] = prev
            if day not in by_day:
                by_day[day] = []
                order.append(day)
            by_day[day].append(json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
        for day in order:
            path = os.path.join(log_dir, day + ".jsonl")
            payload = ("\n".join(by_day[day]) + "\n").encode("utf-8")
            if not _ends_with_newline(path):
                payload = b"\n" + payload  # torn earlier write: keep the damage confined to one line
            fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            set_append_only(path)
        _save_head(log_dir, {"seq": seq, "h": prev, "day": order[-1] if order else head.get("day")})
        return seq - head["seq"] - len(pre)
    finally:
        os.close(lock_fd)  # also releases the flock
