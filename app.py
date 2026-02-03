import streamlit as st
import json
import os
import glob
import subprocess
import signal
from datetime import datetime, timedelta, time
from pathlib import Path

# --- Constants ---
WINDOW_HOURS = 5
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(APP_DIR, "session_data.json")
CLAUDE_HOME = os.path.expanduser("~/.claude")
CLAUDE_CONFIG = os.path.expanduser("~/.claude.json")
CLAUDE_PROJECTS = os.path.join(CLAUDE_HOME, "projects")
AUTO_REFRESH_SECS = 300  # 5 minutes (synced with collector)
COLLECTOR_SUMMARY = os.path.join(os.path.expanduser("~"), ".claude_usage_data", "latest_summary.json")
SESS_CAP_DEFAULT = 40.0    # Session (5h) usage cap in API-$ equivalent
WEEK_CAP_DEFAULT = 96.0    # Weekly usage cap in API-$ equivalent


# --- Persistence ---

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)


# --- Claude local data readers (zero quota cost) ---

def read_claude_config():
    """Read ~/.claude.json for per-project token stats and account info."""
    if not os.path.exists(CLAUDE_CONFIG):
        return {}
    try:
        with open(CLAUDE_CONFIG, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        return {}


def parse_session_transcripts():
    """Parse all JSONL session transcripts for token usage with timestamps.

    Returns a list of dicts: {timestamp, input, output, cache_read, cache_create, model}
    """
    records = []
    if not os.path.isdir(CLAUDE_PROJECTS):
        return records

    for jsonl_path in glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            with open(jsonl_path, "r") as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        msg = obj.get("message", {})
                        if not isinstance(msg, dict) or "usage" not in msg:
                            continue
                        u = msg["usage"]
                        # Extract timestamp from the top-level object
                        ts = obj.get("timestamp") or obj.get("createdAt")

                        records.append({
                            "timestamp": ts,
                            "input": u.get("input_tokens", 0),
                            "output": u.get("output_tokens", 0),
                            "cache_read": u.get("cache_read_input_tokens", 0),
                            "cache_create": u.get("cache_creation_input_tokens", 0),
                            "model": msg.get("model", "unknown"),
                        })
                    except (json.JSONDecodeError, TypeError):
                        continue
        except (IOError, PermissionError):
            continue

    return records


def compute_usage_stats(records):
    """Compute token stats for current session window and this week."""
    now = datetime.now()
    five_hours_ago = now - timedelta(hours=5)
    week_start = now - timedelta(days=now.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

    session_tokens = 0
    week_tokens = 0
    total_tokens = 0
    session_calls = 0
    week_calls = 0
    total_calls = 0

    for r in records:
        t = r["input"] + r["output"] + r["cache_read"] + r["cache_create"]
        total_tokens += t
        total_calls += 1

        ts = r.get("timestamp")
        if ts is None:
            continue

        # Handle both epoch-ms and ISO timestamps, convert UTC to local
        try:
            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts)
            else:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                dt = dt.astimezone().replace(tzinfo=None)
        except (ValueError, OSError):
            continue

        if dt >= five_hours_ago:
            session_tokens += t
            session_calls += 1
        if dt >= week_start:
            week_tokens += t
            week_calls += 1

    return {
        "session_tokens": session_tokens,
        "session_calls": session_calls,
        "week_tokens": week_tokens,
        "week_calls": week_calls,
        "total_tokens": total_tokens,
        "total_calls": total_calls,
    }


def get_project_costs(config):
    """Sum up cost data from all projects in ~/.claude.json."""
    projects = config.get("projects", {})
    total_cost = 0.0
    project_stats = []
    for path, data in projects.items():
        cost = data.get("lastCost", 0)
        total_cost += cost
        inp = data.get("lastTotalInputTokens", 0)
        out = data.get("lastTotalOutputTokens", 0)
        project_stats.append({
            "path": path,
            "cost": cost,
            "input": inp,
            "output": out,
        })
    return total_cost, project_stats


COLLECTOR_SCRIPT = os.path.join(APP_DIR, "collector.py")
COLLECTOR_PID_FILE = os.path.join(APP_DIR, ".collector.pid")


def get_collector_pid():
    """Check if collector daemon is running. Returns PID or None."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "collector.py --daemon"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split()
        if pids:
            return int(pids[0])
    except Exception:
        pass
    return None


def start_collector():
    """Start the collector daemon as a background process."""
    proc = subprocess.Popen(
        ["python3", COLLECTOR_SCRIPT, "--daemon"],
        stdout=open(os.path.join(APP_DIR, "collector.log"), "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    # Save PID for reference
    with open(COLLECTOR_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    return proc.pid


def stop_collector():
    """Stop all collector daemon processes."""
    killed = False
    # Kill all matching PIDs individually
    try:
        result = subprocess.run(
            ["pgrep", "-f", "collector.py --daemon"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split():
            try:
                os.kill(int(line), signal.SIGTERM)
                killed = True
            except (OSError, ValueError):
                pass
    except Exception:
        pass
    # Also pkill as fallback
    try:
        subprocess.run(
            ["pkill", "-f", "collector.py --daemon"],
            capture_output=True, timeout=5,
        )
        killed = True
    except Exception:
        pass
    # Clean up PID file
    if os.path.exists(COLLECTOR_PID_FILE):
        try:
            os.remove(COLLECTOR_PID_FILE)
        except OSError:
            pass
    return killed


def read_collector_summary():
    """Read latest_summary.json produced by collector.py (Admin API data).

    Returns dict with keys: session, daily, weekly (each has input_tokens,
    output_tokens, cache_creation_tokens, cache_read_tokens, cost),
    plus 'timestamp'. Returns None if file not found or stale.
    """
    if not os.path.exists(COLLECTOR_SUMMARY):
        return None
    try:
        with open(COLLECTOR_SUMMARY, "r") as f:
            data = json.load(f)
        # Check staleness: if older than 15 minutes treat as stale
        ts = data.get("timestamp", "")
        if ts:
            try:
                collected_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = datetime.now(collected_at.tzinfo) - collected_at
                data["_age_minutes"] = int(age.total_seconds() / 60)
                data["_stale"] = age.total_seconds() > 900  # > 15 min
            except Exception:
                data["_age_minutes"] = -1
                data["_stale"] = True
        return data
    except (json.JSONDecodeError, ValueError, IOError):
        return None


# --- Time helpers ---

def get_remaining(session_start):
    window_end = session_start + timedelta(hours=WINDOW_HOURS)
    return max(window_end - datetime.now(), timedelta(0))


def fmt(td):
    total = int(td.total_seconds())
    if total < 0:
        return "0h 0m 0s"
    h, r = divmod(total, 3600)
    m, s = divmod(r, 60)
    return "{h}h {m}m {s}s".format(h=h, m=m, s=s)


def effective_cost(d):
    """Compute effective cost weighting cache-reads at 10% (they barely
    count toward rate limits)."""
    inp = d.get("input_tokens", 0)
    out = d.get("output_tokens", 0)
    cc = d.get("cache_creation_tokens", 0)
    cr = d.get("cache_read_tokens", 0)
    return (inp * 15 + out * 75 + cc * 18.75 + cr * 0.1875) / 1_000_000


def fmt_tokens(n):
    if n >= 1_000_000:
        return "{:.1f}M".format(n / 1_000_000)
    if n >= 1_000:
        return "{:.1f}K".format(n / 1_000)
    return str(n)


def compute_all_sessions(session_start):
    today_end = session_start.replace(hour=23, minute=59, second=59, microsecond=0)
    sessions = []
    block = session_start
    while block < today_end:
        end = block + timedelta(hours=WINDOW_HOURS)
        sessions.append((block, min(end, today_end)))
        block = end
    return sessions


# --- Colors ---
CLR_EXPIRED = "#1A237E"
CLR_EXPIRED_BG = "#E8EAF6"
CLR_ACTIVE = "#F9A825"
CLR_ACTIVE_BG = "#FFF8E1"
CLR_UPCOMING = "#2E7D32"
CLR_UPCOMING_BG = "#E8F5E9"
CLR_FREE = "#E0E0E0"

# --- Page config ---
st.set_page_config(page_title="Claude Session Scheduler", page_icon="⏱", layout="wide")

# --- Load persisted data & determine dashboard state ---
persisted = load_data()
if "session_start" not in st.session_state and "session_start" in persisted:
    try:
        st.session_state["session_start"] = datetime.fromisoformat(persisted["session_start"])
    except ValueError:
        pass

# Dashboard active = collector daemon is running
dashboard_active = get_collector_pid() is not None

# Only auto-refresh when dashboard is active (refresh data every 5 min)
if dashboard_active:
    st.markdown(
        '<meta http-equiv="refresh" content="{secs}">'.format(secs=AUTO_REFRESH_SECS),
        unsafe_allow_html=True,
    )

# --- Live countdown + clock JavaScript ---
# This runs client-side and ticks every second regardless of Streamlit reruns.
# Use the real session reset time from collector if available, else from manual session start.
_window_end_epoch_ms = 0
_col_data = read_collector_summary() if dashboard_active else None
_sess_resets = _col_data.get("session_resets_at") if _col_data else None
if _sess_resets:
    try:
        _we = datetime.fromisoformat(_sess_resets)
        _window_end_epoch_ms = int(_we.timestamp() * 1000)
    except (ValueError, TypeError):
        pass
if _window_end_epoch_ms == 0:
    _session_start_val = st.session_state.get("session_start")
    if _session_start_val:
        _we = _session_start_val + timedelta(hours=WINDOW_HOURS)
        _window_end_epoch_ms = int(_we.timestamp() * 1000)

# Weekly reset epoch for JS countdown
_week_reset_epoch_ms = 0
_week_resets = _col_data.get("weekly_resets_at") if _col_data else None
if _week_resets:
    try:
        _wr = datetime.fromisoformat(_week_resets)
        _week_reset_epoch_ms = int(_wr.timestamp() * 1000)
    except (ValueError, TypeError):
        pass

# st.markdown strips <script> tags. Use st.components.v1.html() instead —
# it renders in an iframe that executes JS, and uses parent.document to
# reach into the Streamlit page and update elements.
import streamlit.components.v1 as components

_timer_js = '''<script>
(function() {{
  var windowEndMs = {window_end};
  var weekResetMs = {week_end};
  var doc = window.parent.document;

  function pad(n) {{ return n < 10 ? '0' + n : '' + n; }}
  function fmtCD(ms) {{
    var h = Math.floor(ms / 3600000);
    var m = Math.floor((ms % 3600000) / 60000);
    var s = Math.floor((ms % 60000) / 1000);
    return h + 'h ' + pad(m) + 'm ' + pad(s) + 's';
  }}

  function tick() {{
    var now = new Date();

    // Live clock
    var clockEl = doc.getElementById('live-clock');
    if (clockEl) {{
      var d = now.toLocaleDateString('en-US', {{weekday:'long', month:'long', day:'numeric'}});
      var t = now.toLocaleTimeString('en-US', {{hour:'2-digit', minute:'2-digit', second:'2-digit'}});
      clockEl.textContent = d + '  ' + t;
    }}

    // Session countdown
    if (windowEndMs > 0) {{
      var diff = windowEndMs - now.getTime();
      var expired = diff <= 0;
      var cd = expired ? '0h 00m 00s' : fmtCD(diff);

      var els = doc.querySelectorAll('.live-remaining');
      for (var i = 0; i < els.length; i++) {{
        els[i].textContent = cd;
        if (expired) els[i].style.color = '#1A237E';
      }}

      var totalMs = {total_ms};
      var elapsed = totalMs - diff;
      var pct = Math.min(Math.max(elapsed / totalMs * 100, 0), 100);
      var barEl = doc.getElementById('live-progress-bar');
      var pctEl = doc.getElementById('live-progress-pct');
      if (barEl) {{ barEl.style.width = pct.toFixed(1) + '%'; if (pct > 80) barEl.style.background = '#EF6C00'; }}
      if (pctEl) pctEl.textContent = pct.toFixed(1) + '%';

      var bannerEl = doc.getElementById('live-banner');
      if (bannerEl) {{
        if (expired) {{
          bannerEl.innerHTML = 'Window expired. You can start a new session.';
          bannerEl.style.color = '#1A237E'; bannerEl.style.background = '#E8EAF6'; bannerEl.style.borderLeftColor = '#1A237E';
        }} else {{
          bannerEl.innerHTML = 'Current session has <strong style="color:#F9A825;">' + cd + '</strong> remaining';
        }}
      }}

      var srEl = doc.getElementById('live-sess-reset');
      if (srEl) {{
        if (expired) {{ srEl.textContent = 'reset (window clear)'; }}
        else {{
          var et = new Date(windowEndMs).toLocaleTimeString('en-US', {{hour:'numeric', minute:'2-digit'}});
          srEl.textContent = 'resets ' + et + ' (' + cd + ')';
        }}
      }}
    }}

    // Weekly countdown
    if (weekResetMs > 0) {{
      var wdiff = weekResetMs - now.getTime();
      var wrEl = doc.getElementById('live-week-reset');
      if (wrEl && wdiff > 0) {{
        var wd = Math.floor(wdiff / 86400000);
        var wh = Math.floor((wdiff % 86400000) / 3600000);
        var wm = Math.floor((wdiff % 3600000) / 60000);
        var ws = Math.floor((wdiff % 60000) / 1000);
        var rd = new Date(weekResetMs);
        var dn = rd.toLocaleDateString('en-US', {{weekday:'short', month:'short', day:'numeric'}});
        var rt = rd.toLocaleTimeString('en-US', {{hour:'numeric', minute:'2-digit'}});
        if (wd > 0) wrEl.textContent = 'resets ' + dn + ' ' + rt + ' (' + wd + 'd ' + wh + 'h ' + pad(wm) + 'm)';
        else wrEl.textContent = 'resets today ' + rt + ' (' + wh + 'h ' + pad(wm) + 'm ' + pad(ws) + 's)';
      }}
    }}
  }}

  tick();
  setInterval(tick, 1000);
}})();
</script>'''.format(
    window_end=_window_end_epoch_ms,
    week_end=_week_reset_epoch_ms,
    total_ms=WINDOW_HOURS * 3600 * 1000,
)
components.html(_timer_js, height=0)

# --- Read data sources (only when active, otherwise use cached summary) ---
claude_config = read_claude_config()
account = claude_config.get("oauthAccount", {})

if dashboard_active:
    records = parse_session_transcripts()
    local_stats = compute_usage_stats(records)
    total_cost, project_stats = get_project_costs(claude_config)
    collector = read_collector_summary()
else:
    # When stopped: read last cached summary only (no parsing, no CPU)
    local_stats = {"session_tokens": 0, "session_calls": 0, "week_tokens": 0,
                   "week_calls": 0, "total_tokens": 0, "total_calls": 0}
    total_cost = 0.0
    project_stats = []
    collector = read_collector_summary()  # just read the file, no parsing

# Check credential for subscription type
creds_path = os.path.join(CLAUDE_HOME, ".credentials.json")
sub_type = "Pro"
if os.path.exists(creds_path):
    try:
        with open(creds_path, "r") as f:
            creds = json.load(f)
        sub_type = creds.get("claudeAiOauth", {}).get("subscriptionType", "unknown").title()
    except Exception:
        pass


# ==================== Sidebar ====================
with st.sidebar:
    now_display = datetime.now().strftime("%A, %B %d  %I:%M:%S %p")
    st.markdown(
        '<div style="text-align:center;padding:12px 0 8px;border-bottom:1px solid #ddd;margin-bottom:16px;">'
        '<div style="font-size:10px;color:#999;text-transform:uppercase;letter-spacing:1.5px;">Local Time</div>'
        '<div id="live-clock" style="font-size:16px;font-weight:600;color:#333;margin-top:2px;">{t}</div>'
        '</div>'.format(t=now_display),
        unsafe_allow_html=True,
    )

    # ---- Master Start / Stop ----
    if dashboard_active:
        collector_pid = get_collector_pid()
        st.markdown(
            '<div style="background:#E8F5E9;border:1px solid #A5D6A7;border-radius:8px;'
            'padding:12px 16px;text-align:center;margin-bottom:12px;">'
            '<div style="font-size:10px;color:#2E7D32;text-transform:uppercase;'
            'letter-spacing:1.5px;font-weight:600;">Dashboard Active</div>'
            '<div style="font-size:12px;color:#555;margin-top:4px;">'
            'Collector PID {pid} &middot; Refreshing every 5 min</div>'
            '</div>'.format(pid=collector_pid),
            unsafe_allow_html=True,
        )
        if os.path.exists(COLLECTOR_SUMMARY):
            try:
                with open(COLLECTOR_SUMMARY, "r") as _f:
                    _ts = json.load(_f).get("timestamp", "")
                st.caption("Last data: " + _ts[:19])
            except Exception:
                pass

        if st.button("Stop Dashboard", use_container_width=True, type="secondary",
                      help="Stops the collector daemon and auto-refresh. Zero CPU after this."):
            stop_collector()
            # Clear session tracking too
            st.session_state.pop("session_start", None)
            d = load_data()
            d.pop("session_start", None)
            save_data(d)
            st.rerun()
    else:
        st.markdown(
            '<div style="background:#FFF3E0;border:1px solid #FFE0B2;border-radius:8px;'
            'padding:12px 16px;text-align:center;margin-bottom:12px;">'
            '<div style="font-size:10px;color:#E65100;text-transform:uppercase;'
            'letter-spacing:1.5px;font-weight:600;">Dashboard Stopped</div>'
            '<div style="font-size:12px;color:#888;margin-top:4px;">'
            'No background processes running</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if st.button("Start Dashboard", use_container_width=True, type="primary",
                      help="Starts the collector daemon, begins session tracking, and enables auto-refresh."):
            start_collector()
            st.session_state["session_start"] = datetime.now()
            d = load_data()
            d["session_start"] = st.session_state["session_start"].isoformat()
            save_data(d)
            st.rerun()

    st.divider()

    # ---- Session controls (only when active) ----
    if dashboard_active:
        st.header("Session Controls")

        if st.button("Start New Session", use_container_width=True):
            st.session_state["session_start"] = datetime.now()
            d = load_data()
            d["session_start"] = st.session_state["session_start"].isoformat()
            save_data(d)
            st.rerun()

        st.divider()

        st.subheader("Manual Entry")
        with st.form("manual_time_form", clear_on_submit=False):
            col_d, col_h, col_m = st.columns(3)
            with col_d:
                manual_date = st.date_input(
                    "Date",
                    value=datetime.now().date(),
                    key="manual_date_input",
                )
            with col_h:
                manual_hour = st.selectbox(
                    "Hour",
                    options=list(range(0, 24)),
                    index=datetime.now().hour,
                    key="manual_hour_input",
                )
            with col_m:
                manual_minute = st.selectbox(
                    "Minute",
                    options=list(range(0, 60)),
                    index=datetime.now().minute,
                    key="manual_minute_input",
                )

            set_manual = st.form_submit_button("Set Manual Time", use_container_width=True)
            if set_manual:
                manual_time = time(hour=int(manual_hour), minute=int(manual_minute))
                manual_dt = datetime.combine(manual_date, manual_time)
                if manual_dt > datetime.now():
                    st.error("Cannot be in the future.")
                else:
                    st.session_state["session_start"] = manual_dt
                    d = load_data()
                    d["session_start"] = manual_dt.isoformat()
                    save_data(d)
                    st.rerun()

        st.divider()

        if st.button("Clear Session", use_container_width=True):
            st.session_state.pop("session_start", None)
            d = load_data()
            d.pop("session_start", None)
            save_data(d)
            st.rerun()

        st.divider()

        # --- Calibrate with Claude ---
        with st.expander("Sync with Claude"):
            st.caption(
                "Enter the percentages shown on claude.ai/settings to calibrate."
            )
            cal_sess = st.number_input(
                "Session % on claude.ai", min_value=1, max_value=100, value=19,
            )
            cal_week = st.number_input(
                "Weekly % on claude.ai", min_value=1, max_value=100, value=32,
            )
            if st.button("Calibrate", use_container_width=True):
                # Read current collector data to compute caps
                _col = read_collector_summary()
                if _col:
                    _cs = _col.get("session", {})
                    _cw = _col.get("weekly", {})
                    _se = effective_cost(_cs)
                    _we = effective_cost(_cw)
                    new_s_cap = _se / (cal_sess / 100.0) if cal_sess > 0 else SESS_CAP_DEFAULT
                    new_w_cap = _we / (cal_week / 100.0) if cal_week > 0 else WEEK_CAP_DEFAULT
                    d = load_data()
                    d["usage_caps"] = {"session": round(new_s_cap, 2), "weekly": round(new_w_cap, 2)}
                    save_data(d)
                    st.success("Calibrated")
                    st.rerun()
                else:
                    st.error("No collector data yet. Start the dashboard first.")


# ==================== Main Area ====================

st.markdown(
    '<style>'
    'h1,h2,h3{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}}'
    '.block-container{{max-width:1200px;}}'
    '</style>'.replace("{{", "{").replace("}}", "}"),
    unsafe_allow_html=True,
)

status_dot = '<span style="color:#2E7D32;">&#9679;</span>' if dashboard_active else '<span style="color:#999;">&#9679;</span>'
status_word = "Active" if dashboard_active else "Stopped"

st.markdown(
    '<h1 style="margin-bottom:2px;">Claude Session Scheduler</h1>'
    '<p style="color:#777;font-size:14px;margin-top:0;">'
    '{dot} {status} &middot; {name} &middot; {sub} plan</p>'.format(
        dot=status_dot, status=status_word,
        name=account.get("displayName", ""),
        sub=sub_type,
    ),
    unsafe_allow_html=True,
)

# --- Stopped state: show idle screen ---
if not dashboard_active:
    st.markdown(
        '<div style="text-align:center;padding:80px 20px;font-family:-apple-system,BlinkMacSystemFont,'
        '\'Segoe UI\',Roboto,sans-serif;">'
        '  <div style="font-size:64px;margin-bottom:20px;">&#9211;</div>'
        '  <div style="font-size:22px;font-weight:600;color:#444;margin-bottom:10px;">'
        '    Dashboard is stopped</div>'
        '  <div style="font-size:15px;color:#888;max-width:480px;margin:0 auto;line-height:1.6;">'
        '    No background processes are running. Zero CPU usage.<br>'
        '    Click <strong>Start Dashboard</strong> in the sidebar to begin tracking your session '
        '    and collecting usage data.</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.stop()


# ========== TOP SECTION: Usage (Claude-style percentage bars) ==========

# --- Compute usage percentages ---
# Claude Pro limits are not publicly documented in exact token counts.
# We estimate using the API-equivalent cost of consumed tokens.
# Cache-read tokens are very cheap and contribute minimally to rate limits,
# so we weight them at 10% when computing "effective cost" for the percentage.
#
# Default caps (adjustable in sidebar Settings):
#   Session (5h): ~$100 API-equivalent
#   Weekly:       ~$500 API-equivalent

# Load saved caps or use defaults
_caps = persisted.get("usage_caps", {})
sess_cap = _caps.get("session", SESS_CAP_DEFAULT)
week_cap = _caps.get("weekly", WEEK_CAP_DEFAULT)


def bar_color(pct):
    if pct >= 80:
        return "#EF4444"   # red
    if pct >= 50:
        return "#F59E0B"   # amber
    return "#10B981"       # teal-green


# Get data from collector summary (preferred) or local stats
if collector is not None:
    c_sess = collector.get("session", {})
    c_week = collector.get("weekly", {})
    sess_used = effective_cost(c_sess)
    week_used = effective_cost(c_week)
    age_min = collector.get("_age_minutes", -1)
    is_stale = collector.get("_stale", False)
else:
    # Rough fallback from local inline stats
    sess_used = total_cost * 0.5
    week_used = total_cost
    age_min = -1
    is_stale = True

sess_pct = min(sess_used / sess_cap * 100, 100) if sess_cap > 0 else 0
week_pct = min(week_used / week_cap * 100, 100) if week_cap > 0 else 0

# Session reset: from collector data (oldest call in window + 5h)
now = datetime.now()
_sess_resets_raw = collector.get("session_resets_at") if collector else None
if _sess_resets_raw:
    try:
        sess_reset_dt = datetime.fromisoformat(_sess_resets_raw)
        _sess_remaining = sess_reset_dt - now
        if _sess_remaining.total_seconds() > 0:
            sess_reset_str = "resets {t} ({cd})".format(
                t=sess_reset_dt.strftime("%I:%M %p"),
                cd=fmt(_sess_remaining),
            )
        else:
            sess_reset_str = "reset (window clear)"
    except (ValueError, TypeError):
        sess_reset_str = "5-hour rolling window"
else:
    sess_reset_str = "no usage in current window"

# Weekly reset: from collector data (account creation + 7-day cycle)
_week_resets_raw = collector.get("weekly_resets_at") if collector else None
if _week_resets_raw:
    try:
        week_reset_dt = datetime.fromisoformat(_week_resets_raw)
        _week_remaining = week_reset_dt - now
        _days_left = _week_remaining.days
        if _days_left > 0:
            week_reset_str = "resets {dt} ({d}d {h}h)".format(
                dt=week_reset_dt.strftime("%a, %b %d %I:%M %p"),
                d=_days_left,
                h=int((_week_remaining.seconds) / 3600),
            )
        else:
            week_reset_str = "resets {dt} ({h}h)".format(
                dt=week_reset_dt.strftime("today %I:%M %p"),
                h=int(_week_remaining.total_seconds() / 3600),
            )
    except (ValueError, TypeError):
        week_reset_str = ""
else:
    week_reset_str = ""

# Token detail strings for the subtitle
if collector is not None:
    sess_detail = "{inp} in + {out} out".format(
        inp=fmt_tokens(c_sess.get("input_tokens", 0) + c_sess.get("cache_creation_tokens", 0)),
        out=fmt_tokens(c_sess.get("output_tokens", 0)),
    )
    week_detail = "{inp} in + {out} out".format(
        inp=fmt_tokens(c_week.get("input_tokens", 0) + c_week.get("cache_creation_tokens", 0)),
        out=fmt_tokens(c_week.get("output_tokens", 0)),
    )
else:
    sess_detail = ""
    week_detail = ""

# Freshness indicator
if age_min >= 0 and not is_stale:
    fresh_text = "Updated {m} min ago".format(m=age_min)
elif is_stale and age_min >= 0:
    fresh_text = "Data is {m} min old".format(m=age_min)
else:
    fresh_text = ""

sess_bar_clr = bar_color(sess_pct)
week_bar_clr = bar_color(week_pct)

usage_html = (
    '<div style="display:flex;gap:20px;margin-bottom:28px;font-family:-apple-system,BlinkMacSystemFont,'
    '\'Segoe UI\',Roboto,sans-serif;">'
    ''
    '  <div style="flex:1;background:#FFFFFF;border:1px solid #E5E7EB;border-radius:16px;padding:24px 28px;">'
    '    <div style="display:flex;justify-content:space-between;align-items:center;">'
    '      <div style="font-size:15px;font-weight:600;color:#1A1A1A;">Session usage <span style="font-size:10px;font-weight:400;color:#CCC;">(estimate)</span></div>'
    '      <div style="font-size:28px;font-weight:700;color:{sess_clr};">~{sess_pct:.0f}%</div>'
    '    </div>'
    '    <div style="font-size:12px;color:#B0B0B0;margin:4px 0 14px;">{sess_detail}</div>'
    '    <div style="background:#F3F4F6;border-radius:6px;height:8px;overflow:hidden;margin-bottom:14px;">'
    '      <div style="background:{sess_clr};height:100%;width:{sess_pct:.1f}%;border-radius:6px;'
    '      transition:width 0.5s ease;"></div>'
    '    </div>'
    '    <div id="live-sess-reset" style="font-size:12px;color:#9CA3AF;">{sess_reset}</div>'
    '  </div>'
    ''
    '  <div style="flex:1;background:#FFFFFF;border:1px solid #E5E7EB;border-radius:16px;padding:24px 28px;">'
    '    <div style="display:flex;justify-content:space-between;align-items:center;">'
    '      <div style="font-size:15px;font-weight:600;color:#1A1A1A;">Weekly usage <span style="font-size:10px;font-weight:400;color:#CCC;">(estimate)</span></div>'
    '      <div style="font-size:28px;font-weight:700;color:{week_clr};">~{week_pct:.0f}%</div>'
    '    </div>'
    '    <div style="font-size:12px;color:#B0B0B0;margin:4px 0 14px;">{week_detail}</div>'
    '    <div style="background:#F3F4F6;border-radius:6px;height:8px;overflow:hidden;margin-bottom:14px;">'
    '      <div style="background:{week_clr};height:100%;width:{week_pct:.1f}%;border-radius:6px;'
    '      transition:width 0.5s ease;"></div>'
    '    </div>'
    '    <div id="live-week-reset" style="font-size:12px;color:#9CA3AF;">{week_reset}</div>'
    '  </div>'
    ''
    '</div>'
).format(
    sess_pct=sess_pct, sess_clr=sess_bar_clr, sess_reset=sess_reset_str, sess_detail=sess_detail,
    week_pct=week_pct, week_clr=week_bar_clr, week_reset=week_reset_str, week_detail=week_detail,
)

st.markdown(usage_html, unsafe_allow_html=True)

# Small detail row below the bars
if fresh_text:
    st.markdown(
        '<div style="text-align:right;font-size:11px;color:#D1D5DB;margin-top:-20px;margin-bottom:16px;'
        'font-family:-apple-system,sans-serif;">{ft}</div>'.format(ft=fresh_text),
        unsafe_allow_html=True,
    )


# ========== Session Tracker ==========

session_start = st.session_state.get("session_start")

if session_start is None:
    st.info("No active session. Click **Start Session Now** in the sidebar or enter a manual start time.")
else:
    now = datetime.now()
    remaining = get_remaining(session_start)
    window_end = session_start + timedelta(hours=WINDOW_HOURS)
    window_expired = now >= window_end
    elapsed_secs = (now - session_start).total_seconds()
    total_secs = WINDOW_HOURS * 3600
    pct = min(elapsed_secs / total_secs, 1.0) * 100
    all_sessions = compute_all_sessions(session_start)

    remaining_color = CLR_ACTIVE if not window_expired else CLR_EXPIRED
    bar_clr = "#EF6C00" if pct > 80 else CLR_ACTIVE
    remaining_str = fmt(remaining)
    start_str = session_start.strftime("%I:%M %p")
    end_str = window_end.strftime("%I:%M %p")

    divider = '<hr style="border:none;border-top:1px solid #ECEFF1;margin:28px 0;">'

    if window_expired:
        banner_html = (
            '<div id="live-banner" style="background:{bg};border-left:5px solid {c};padding:14px 18px;'
            'border-radius:6px;margin-bottom:20px;font-size:15px;color:{c};">'
            'Window expired. You can start a new session.</div>'
        ).format(bg=CLR_EXPIRED_BG, c=CLR_EXPIRED)
    else:
        banner_html = (
            '<div id="live-banner" style="background:{bg};border-left:5px solid {c};padding:14px 18px;'
            'border-radius:6px;margin-bottom:20px;font-size:15px;color:#333;">'
            'Current session has <strong style="color:{c};">'
            '<span class="live-remaining">{rem}</span></strong> remaining</div>'
        ).format(bg=CLR_ACTIVE_BG, c=CLR_ACTIVE, rem=remaining_str)

    metrics_html = (
        '<div style="display:flex;gap:14px;margin-bottom:18px;">'
        '  <div style="flex:1;background:#FAFAFA;border:1px solid #EEE;border-radius:10px;padding:18px;text-align:center;">'
        '    <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px;">Session Start</div>'
        '    <div style="font-size:26px;font-weight:700;color:#222;margin-top:6px;">{start}</div>'
        '  </div>'
        '  <div style="flex:1;background:#FAFAFA;border:1px solid #EEE;border-radius:10px;padding:18px;text-align:center;">'
        '    <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px;">Balance Time</div>'
        '    <div class="live-remaining" style="font-size:26px;font-weight:700;color:{rem_clr};margin-top:6px;">{rem}</div>'
        '  </div>'
        '  <div style="flex:1;background:#FAFAFA;border:1px solid #EEE;border-radius:10px;padding:18px;text-align:center;">'
        '    <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:1px;">Window Ends</div>'
        '    <div style="font-size:26px;font-weight:700;color:#222;margin-top:6px;">{end}</div>'
        '  </div>'
        '</div>'
    ).format(start=start_str, rem=remaining_str, rem_clr=remaining_color, end=end_str)

    progress_html = (
        '<div style="margin-bottom:24px;">'
        '  <div style="display:flex;justify-content:space-between;font-size:12px;color:#888;margin-bottom:5px;">'
        '    <span>Window Progress</span><span id="live-progress-pct">{pct:.1f}%</span>'
        '  </div>'
        '  <div style="background:#ECEFF1;border-radius:10px;height:12px;overflow:hidden;">'
        '    <div id="live-progress-bar" style="background:{bar};height:100%;width:{pct:.1f}%;'
        'border-radius:10px;transition:width 0.3s ease;"></div>'
        '  </div>'
        '</div>'
    ).format(pct=pct, bar=bar_clr)

    # Session plan cards
    cards_html = ""
    for i, (s_start, s_end) in enumerate(all_sessions, start=1):
        dur = s_end - s_start
        is_current = s_start <= now < s_end
        is_past = now >= s_end

        if is_past:
            border = CLR_EXPIRED
            bg = CLR_EXPIRED_BG
            badge_bg = CLR_EXPIRED
            badge_text = "Completed"
        elif is_current:
            border = CLR_ACTIVE
            bg = CLR_ACTIVE_BG
            badge_bg = CLR_ACTIVE
            badge_text = 'Active &mdash; <span class="live-remaining">' + remaining_str + '</span> left'
        else:
            wait = fmt(s_start - now)
            border = CLR_UPCOMING
            bg = CLR_UPCOMING_BG
            badge_bg = CLR_UPCOMING
            badge_text = "Starts in " + wait

        time_range = s_start.strftime("%I:%M %p") + " &mdash; " + s_end.strftime("%I:%M %p")
        dur_str = fmt(dur)

        cards_html += (
            '<div style="display:flex;align-items:center;border-left:5px solid {border};background:{bg};'
            'border-radius:8px;padding:14px 20px;margin-bottom:10px;">'
            '  <div style="flex:0 0 100px;font-weight:700;font-size:15px;color:#333;">Session {num}</div>'
            '  <div style="flex:1;font-size:14px;color:#555;">{time} &nbsp;&middot;&nbsp; {dur}</div>'
            '  <div><span style="background:{badge_bg};color:#fff;padding:3px 12px;border-radius:14px;'
            '  font-size:12px;font-weight:500;">{badge_text}</span></div>'
            '</div>'
        ).format(border=border, bg=bg, num=i, time=time_range, dur=dur_str,
                 badge_bg=badge_bg, badge_text=badge_text)

    # Timeline
    timeline_blocks = ""
    for h in range(24):
        block_s = now.replace(hour=h, minute=0, second=0, microsecond=0)
        block_e = block_s + timedelta(hours=1)
        color = CLR_FREE
        for s_start, s_end in all_sessions:
            if s_start <= block_s < s_end or s_start < block_e <= s_end:
                if s_start <= now < s_end:
                    color = CLR_ACTIVE
                elif now >= s_end:
                    color = CLR_EXPIRED
                else:
                    color = CLR_UPCOMING
                break
        hlabel = datetime.strptime(str(h), "%H").strftime("%I%p").lstrip("0").lower()
        timeline_blocks += (
            '<div style="display:inline-block;text-align:center;'
            'width:calc(100%/24 - 3px);margin:0 1px;">'
            '<div style="font-size:9px;color:#AAA;margin-bottom:3px;">{lbl}</div>'
            '<div style="background:{clr};height:30px;border-radius:4px;"></div>'
            '</div>'
        ).format(lbl=hlabel, clr=color)

    timeline_html = (
        '<div style="margin-top:8px;">{blocks}</div>'
        '<div style="display:flex;gap:18px;font-size:12px;color:#777;margin-top:10px;">'
        '  <div><span style="display:inline-block;width:12px;height:12px;background:{exp};'
        '  border-radius:3px;vertical-align:middle;"></span> Completed</div>'
        '  <div><span style="display:inline-block;width:12px;height:12px;background:{act};'
        '  border-radius:3px;vertical-align:middle;"></span> Active</div>'
        '  <div><span style="display:inline-block;width:12px;height:12px;background:{upc};'
        '  border-radius:3px;vertical-align:middle;"></span> Upcoming</div>'
        '  <div><span style="display:inline-block;width:12px;height:12px;background:{fre};'
        '  border-radius:3px;vertical-align:middle;"></span> Free</div>'
        '</div>'
    ).format(blocks=timeline_blocks, exp=CLR_EXPIRED, act=CLR_ACTIVE, upc=CLR_UPCOMING, fre=CLR_FREE)

    # Tips
    tips_html = (
        '<ul style="font-size:14px;color:#555;line-height:2;padding-left:20px;">'
        '  <li><strong>Batch your prompts</strong> &mdash; group related questions into one detailed prompt.</li>'
        '  <li><strong>Use system prompts wisely</strong> &mdash; fewer follow-up corrections needed.</li>'
        '  <li><strong>Plan around the 5-hour window</strong> &mdash; use intensively, then break.</li>'
        '  <li><strong>Save important outputs locally</strong> &mdash; copy before the window ends.</li>'
        '  <li><strong>Use Projects / custom instructions</strong> &mdash; less back-and-forth per session.</li>'
        '</ul>'
    )

    # Render all sections
    full_html = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
        '  <h2 style="color:#222;margin-bottom:14px;">Current Session</h2>'
        '  {banner}'
        '  {metrics}'
        '  {progress}'
        '  {div}'
        '  <h2 style="color:#222;margin-bottom:14px;">Today\'s Session Plan</h2>'
        '  {cards}'
        '  {div}'
        '  <h2 style="color:#222;margin-bottom:14px;">Daily Timeline</h2>'
        '  {timeline}'
        '  {div}'
        '  <h2 style="color:#222;margin-bottom:14px;">Usage Tips</h2>'
        '  {tips}'
        '</div>'
    ).format(
        banner=banner_html, metrics=metrics_html, progress=progress_html,
        cards=cards_html, timeline=timeline_html, tips=tips_html, div=divider,
    )

    st.markdown(full_html, unsafe_allow_html=True)
