#!/usr/bin/env python3
"""
Background collector for Claude usage data.

Primary source : local CLI session transcripts (~/.claude/projects/**/*.jsonl)
Optional source: Anthropic Admin API (if CLAUDE_ADMIN_API_KEY is set)

Saves to ~/.claude_usage_data/latest_summary.json every 5 minutes.
The Streamlit app reads this file on each page load.

Usage:
  python3 collector.py          # run once
  python3 collector.py --daemon # run continuously every 5 minutes
"""

import json
import glob
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

ADMIN_API_KEY = os.environ.get("CLAUDE_ADMIN_API_KEY", "")
CLAUDE_PROJECTS = os.path.join(os.path.expanduser("~/.claude"), "projects")
CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")
DATA_DIR = Path.home() / ".claude_usage_data"
SUMMARY_FILE = DATA_DIR / "latest_summary.json"
HISTORY_FILE = DATA_DIR / "usage_history.jsonl"
INTERVAL_SECS = 300  # 5 minutes


# ─── Local transcript parser (always works, zero cost) ───

def parse_all_transcripts():
    """Parse every JSONL transcript under ~/.claude/projects/ and return
    a list of records with timestamp and token counts."""
    records = []
    if not os.path.isdir(CLAUDE_PROJECTS):
        return records

    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            with open(path, "r") as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        msg = obj.get("message")
                        if not isinstance(msg, dict) or "usage" not in msg:
                            continue
                        u = msg["usage"]
                        ts = obj.get("timestamp") or obj.get("createdAt")
                        records.append({
                            "ts": ts,
                            "input": u.get("input_tokens", 0),
                            "output": u.get("output_tokens", 0),
                            "cache_create": u.get("cache_creation_input_tokens", 0),
                            "cache_read": u.get("cache_read_input_tokens", 0),
                            "model": msg.get("model", ""),
                        })
                    except (json.JSONDecodeError, TypeError):
                        continue
        except (IOError, PermissionError):
            continue
    return records


def parse_ts(ts):
    """Convert a raw timestamp (epoch-ms, epoch-s, or ISO string) to local naive datetime."""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
        # Parse ISO string and convert UTC to local time
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        # Convert aware UTC datetime to local naive datetime
        return dt.astimezone().replace(tzinfo=None)
    except (ValueError, OSError):
        return None


def get_weekly_reset():
    """Compute the next weekly reset time based on account creation date."""
    if not os.path.exists(CLAUDE_CONFIG):
        return None
    try:
        with open(CLAUDE_CONFIG, "r") as f:
            cfg = json.load(f)
        raw = cfg.get("firstStartTime") or cfg.get("claudeCodeFirstTokenDate")
        if not raw:
            return None
        origin = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        origin_local = origin.astimezone().replace(tzinfo=None)
        now = datetime.now()
        # Advance by 7-day intervals from origin until we're past now
        reset = origin_local
        while reset <= now:
            reset += timedelta(days=7)
        return reset
    except Exception:
        return None


def compute_local_summary(records):
    """Bucket records into session (5h), daily, and weekly windows.
    Also track oldest timestamp in each window for reset time calculation."""
    now = datetime.now()
    five_h_ago = now - timedelta(hours=5)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)

    buckets = {
        "session": {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost": 0.0},
        "daily":   {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost": 0.0},
        "weekly":  {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost": 0.0},
    }

    session_oldest = None
    session_newest = None

    for r in records:
        dt = parse_ts(r["ts"])
        inp = r["input"]
        out = r["output"]
        cc = r["cache_create"]
        cr = r["cache_read"]
        # Rough cost estimate (opus pricing per 1M tokens: $15 in / $75 out)
        cost = (inp * 15 + out * 75 + cc * 18.75 + cr * 1.875) / 1_000_000

        if dt is not None:
            if dt >= five_h_ago:
                b = buckets["session"]
                b["input_tokens"] += inp
                b["output_tokens"] += out
                b["cache_creation_tokens"] += cc
                b["cache_read_tokens"] += cr
                b["cost"] += cost
                if session_oldest is None or dt < session_oldest:
                    session_oldest = dt
                if session_newest is None or dt > session_newest:
                    session_newest = dt
            if dt >= day_start:
                b = buckets["daily"]
                b["input_tokens"] += inp
                b["output_tokens"] += out
                b["cache_creation_tokens"] += cc
                b["cache_read_tokens"] += cr
                b["cost"] += cost
            if dt >= week_start:
                b = buckets["weekly"]
                b["input_tokens"] += inp
                b["output_tokens"] += out
                b["cache_creation_tokens"] += cc
                b["cache_read_tokens"] += cr
                b["cost"] += cost

    # Session resets when the oldest call in the window falls off (oldest + 5h)
    if session_oldest:
        buckets["session_resets_at"] = (session_oldest + timedelta(hours=5)).isoformat()
        buckets["session_oldest"] = session_oldest.isoformat()
        buckets["session_newest"] = session_newest.isoformat()
    else:
        buckets["session_resets_at"] = None
        buckets["session_oldest"] = None
        buckets["session_newest"] = None

    # Weekly reset based on account creation cycle
    wr = get_weekly_reset()
    buckets["weekly_resets_at"] = wr.isoformat() if wr else None

    return buckets


def get_config_cost():
    """Read total cost from ~/.claude.json project stats."""
    if not os.path.exists(CLAUDE_CONFIG):
        return 0.0
    try:
        with open(CLAUDE_CONFIG, "r") as f:
            cfg = json.load(f)
        total = 0.0
        for data in cfg.get("projects", {}).values():
            total += data.get("lastCost", 0)
        return total
    except Exception:
        return 0.0


# ─── Admin API (optional, requires CLAUDE_ADMIN_API_KEY) ───

def fetch_admin_api(start_str, end_str, bucket_width="1h"):
    url = (
        "https://api.anthropic.com/v1/organizations/usage_report/messages?"
        "starting_at={s}&ending_at={e}&bucket_width={b}"
    ).format(s=start_str, e=end_str, b=bucket_width)
    headers = {"anthropic-version": "2023-06-01", "x-api-key": ADMIN_API_KEY}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def calc_api_totals(data):
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0, "cost": 0.0}
    for bucket in data.get("buckets", []):
        for result in bucket.get("results", []):
            totals["input_tokens"] += result.get("input_tokens", 0)
            totals["output_tokens"] += result.get("output_tokens", 0)
            totals["cache_creation_tokens"] += result.get("cache_creation_input_tokens", 0)
            totals["cache_read_tokens"] += result.get("cache_read_input_tokens", 0)
            totals["cost"] += result.get("cost", 0.0)
    return totals


def collect_from_admin_api():
    now = datetime.now(timezone.utc)
    end_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    sess_start = (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "session": calc_api_totals(fetch_admin_api(sess_start, end_str, "1h")),
        "daily": calc_api_totals(fetch_admin_api(day_start, end_str, "1h")),
        "weekly": calc_api_totals(fetch_admin_api(week_start, end_str, "1d")),
    }


# ─── Main ───

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("[{ts}] {msg}".format(ts=ts, msg=msg))


def collect():
    source = "local"
    buckets = None

    # Try Admin API first if key is available
    if ADMIN_API_KEY:
        try:
            log("Trying Admin API...")
            buckets = collect_from_admin_api()
            source = "admin_api"
            log("Admin API OK.")
        except Exception as e:
            log("Admin API failed ({e}), falling back to local.".format(e=e))
            buckets = None

    # Fall back to local transcripts
    if buckets is None:
        log("Parsing local transcripts...")
        records = parse_all_transcripts()
        log("Found {n} API response records.".format(n=len(records)))
        buckets = compute_local_summary(records)
        source = "local"

    summary = {
        "timestamp": datetime.now().isoformat(),
        "source": source,
        "session": buckets["session"],
        "daily": buckets["daily"],
        "weekly": buckets["weekly"],
        "session_resets_at": buckets.get("session_resets_at"),
        "session_oldest": buckets.get("session_oldest"),
        "session_newest": buckets.get("session_newest"),
        "weekly_resets_at": buckets.get("weekly_resets_at"),
        "config_cost": get_config_cost(),
    }

    DATA_DIR.mkdir(exist_ok=True)
    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f, indent=2)
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps(summary) + "\n")

    log("Saved to {f} (source: {s})".format(f=SUMMARY_FILE, s=source))
    for period in ("session", "daily", "weekly"):
        b = buckets[period]
        log("  {p:>7}: {inp} in / {out} out / ${cost:.4f}".format(
            p=period.title(),
            inp=b["input_tokens"], out=b["output_tokens"], cost=b["cost"],
        ))


def main():
    daemon = "--daemon" in sys.argv

    if daemon:
        log("Starting collector daemon (every {s}s)...".format(s=INTERVAL_SECS))
        log("Source: {s}".format(s="Admin API + local fallback" if ADMIN_API_KEY else "Local transcripts only"))
        while True:
            try:
                collect()
            except Exception as e:
                log("ERROR: {e}".format(e=e))
            time.sleep(INTERVAL_SECS)
    else:
        collect()


if __name__ == "__main__":
    main()
