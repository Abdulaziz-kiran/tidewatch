#!/usr/bin/env python3
"""Collect AI limits from files and agy; fast output modes only read files."""
import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import plistlib
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent
HOME = Path.home()
AGY = shutil.which("agy") or next(
    (path for path in ("/opt/homebrew/bin/agy", "/usr/local/bin/agy")
     if os.path.isfile(path) and os.access(path, os.X_OK)), None)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DATA = ROOT / "data"
CACHE = DATA / "limits.json"
CLAUDE_CACHE = DATA / "claude-usage.json"
MODELS_FILE = DATA / "known-models.json"
SETTINGS = DATA / "settings.json"
# The standard settings file; data stays in DATA unless it sets data_dir.
USER_SETTINGS = (Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config")
                 / "tidewatch" / "settings.json")
LOCALES = ROOT / "locales"
LANGUAGE = None  # From settings; otherwise TIDEWATCH_LANG, the system language, English.
HELLO_STATE = DATA / "hello.json"
HELLO_LOG = DATA / "hello-log.jsonl"
LOCK = DATA / "refreshing"
HISTORY = DATA / "history.jsonl"
HISTORY_ARCHIVE = DATA / "history-archive"
HISTORY_LIMIT = 5 * 1024 * 1024
CONFIG = None  # Explicit settings file, passed on to background refreshes.
TZ = None  # None: the system's local time zone.
LOW_THRESHOLD, CACHE_MINUTES = 10, 30
STALE_MEASUREMENT_SECONDS = 2 * 3600
CLAUDE_STALE_SECONDS = 6 * 3600
# Planned working windows (local time). Night stands for unattended agent work.
WORK_HOURS = [("19:00", "22:30")]
NIGHT_HOURS = [("22:30", "04:00")]
TARGET_HOURS_BEFORE = 24
WARNING_HORIZON_DAYS = 5
NORMAL_RATE = {"Claude": 3, "Codex": 3, "Antigravity": 3}  # Percent per hour, estimates.
NIGHT_RATE = {"Claude": 1, "Codex": 1, "Antigravity": 1}
FAST_RATE = {"Claude": 6, "Codex": 20, "Antigravity": 10}
CLAUDE_5H_CAP = 20
SUBSCRIPTION_END = {}  # {"Codex": "2026-12-01T00:00:00+00:00"}
PAID_PLANS = {"plus", "pro", "team", "business", "enterprise", "edu"}
FREE_PLANS = {"free"}
BACKLOG_FILE = None
ROSTER_FILE = None
HELLO_PROMPT = "hello"
CODEX_MODEL_FILES = ("models_cache.json", "models_cache.codex_work_desktop.json")
STRICT_CODEX_MODEL_FILES = False
SHORT_SUMMARY = False
CLAUDE_MISSING_IS_STALE = False
SOURCES = (("claude", "Claude"), ("codex", "Codex"), ("agy", "Antigravity"))
# Order determines menu title color priority.
WARNING_COLORS = {"rescue": "red", "pace": "orange", "new_model": "#0A84FF",
                  "new_source": "#0A84FF", "low": ""}
SHORT_ORDER = {"new_source": -1, "rescue": 0, "pace": 1}
GROUP_NAMES = {"Gemini Models": "Gemini", "Gemini 4 Argon": "Argon",
               "Claude and GPT models": "Claude/GPT"}
MESSAGES = {
    "percent": "{}%",
    "decimal_comma": False,
    "date_format": None,
    "duration_days": "{days}d {hours}h",
    "duration_hours": "{hours}h {minutes}m",
    "duration_minutes": "{minutes}m",
    "duration_long_days": "{days}d {hours}h",
    "duration_long_hours": "{hours}h {minutes}m",
    "duration_long_minutes": "{minutes}m",
    "window_weekly": "weekly",
    "window_5h": "5-hour",
    "window_minutes": "{minutes} minutes",
    "table_weekly": "Weekly",
    "table_group_weekly": "Weekly",
    "label_rescue": "AT RISK",
    "label_pace": "OFF PACE",
    "label_low": "LOW",
    "label_new_model": "NEW MODEL",
    "label_new_source": "NEW SOURCE",
    "label_claude_stale": "CLAUDE DATA STALE",
    "work_time": "evening {evening} h + night {night} h",
    "evening_missed": "missed",
    "evening_no_time": "no time",
    "measured_ago": " (measured {age} ago)",
    "warn_low": "{label}: {name} {window} {percent} left, resets {date} ({duration}){suffix}",
    "warn_missed": ("{label}: {name} weekly {percent} unused; target {target} missed, "
                    "resets {date} ({duration}); until last use {work}, fast ~{fast}"),
    "warn_pace": ("{label}: {name} weekly {percent}; until target {target}: "
                  "{work}: normal ~{normal}, fast ~{fast}"),
    "warn_subscription_end": "; subscription ends {date}",
    "warn_new_model": ("{label}: {name} ({id}): review which models you use, "
                       "then run tidewatch.py --accept-models{suffix}"),
    "new_model_separator": ": review",
    "warn_new_models": ("{label} ({count}): {names}: review which models you use, "
                        "then run tidewatch.py --accept-models"),
    "warn_new_source": ("{label}: {source} models were added to the baseline automatically "
                        "({count} found): review them, then run tidewatch.py "
                        "--accept-models{suffix}"),
    "more_warnings": " (+{count} warnings: tidewatch.py)",
    "claude_stale": ("{label}: last saved {age} ago; "
                     "save fresh usage with tidewatch.py --claude-save"),
    "unknown": "unknown",
    "claude_missing": "Claude: no saved usage yet (tidewatch.py --claude-save)",
    "claude_windows_invalid": "Claude windows are empty or invalid",
    "no_subscription": "{source}: no subscription (hidden)",
    "models_error": "models: {error}",
    "source_models_error": "{source} models: {error}",
    "refresh_running": "refresh already in progress",
    "facts": "LIMITS: {items}",
    "facts_item": "{name} {percent}",
    "facts_stale": " (stale data)",
    "facts_left": ", {duration} left, ",
    "facts_no_waste": "nothing goes unused",
    "facts_waste": "~{percent} goes unused",
    "facts_daily": ", ~{percent} a day",
    "roster": "Roster: {path}",
    "backlog": "Backlog: {count} items, {path}",
    "backlog_empty": "Backlog: empty, {path}",
    "backlog_item": "Backlog ({source}): {item}",
    "backlog_done_heading": "## Done",
    "menu_headers": ["Tool", "Limit", "Left", "Time left", "Resets", "Per evening"],
    "menu_no_fresh_data": "no fresh data",
    "menu_stale_record": " (stale record)",
    "menu_reset": "reset",
    "menu_unused_legend": "~% : share likely to go unused at this pace (estimate)",
    "menu_measured": "Measured: {items}",
    "menu_age": "{name} {age} ago",
    "menu_refresh": "Refresh",
    "menu_error": "Could not read data",
    "text_data_cache": "Data: cache, {age} ago",
    "text_data_now": "Data: now",
    "text_reset_no_data": "reset, no fresh data",
    "text_last_hello": "Last automatic message: {group} {date} ({model}, {result})",
    "hello_sent": "sent",
    "hello_no_model": "no model",
    "hello_error": "error: {error}",
    "text_models": "Models: {count}",
    "text_baseline": "Models: {source} baseline created ({count} model)",
    "text_new": "New: {name} ({id})",
    "text_removed": "Removed model: {name} ({id})",
    "text_warnings": "Warnings",
    "text_none": "None",
    "accept_result": "Compared sources: {sources}\nAdded: {added}\nRemoved: {removed}",
    "none": "none",
    "backfill_done": "Added {count} lines to history.",
    "pattern_title": "Usage pattern (local time): each drop counts at the second observation.",
    "pattern_source": "{source}: {total} used in total, {pairs} pairs",
    "pattern_hour": "  {hour}:00–{hour}:59: {share} share, {used} used",
    "pattern_evening": "  {start}–{end} share: {share}",
    "pattern_fastest": "  Fastest (at least 2 hours): ~{rate}/hour",
    "pattern_empty": "No weekly observations in history.",
    "error_claude_save": "Could not save Claude usage",
    "error_accept": "Could not accept models",
    "error_other": "Could not complete operation",
    "error_no_details": "No details",
}
ENGLISH = dict(MESSAGES)
MESSAGE_OVERRIDES = {}
_lock = None


def tr(key, **values):
    return MESSAGES[key].format(**values)


def language_code(value):
    """'tr_TR.UTF-8', 'pt-BR' or 'zh-Hans' -> 'tr', 'pt', 'zh'."""
    return str(value).replace("_", "-").split(".")[0].split("@")[0].split("-")[0].lower()


def system_language():
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value and value not in ("C", "POSIX") and not value.startswith("C."):
            return language_code(value)
    if sys.platform == "darwin":
        # The macOS language list, read from the preferences file without starting a process.
        try:
            with (HOME / "Library" / "Preferences" / ".GlobalPreferences.plist").open("rb") as f:
                languages = plistlib.load(f).get("AppleLanguages") or []
            if languages and isinstance(languages[0], str):
                return language_code(languages[0])
        except (OSError, ValueError, AttributeError, plistlib.InvalidFileException):
            pass
    return "en"


def available_languages():
    return sorted({"en"} | {p.stem for p in LOCALES.glob("*.json")})


def load_messages(language=None):
    """English, then the language file from locales/, then the settings' own messages.
    An unknown language falls back to English."""
    code = language_code(language or "en")
    messages = dict(ENGLISH)
    path = LOCALES / f"{code}.json"
    if code != "en" and code.isalpha() and path.is_file():
        translated = read_json(path)
        if not isinstance(translated, dict) or not set(translated) <= set(ENGLISH):
            raise ValueError(f"Unknown message keys in {path.name}")
        messages.update(translated)
    messages.update(MESSAGE_OVERRIDES)
    MESSAGES.clear()
    MESSAGES.update(messages)


def display_width(value):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else
               0 if unicodedata.combining(c) else 1 for c in value)


def pad(value, width, right=False):
    space = " " * max(0, width - display_width(value))
    return space + value if right else value + space


def number(value, minimum=0, maximum=253402214400):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Invalid numeric field")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("Numeric field out of range")
    return value


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid text field")
    return value.replace("\n", " ").replace("\r", " ").replace("|", "/")


def iso_epoch(value):
    dt = datetime.fromisoformat(text(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Missing timezone")
    return number(dt.timestamp())


def child_time(obj, timestamp):
    """A broken or null nested timestamp must not drop a valid record on the same line."""
    try:
        return iso_epoch(obj["timestamp"])
    except (ValueError, TypeError, AttributeError):
        return timestamp


def local_time(epoch):
    return datetime.fromtimestamp(epoch, TZ) if TZ else datetime.fromtimestamp(epoch)


def format_date(epoch):
    local = local_time(number(epoch))
    if MESSAGES["date_format"]:
        return local.strftime(MESSAGES["date_format"])
    return (f"{MONTHS[local.month - 1]} {local.day:02d} "
            f"{local.hour:02d}:{local.minute:02d}")


def format_duration(seconds, long=False):
    minutes = max(0, int(seconds)) // 60
    hours, minutes = divmod(minutes, 60)
    days, hours_left = divmod(hours, 24)
    prefix = "duration_long_" if long else "duration_"
    if days:
        return tr(prefix + "days", days=days, hours=hours_left)
    if hours:
        return tr(prefix + "hours", hours=hours, minutes=minutes)
    return tr(prefix + "minutes", minutes=minutes)


def decimal(value):
    result = f"{value:.1f}"
    return result.replace(".", ",") if MESSAGES["decimal_comma"] else result


def percent(value):
    return MESSAGES["percent"].format(value)


def window_label(window):
    if window == "weekly":
        return tr("window_weekly")
    if window == "5-hour":
        return tr("window_5h")
    if window.endswith(" minutes"):
        return tr("window_minutes", minutes=window[:-len(" minutes")])
    return window


def window_from_minutes(minutes):
    return {300: "5-hour", 10080: "weekly"}.get(minutes, f"{minutes:g} minutes")


def merge(intervals):
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def clock(day, value):
    moment = datetime.combine(day, datetime.strptime(value, "%H:%M").time())
    return moment.replace(tzinfo=TZ) if TZ else moment


def work_intervals(start, end, night=False):
    """Build windows from the local calendar, clip them and merge overlaps."""
    if end <= start:
        return []
    day = local_time(start).date() - timedelta(days=1)
    last_day = local_time(end).date()
    intervals = []
    while day <= last_day:
        for first, last in (NIGHT_HOURS if night else WORK_HOURS):
            a, b = clock(day, first), clock(day, last)
            if b < a:
                b += timedelta(days=1)
            p, q = max(start, a.timestamp()), min(end, b.timestamp())
            if q > p and (not night or a.timestamp() > start):
                intervals.append((p, q))
        day += timedelta(days=1)
    return merge(intervals)


def total_hours(intervals):
    return sum(b - a for a, b in intervals) / 3600


def night_intervals(start, end, kind):
    # Normal pace counts only nights that start later; fast pace also keeps a started night.
    if kind == "normal":
        return work_intervals(start, end, night=True)
    before = local_time(start).replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    return [(max(a, start), b) for a, b in
            work_intervals(before.timestamp(), end, night=True) if b > start]


def capacity(x, start, end, kind):
    source = x["source"]
    evenings = work_intervals(start, end)
    nights = night_intervals(start, end, kind)
    if kind == "normal":
        return (total_hours(evenings) * NORMAL_RATE[source] +
                total_hours(nights) * NIGHT_RATE[source])
    # One cap for an evening and the night that follows it: an optimistic upper bound.
    total = 0
    for a, b in merge(evenings + nights):
        hours = (b - a) / 3600
        cap = CLAUDE_5H_CAP * math.ceil(hours / 5) if source == "Claude" else math.inf
        total += min(hours * FAST_RATE[source], cap)
    return total


def work_time(start, end):
    evening = total_hours(work_intervals(start, end))
    night = total_hours(night_intervals(start, end, "fast"))
    return tr("work_time", evening=decimal(evening), night=decimal(night))


def subscription_end(x):
    end = SUBSCRIPTION_END.get(x["source"])
    if end:
        end = iso_epoch(end)
        if x["data_time"] <= end:
            return end
    return None


def no_subscription(x, now):
    end = SUBSCRIPTION_END.get(x["source"])
    if end:
        end = iso_epoch(end)
        if now < end:
            return False
        return not (x["data_time"] > end and x.get("plan") in PAID_PLANS)
    return x.get("plan") in FREE_PLANS


def visible_limits(data, now):
    return [x for x in all_limits(data) if not no_subscription(x, now)]


def target_and_last(x):
    end = subscription_end(x)
    last = min(x["reset"], end) if end is not None else x["reset"]
    return last - TARGET_HOURS_BEFORE * 3600, last


def sub_limit(x):
    """Claude's per-model weekly limit (e.g. Sonnet) is part of the all-models limit;
    what is left there is not wasted separately, so it gets no level."""
    return x["source"] == "Claude" and "·" in x["name"] and "all" not in x["name"].lower()


def claude_stale(x, now):
    return x["source"] == "Claude" and now - x["data_time"] > CLAUDE_STALE_SECONDS


def level(x, now):
    if (x["window"] != "weekly" or x["remaining"] < LOW_THRESHOLD or sub_limit(x) or
            x["reset"] <= now or no_subscription(x, now) or claude_stale(x, now)):
        return ""
    target, _ = target_and_last(x)
    if target - now > WARNING_HORIZON_DAYS * 86400:
        return ""
    if now >= target or x["remaining"] > capacity(x, now, target, "fast"):
        return "rescue"
    if x["remaining"] > capacity(x, now, target, "normal"):
        return "pace"
    return ""


def per_evening(x, now):
    if x["window"] != "weekly":
        return "-"
    target, _ = target_and_last(x)
    if now >= target:
        return tr("evening_missed")
    hours = total_hours(work_intervals(now, target))
    day = local_time(now).replace(hour=0, minute=0, second=0, microsecond=0)
    daily = total_hours(work_intervals(day.timestamp(), (day + timedelta(days=1)).timestamp()))
    if not (hours and daily):
        return tr("evening_no_time")
    return percent(f"{x['remaining'] / max(1, math.ceil(hours / daily - 0.01)):.0f}")


def active_backlog_lines():
    for line in BACKLOG_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip() == MESSAGES["backlog_done_heading"]:
            break
        if line.startswith("- "):
            yield line


def backlog_lines(data, now=None):
    now = time.time() if now is None else now
    if BACKLOG_FILE is None:
        return []
    sources = {x["source"] for x in visible_limits(data, now) if level(x, now)}
    first = {}
    try:
        for line in active_backlog_lines():
            for source in sources:
                prefix = f"- [{source}] "
                if line.startswith(prefix) and source not in first:
                    first[source] = text(line[len(prefix):])
    except (OSError, UnicodeError, ValueError):
        return []
    return [tr("backlog_item", source=name, item=first[name])
            for _, name in SOURCES if name in first]


def read_json(path):
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".limits-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe(operation, *args):
    try:
        return operation(*args)
    except Exception as error:
        return {"error": f"{getattr(operation, '__name__', 'source')}: {type(error).__name__}"}


def limit(source, name, window, remaining, reset, timestamp, plan=None):
    return dict(source=text(source), name=text(name), window=text(window),
                remaining=round(number(remaining, 0, 100)), reset=number(reset),
                data_time=number(timestamp), plan=text(plan) if plan is not None else None)


def run_agy(args, timeout):
    hello = (isinstance(args, list) and len(args) == 8
             and args[:3] == ["-p", HELLO_PROMPT, "--model"]
             and isinstance(args[3], str) and bool(args[3])
             and args[4:] == ["--output-format", "json", "--print-timeout", "60s"])
    if args != ["models"] and args != [
            "-p", "/usage", "--output-format", "json", "--print-timeout", "60s"] and not hello:
        raise ValueError("Only agy usage, model listing, and the fixed hello prompt are allowed")
    if AGY is None:
        raise FileNotFoundError("agy is not installed")
    process = subprocess.Popen([AGY] + args, cwd=str(HOME), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            process.stdout.close()
            process.stderr.close()
        raise TimeoutError("agy timed out") from None
    if process.returncode:
        raise RuntimeError(f"agy exit code {process.returncode}")
    return output


def agy_usage():
    if AGY is None:
        return {}
    raw = json.loads(run_agy(
        ["-p", "/usage", "--output-format", "json", "--print-timeout", "60s"], 40))
    timestamp, items = time.time(), []
    for group in raw["command"]["data"]["groups"]:
        for bucket in group["buckets"]:
            window = {"weekly": "weekly", "5h": "5-hour"}[bucket["window"]]
            items.append(limit("Antigravity", group["name"], window,
                               number(bucket["remaining_fraction"], 0, 1) * 100,
                               iso_epoch(bucket["reset_time"]), timestamp))
    if not items:
        raise ValueError("No Antigravity limits found")
    return {"limits": items, "data_time": timestamp}


def tail(path, size=512 * 1024):
    with path.open("rb") as file:
        file.seek(0, 2)
        start = max(0, file.tell() - size)
        file.seek(start)
        raw = file.read(size)
    # The truncated first line may contain partial UTF-8 or JSON.
    return (raw.split(b"\n", 1)[-1] if start else raw).decode(
        "utf-8", errors="replace").splitlines()


def history_paths():
    return sorted(HISTORY_ARCHIVE.glob("history-*.jsonl")) + [HISTORY]


def history_line(line):
    try:
        x = json.loads(line)
        for field in ("observed_at", "recorded_at", "reset"):
            number(x[field])
        number(x["remaining"], maximum=100)
        for field in ("source", "name", "window"):
            text(x[field])
        return x
    except (ValueError, TypeError, KeyError):
        return None


def history_records():
    for path in history_paths():
        try:
            with path.open(encoding="utf-8", errors="replace") as file:
                for line in file:
                    x = history_line(line)
                    if x:
                        yield x
        except OSError:
            continue


def history_key(x):
    return x["source"], x["name"], x["window"]


def history_latest(keys):
    """Read a small tail first; grow it when a rarely updated source is missing."""
    result = {}
    for path in reversed(history_paths()):
        if keys <= result.keys():
            break
        try:
            size, total = 512 * 1024, path.stat().st_size
            while True:
                for line in reversed(tail(path, size)):
                    x = history_line(line)
                    if x:
                        result.setdefault(history_key(x), x)
                if keys <= result.keys() or size >= total:
                    break
                size *= 2
        except FileNotFoundError:
            continue
    return result


def rotate_history():
    if HISTORY.exists() and HISTORY.stat().st_size > HISTORY_LIMIT:
        HISTORY_ARCHIVE.mkdir(parents=True, exist_ok=True)
        moment = local_time(time.time())
        while True:
            target = HISTORY_ARCHIVE / ("history-" + moment.strftime("%Y%m%d-%H%M%S") + ".jsonl")
            if not target.exists():
                break
            moment += timedelta(seconds=1)
        HISTORY.rename(target)


def append_history(lines):
    if _lock is None:
        raise RuntimeError("Writing history requires the lock")
    rotate_history()
    if lines:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(json.dumps(x, ensure_ascii=False, allow_nan=False) + "\n"
                          for x in lines)
        with HISTORY.open("a", encoding="utf-8") as file:
            file.write(content)
    rotate_history()


def write_history(items, now):
    if _lock is None:
        raise RuntimeError("Writing history requires the lock")
    latest = history_latest({history_key(x) for x in items})
    lines = []
    for x in items:
        key = history_key(x)
        old = latest.get(key)
        if (old is None or old["remaining"] != x["remaining"] or old["reset"] != x["reset"] or
                now - old["recorded_at"] >= 21600):
            record = {k: x[k] for k in ("source", "name", "window", "remaining", "reset")}
            record.update(observed_at=x["data_time"], recorded_at=now)
            lines.append(record)
            latest[key] = record
    append_history(lines)


def raw_codex_records(obj, timestamp=None):
    """Unrounded raw percentages for the history backfill, whatever the reset time."""
    if isinstance(obj, dict):
        if "timestamp" in obj:
            timestamp = child_time(obj, timestamp)
        rates = obj.get("rate_limits")
        if isinstance(rates, dict) and timestamp is not None:
            for name in ("primary", "secondary"):
                try:
                    x = rates[name]
                    window = window_from_minutes(number(x["window_minutes"], 1))
                    yield dict(source="Codex", name="Codex", window=window,
                               remaining=100 - number(x["used_percent"], 0, 100),
                               reset=number(x["resets_at"]), observed_at=timestamp)
                except (ValueError, TypeError, KeyError):
                    continue
        for child in obj.values():
            yield from raw_codex_records(child, timestamp)
    elif isinstance(obj, list):
        for child in obj:
            yield from raw_codex_records(child, timestamp)


def backfill_history():
    if not acquire_lock():
        raise ValueError(tr("refresh_running"))
    try:
        existing = {(x["observed_at"], x["window"]) for x in history_records()
                    if x["source"] == "Codex"}
        new = {}
        now = time.time()
        for path in sorted((codex_home() / "sessions").rglob("*.jsonl")):
            try:
                with path.open(encoding="utf-8", errors="replace") as file:
                    for line in file:
                        if '"rate_limits"' not in line:
                            continue
                        try:
                            for x in raw_codex_records(json.loads(line)):
                                key = (x["observed_at"], x["window"])
                                if key not in existing:
                                    new.setdefault(key, dict(x, recorded_at=now))
                        except (ValueError, TypeError, KeyError, RecursionError):
                            continue
            except OSError:
                continue
        lines = [new[k] for k in sorted(new)]
        append_history(lines)
        return tr("backfill_done", count=len(lines))
    finally:
        release_lock()


def in_window(moment, window):
    start, end = ((int(v[:2]), int(v[3:])) for v in window)
    now = (moment.hour, moment.minute)
    return start <= now < end if start <= end else (now >= start or now < end)


def usage_pattern():
    groups, sources = {}, {}
    for x in history_records():
        if x["window"] == "weekly":
            groups.setdefault(history_key(x), {})[x["observed_at"]] = x
    for (source, _, _), records in sorted(groups.items()):
        pattern = sources.setdefault(source, dict(hours=[0.0] * 24, evening=0.0,
                                                  rate=0.0, pairs=0))
        items = sorted(records.values(), key=lambda x: x["observed_at"])
        for a, b in zip(items, items[1:]):
            duration = b["observed_at"] - a["observed_at"]
            if (abs(b["reset"] - a["reset"]) >= 600 or b["remaining"] > a["remaining"]
                    or duration <= 0):
                continue
            drop = a["remaining"] - b["remaining"]
            local = local_time(b["observed_at"])
            pattern["hours"][local.hour] += drop
            pattern["pairs"] += 1
            if WORK_HOURS and in_window(local, WORK_HOURS[0]):
                pattern["evening"] += drop
        # Fastest: from each observation to the first one at least 2 hours later, same cycle.
        j = 0
        for i, a in enumerate(items):
            j = max(j, i + 1)
            while j < len(items) and items[j]["observed_at"] - a["observed_at"] < 7200:
                j += 1
            if j >= len(items):
                break
            b = items[j]
            if abs(b["reset"] - a["reset"]) < 600 and b["remaining"] <= a["remaining"]:
                hours = (b["observed_at"] - a["observed_at"]) / 3600
                pattern["rate"] = max(pattern["rate"], (a["remaining"] - b["remaining"]) / hours)
    lines = [tr("pattern_title")]
    for source, p in sorted(sources.items()):
        total = sum(p["hours"])
        lines.append(tr("pattern_source", source=source, total=percent(decimal(total)),
                        pairs=p["pairs"]))
        for hour, drop in enumerate(p["hours"]):
            share = drop / total * 100 if total else 0
            lines.append(tr("pattern_hour", hour=f"{hour:02d}", share=percent(decimal(share)),
                            used=percent(decimal(drop))))
        if WORK_HOURS:
            share = p["evening"] / total * 100 if total else 0
            lines.append(tr("pattern_evening", start=WORK_HOURS[0][0], end=WORK_HOURS[0][1],
                            share=percent(decimal(share))))
        lines.append(tr("pattern_fastest", rate=percent(decimal(p["rate"]))))
    if not sources:
        lines.append(tr("pattern_empty"))
    return "\n".join(lines)


def codex_record(obj, timestamp):
    if isinstance(obj, dict):
        if "timestamp" in obj:
            timestamp = child_time(obj, timestamp)
        rates = obj.get("rate_limits")
        if isinstance(rates, dict):
            items = []
            for name in ("primary", "secondary"):
                try:
                    record = rates[name]
                    window = window_from_minutes(number(record["window_minutes"], 1))
                    reset = number(record["resets_at"])
                    remaining = 100 - number(record["used_percent"], 0, 100)
                    items.append(limit("Codex", "Codex", window,
                                       100 if reset <= time.time() else remaining,
                                       reset, timestamp, rates.get("plan_type")))
                except (KeyError, TypeError, ValueError):
                    continue
            if items:
                return {"limits": items, "data_time": timestamp}
        for child in obj.values():
            found = codex_record(child, timestamp)
            if found:
                return found
    elif isinstance(obj, list):
        for child in obj:
            found = codex_record(child, timestamp)
            if found:
                return found
    return None


def codex_home():
    return Path(os.environ.get("CODEX_HOME", str(HOME / ".codex"))).expanduser()


def codex_usage():
    sessions = codex_home() / "sessions"
    if not sessions.exists():
        return {}
    deadline, files, candidates = time.monotonic() + 1, [], []
    for path in sessions.rglob("*.jsonl"):
        if time.monotonic() >= deadline:
            break
        try:
            files.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for timestamp, path in sorted(files, reverse=True)[:5]:
        if time.monotonic() >= deadline:
            break
        try:
            for line in reversed(tail(path)):
                if time.monotonic() >= deadline:
                    break
                if '"rate_limits"' not in line:
                    continue
                try:
                    found = codex_record(json.loads(line), timestamp)
                except (ValueError, TypeError, KeyError, RecursionError):
                    continue
                if found:
                    candidates.append(found)
                    break
        except (OSError, UnicodeError, ValueError, RecursionError):
            continue
    if not candidates:
        raise ValueError("No Codex limit record found")
    return max(candidates, key=lambda record: record["data_time"])


def claude_windows(windows):
    if not isinstance(windows, list) or not windows:
        raise ValueError(tr("claude_windows_invalid"))
    result = []
    for window in windows:
        name = text(window["label"])
        used = number(window["percentUsed"], 0, 100)
        if window["resetsAt"] is None:
            continue
        iso_epoch(window["resetsAt"])
        result.append(dict(label=name, percentUsed=used, resetsAt=window["resetsAt"]))
    if not result:
        raise ValueError("No valid Claude windows")
    return result


def claude_save(raw):
    data = json.loads(raw)
    windows = claude_windows(data.get("plan", data)["windows"])
    record = {"saved_at": time.time(), "windows": windows}
    if not acquire_lock():
        raise ValueError(tr("refresh_running"))
    try:
        atomic_json(CLAUDE_CACHE, record)
        safe(write_history, claude_limits(windows, record["saved_at"]), record["saved_at"])
        return record
    finally:
        release_lock()


def claude_limits(windows, timestamp):
    return [limit("Claude", x["label"],
                  "weekly" if "weekly" in x["label"].lower() else "5-hour",
                  100 - x["percentUsed"], iso_epoch(x["resetsAt"]), timestamp)
            for x in windows]


def claude_usage():
    if not CLAUDE_CACHE.exists():
        return {"note": tr("claude_missing")} if CLAUDE_MISSING_IS_STALE else {}
    data = read_json(CLAUDE_CACHE)
    timestamp = number(data["saved_at"])
    items = claude_limits(claude_windows(data["windows"]), timestamp)
    return {"limits": items, "saved_at": timestamp, "data_time": timestamp}


def agy_models():
    if AGY is None:
        return {}
    items = {}
    for line in run_agy(["models"], 30).splitlines():
        if "\t" in line and not line.startswith("Fetching"):
            identifier, name = line.split("\t", 1)
            items["agy:" + text(identifier)] = text(name)
    if not items:
        raise ValueError("agy model list is empty")
    return items


def codex_models():
    items = {}
    for name in CODEX_MODEL_FILES:
        # In strict mode a missing or empty file makes the source fail, so that
        # --accept-models does not drop the other file's models from the baseline.
        path = codex_home() / name
        if not path.exists():
            if STRICT_CODEX_MODEL_FILES:
                raise ValueError(f"Codex model file missing: {name}")
            continue
        file_models = read_json(path)["models"]
        if not isinstance(file_models, list) or (STRICT_CODEX_MODEL_FILES and not file_models):
            raise ValueError(f"Codex model file empty or invalid: {name}")
        for model in file_models:
            identifier = text(model["slug"])
            items["codex:" + identifier] = text(model.get("display_name") or identifier)
    if not items:
        if not STRICT_CODEX_MODEL_FILES and not (codex_home() / "sessions").exists():
            return {}
        raise ValueError("Codex model list is empty")
    return items


def models():
    result = {"models": {}, "successful": [], "errors": {}}
    for source, operation in (("agy", agy_models), ("codex", codex_models)):
        data = safe(operation)
        if "error" in data:
            result["errors"][source] = data["error"]
        elif data:
            result["successful"].append(source)
            result["models"].update(data)
    return result


def model_dictionary(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid model dictionary")
    return {text(k): text(v) for k, v in data.items()}


def model_status(data, accept=False):
    raw = read_json(MODELS_FILE) if MODELS_FILE.exists() else {"models": {}}
    old = model_dictionary(raw["models"])
    # A baseline written automatically is reported until --accept-models.
    pending = raw.get("pending_accept", {})
    if not isinstance(pending, dict):
        raise ValueError("Invalid pending sources")
    pending = {text(k): number(v) for k, v in pending.items()}
    if accept and not data["successful"]:
        raise ValueError("No successful model source")
    result = {"new": {}, "removed": {}, "baseline_created": pending}
    baseline, new_baseline = dict(old), False
    for source in data["successful"]:
        before = {k: v for k, v in old.items() if k.startswith(source + ":")}
        now = {k: v for k, v in data["models"].items() if k.startswith(source + ":")}
        if not now:
            continue
        if before:
            result["new"].update({k: v for k, v in now.items() if k not in before})
            result["removed"].update({k: v for k, v in before.items() if k not in now})
        else:
            pending[source] = len(now)
            new_baseline = True
        if accept or not before:
            baseline = {k: v for k, v in baseline.items() if not k.startswith(source + ":")}
            baseline.update(now)
        if accept:
            pending.pop(source, None)
    if accept or new_baseline:
        stamp = datetime.now(TZ) if TZ else datetime.now().astimezone()
        atomic_json(MODELS_FILE, {"updated_at": stamp.isoformat(),
                                  "models": baseline, "pending_accept": pending})
    return result


def acquire_lock():
    global _lock
    if _lock is not None:
        return False
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                with LOCK.open("r") as file:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    status = os.fstat(file.fileno())
                    if (time.time() - status.st_mtime > 180 and
                            LOCK.stat().st_ino == status.st_ino):
                        LOCK.unlink()
                        continue
            except (OSError, ValueError):
                pass
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.write(fd, str(os.getpid()).encode())
            _lock = fd, os.fstat(fd).st_ino
            return True
        except BaseException:
            os.close(fd)
            raise
    return False


def release_lock():
    global _lock
    if _lock is None:
        return
    fd, inode = _lock
    try:
        if LOCK.stat().st_ino == inode and LOCK.read_text() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass
    finally:
        os.close(fd)
        _lock = None


def auto_hello_enabled():
    try:
        settings = read_json(SETTINGS)
        return isinstance(settings, dict) and settings.get("auto_hello") is True
    except (OSError, ValueError, UnicodeError, RecursionError):
        return False


def weekly_not_started(x, previous):
    try:
        if not all(y["source"] == "Antigravity" and y["window"] == "weekly" and
                   number(y["remaining"], 0, 100) == 100 for y in (x, previous)):
            return False
        if x["name"] != previous["name"]:
            return False
        interval = number(x["data_time"]) - number(previous["data_time"])
        shift = number(x["reset"]) - number(previous["reset"])
        # A fixed reset is not a sliding counter, even within the tolerance.
        return interval >= 60 and shift > 0 and abs(shift - interval) < 300
    except (KeyError, TypeError, ValueError):
        return False


def hello_model(group, data):
    preferences = {"Gemini Models": ("gemini-4-hydrogen-low", ("hydrogen", "flash")),
                   "Gemini 4 Argon": ("gemini-4-argon-low", ("argon",)),
                   "Claude and GPT models": ("gpt-oss-120b-medium", ("gpt-oss", "sonnet"))}
    if group not in preferences or "agy" not in data["successful"]:
        return None
    ids = [k[4:] for k in data["models"] if k.startswith("agy:")]
    preferred, keywords = preferences[group]
    if preferred in ids:
        return preferred
    for keyword in keywords:
        candidates = [k for k in ids if keyword in k.lower()]
        if candidates:
            return min(candidates, key=lambda k: ("low" not in k.lower(), k))
    return None


def send_hello(limits, model_data, previous_limits):
    if not auto_hello_enabled():
        return
    state = safe(read_json, HELLO_STATE)
    if not isinstance(state, dict):
        state = {}
    state = {k: v for k, v in state.items()
             if isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v)}
    for x in limits:
        group, timestamp = x["name"], time.time()
        if (not any(weekly_not_started(x, previous) for previous in previous_limits)
                or timestamp - state.get(group, 0) < 21600):
            continue
        model = hello_model(group, model_data)
        result = "no model"
        if model:
            try:
                run_agy(["-p", HELLO_PROMPT, "--model", model,
                         "--output-format", "json", "--print-timeout", "60s"], 90)
                result = "sent"
                state[group] = time.time()
                atomic_json(HELLO_STATE, state)
            except Exception as error:
                result = f"error: {type(error).__name__}"
        HELLO_LOG.parent.mkdir(parents=True, exist_ok=True)
        with HELLO_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(dict(time=timestamp, group=group, model=model, result=result),
                                  ensure_ascii=False) + "\n")


def last_hello():
    if not auto_hello_enabled():
        return None
    try:
        for line in reversed(tail(HELLO_LOG)):
            try:
                x = json.loads(line)
                number(x["time"])
                text(x["group"])
                text(x["result"])
                if x["model"] is not None:
                    text(x["model"])
                return dict(time=x["time"], group=text(x["group"]),
                            model=text(x["model"]) if x["model"] is not None else None,
                            result=text(x["result"]))
            except (ValueError, TypeError, KeyError):
                continue
    except (OSError, UnicodeError):
        pass
    return None


def hello_result(result):
    if result == "sent":
        return tr("hello_sent")
    if result == "no model":
        return tr("hello_no_model")
    if result.startswith("error: "):
        return tr("hello_error", error=result[len("error: "):])
    return result


def cached_source(section, source):
    if not isinstance(section, dict):
        raise ValueError("Invalid cached source schema")
    result = {}
    for field in ("error", "last_error", "note"):
        if field in section:
            result[field] = text(section[field])
    for field in ("saved_at", "data_time"):
        if field in section:
            result[field] = number(section[field])
    if "limits" in section:
        if not isinstance(section["limits"], list):
            raise ValueError("Invalid limits list")
        result["limits"] = [limit(source, x["name"], x["window"], x["remaining"],
                                  x["reset"], x["data_time"], x.get("plan"))
                            for x in section["limits"]]
    return result


def cached_models(data):
    status = data.get("models_status", {})
    result = {k: model_dictionary(status.get(k, {})) for k in ("new", "removed")}
    if "error" in status:
        result["error"] = text(status["error"])
    baseline = status.get("baseline_created", {})
    result["baseline_created"] = {text(k): number(v) for k, v in baseline.items()}
    return result


def read_cache():
    try:
        raw = read_json(CACHE)
        timestamp = number(raw["timestamp"])
        if raw["version"] != 2:
            return None
        data = {"version": 2, "timestamp": timestamp}
        for key, source in SOURCES:
            data[key] = safe(cached_source, raw.get(key, {}), source)
        data["models_status"] = safe(cached_models, raw)
        models_raw = raw.get("model_sources", {})
        try:
            data["model_sources"] = {
                "models": model_dictionary(models_raw.get("models", {})),
                "errors": model_dictionary(models_raw.get("errors", {}))}
        except (ValueError, TypeError, AttributeError):
            data["model_sources"] = {"errors": {"cache": "Invalid model schema"}}
        return data
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return None


def keep_last_good(new, old):
    if "error" in new and old.get("limits"):
        return dict(old, last_error=new["error"])
    return new


def collect():
    if not acquire_lock():
        return dict(read_cache() or {}, **{"note": tr("refresh_running")})
    try:
        # Read under the lock: a refresh that finished meanwhile must not be overwritten.
        old = read_cache() or {}
        data = {"version": 2, "timestamp": time.time()}
        for key, operation in (("agy", agy_usage), ("codex", codex_usage),
                               ("claude", claude_usage)):
            result = safe(operation)
            data[key] = keep_last_good(result, old.get(key, {}))
            if key == "agy":
                fresh_agy = "error" not in result
        data["model_sources"] = models()
        data["models_status"] = safe(model_status, data["model_sources"])
        # Keep the previous notices of a model source that failed this time.
        failed = data["model_sources"]["errors"]
        previous_status = old.get("models_status", {})
        if failed and "error" not in data["models_status"]:
            for field in ("new", "removed"):
                data["models_status"][field].update(
                    {k: v for k, v in previous_status.get(field, {}).items()
                     if k.partition(":")[0] in failed})
        if auto_hello_enabled() and fresh_agy:
            previous_agy = old.get("agy", {})
            previous_limits = (previous_agy.get("limits", [])
                               if not any(k in previous_agy for k in ("error", "last_error"))
                               else [])
            safe(send_hello, data["agy"].get("limits", []),
                 data["model_sources"], previous_limits)
        data["timestamp"] = time.time()
        atomic_json(CACHE, data)
        safe(write_history, all_limits(data), data["timestamp"])
        return data
    finally:
        release_lock()


def all_limits(data):
    return [x for k, _ in SOURCES for x in data.get(k, {}).get("limits", [])]


def display_name(x):
    return x["source"] + (" " + x["name"] if x["name"] != x["source"] else "")


def warnings(data, now=None):
    now = time.time() if now is None else now
    result = []
    for x in visible_limits(data, now):
        age = max(0, now - x["data_time"])
        if x["reset"] <= now or claude_stale(x, now):
            continue
        name, remaining = display_name(x), percent(x["remaining"])
        duration = format_duration(x["reset"] - now)
        suffix = (tr("measured_ago", age=format_duration(age))
                  if age > STALE_MEASUREMENT_SECONDS else "")
        kind = level(x, now)
        if x["remaining"] < LOW_THRESHOLD:
            result.append(tr("warn_low", label=tr("label_low"), name=name,
                             window=window_label(x["window"]), percent=remaining,
                             date=format_date(x["reset"]), duration=duration, suffix=suffix))
        elif kind:
            target, last = target_and_last(x)
            label = tr("label_" + kind)
            if now >= target:
                message = tr("warn_missed", label=label, name=name, percent=remaining,
                             target=format_date(target), date=format_date(x["reset"]),
                             duration=duration, work=work_time(now, last),
                             fast=percent(decimal(capacity(x, now, last, "fast"))))
            else:
                message = tr("warn_pace", label=label, name=name, percent=remaining,
                             target=format_date(target), work=work_time(now, target),
                             normal=percent(decimal(capacity(x, now, target, "normal"))),
                             fast=percent(decimal(capacity(x, now, target, "fast"))))
            if last < x["reset"]:
                message += tr("warn_subscription_end", date=format_date(last))
            result.append(message + suffix)
    age = max(0, now - data.get("timestamp", now))
    suffix = tr("measured_ago", age=format_duration(age)) if age > STALE_MEASUREMENT_SECONDS else ""
    status = data.get("models_status", {})
    new_models = [tr("warn_new_model", label=tr("label_new_model"), name=v, id=k, suffix=suffix)
                  for k, v in status.get("new", {}).items()]
    baselines = [tr("warn_new_source", label=tr("label_new_source"), source=k, count=count,
                    suffix=suffix) for k, count in status.get("baseline_created", {}).items()]
    return new_models + baselines + result


def warning_kind(warning):
    label = warning.partition(":")[0]
    for kind in WARNING_COLORS:
        if label == tr("label_" + kind):
            return kind
    return ""


def short_warnings(alerts):
    """For the session hook: new models on one line, then at risk, off pace, low."""
    new = [s for s in alerts if s.startswith(tr("label_new_model") + ": ")]
    other = [s for s in alerts if s not in new]
    other.sort(key=lambda s: SHORT_ORDER.get(warning_kind(s), 2))
    if len(new) > 1:
        start = len(tr("label_new_model") + ": ")
        names = ", ".join(s[start:].split(tr("new_model_separator"))[0] for s in new)
        new = [tr("warn_new_models", label=tr("label_new_model"), count=len(new), names=names)]
    return new + other


def claude_stale_hint(data, now=None):
    now = time.time() if now is None else now
    claude = data.get("claude", {})
    timestamp = claude.get("saved_at")
    if timestamp is None and not CLAUDE_MISSING_IS_STALE:
        return ""
    if (not timestamp or now - timestamp > CLAUDE_STALE_SECONDS or
            any(x["reset"] <= now for x in claude.get("limits", []))):
        age = format_duration(now - timestamp) if timestamp else tr("unknown")
        return tr("claude_stale", label=tr("label_claude_stale"), age=age)
    return ""


def cache_fresh(data):
    return (data is not None and 0 <= time.time() - data["timestamp"] < CACHE_MINUTES * 60
            and not any(x["reset"] <= time.time() for x in data.get("agy", {}).get(
                "limits", [])))


def config_args():
    return ["--config", str(CONFIG)] if CONFIG else []


def refresh_background():
    subprocess.Popen([sys.executable, str(ROOT / "tidewatch.py"), "--refresh", "--quiet"]
                     + config_args(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def prepare(args):
    data = read_cache()
    fresh = cache_fresh(data)
    sources = (("codex", codex_usage), ("claude", claude_usage))
    # Fast modes never wait for agy, even with --refresh.
    if args.short or args.menubar:
        if not fresh or args.refresh:
            safe(refresh_background)
        data = dict(data or {})
        # A blocked read under ~/.codex cannot be interrupted; fast modes use the cache.
        sources = sources[1:]
    elif args.refresh or not fresh:
        data = collect()
        return data, "note" in data
    for k, operation in sources:
        data[k] = keep_last_good(safe(operation), data.get(k, {}))
    return data, True


def source_notes(data, now=None):
    now = time.time() if now is None else now
    lines = []
    for k, name in SOURCES:
        items = data.get(k, {}).get("limits", [])
        if items and all(no_subscription(x, now) for x in items):
            lines.append(tr("no_subscription", source=name))
            continue
        for field in ("error", "last_error", "note"):
            if field in data.get(k, {}):
                lines.append(f"{k}: {data[k][field]}")
    status = data.get("models_status", {})
    if "error" in status:
        lines.append(tr("models_error", error=status["error"]))
    for k, error in data.get("model_sources", {}).get("errors", {}).items():
        lines.append(tr("source_models_error", source=k, error=error))
    if "note" in data:
        lines.append(data["note"])
    return lines


def table_name(x):
    if x["source"] == "Claude":
        if "5-hour" in x["name"].lower():
            return window_label("5-hour")
        if "weekly" in x["name"].lower():
            suffix = x["name"][x["name"].lower().index("weekly") + len("weekly"):]
            suffix = suffix.replace("·", "").strip()
            return tr("table_weekly") + (
                f" ({suffix})" if suffix and suffix.lower() != "all models" else "")
        return x["name"]
    if x["source"] == "Codex":
        return tr("table_weekly") if x["window"] == "weekly" else window_label(x["window"])
    return GROUP_NAMES.get(x["name"], x["name"]) + " " + (
        tr("table_group_weekly") if x["window"] == "weekly" else window_label(x["window"]))


def outlook(data, now=None):
    now = time.time() if now is None else now
    result = []
    for x in visible_limits(data, now):
        if x["window"] != "weekly" or sub_limit(x):
            continue
        _, last = target_and_last(x)
        if x["reset"] <= now or last <= now:
            continue
        stale = claude_stale(x, now)
        unused = max(0, x["remaining"] - capacity(x, now, last, "normal"))
        daily = x["remaining"] / max(1, (last - now) / 86400)
        result.append(dict(source=x["source"], name=x["name"], remaining=x["remaining"],
                           reset=x["reset"], last=last, unused=None if stale else unused,
                           daily=None if stale else daily, stale=stale,
                           data_time=x["data_time"]))
    return result


def facts_line(data, now=None):
    now = time.time() if now is None else now
    parts = []
    for x in sorted(outlook(data, now), key=lambda x: x["last"]):
        name = (x["source"] if x["source"] in ("Claude", "Codex") else
                GROUP_NAMES.get(x["name"], x["name"]))
        part = tr("facts_item", name=name, percent=percent(f"{x['remaining']:g}"))
        if x["stale"]:
            part += tr("facts_stale")
        else:
            part += tr("facts_left", duration=format_duration(x["last"] - now))
            part += (tr("facts_no_waste") if round(x["unused"]) == 0 else
                     tr("facts_waste", percent=percent(f"{x['unused']:.0f}")))
            if x["last"] - now > 2 * 86400 and round(x["daily"]) > 0:
                part += tr("facts_daily", percent=percent(f"{x['daily']:.0f}"))
        age = max(0, now - x["data_time"])
        if x["source"] in ("Codex", "Antigravity") and age > STALE_MEASUREMENT_SECONDS:
            part += tr("measured_ago", age=format_duration(age))
        parts.append(part)
    return tr("facts", items=" · ".join(parts)) if parts else ""


def guide_lines():
    lines = []
    if ROSTER_FILE is not None and ROSTER_FILE.is_file():
        lines.append(tr("roster", path=ROSTER_FILE))
    if BACKLOG_FILE is not None:
        try:
            count = sum(1 for _ in active_backlog_lines())
            lines.append(tr("backlog", count=count, path=BACKLOG_FILE) if count else
                         tr("backlog_empty", path=BACKLOG_FILE))
        except (OSError, UnicodeError):
            pass
    return lines


def warning_color(warning):
    return WARNING_COLORS.get(warning_kind(warning), "")


def menubar(data):
    now, alerts = time.time(), warnings(data)
    items = visible_limits(data, now)
    unused = {(x["source"], x["name"]): x for x in outlook(data, now)}
    warning_colors = [warning_color(s) for s in alerts]
    title_color = next((r for r in WARNING_COLORS.values() if r and r in warning_colors), "")
    title = "AI" + (f" | color={title_color}" if title_color else "")
    cells, colors = [list(MESSAGES["menu_headers"])], ["gray"]
    for x in items:
        expired = x["reset"] <= now
        old = claude_stale(x, now)
        duration = (tr("menu_no_fresh_data") if expired else
                    format_duration(x["reset"] - now, True))
        share = (unused.get((x["source"], x["name"]), {}).get("unused")
                 if x["window"] == "weekly" else None)
        cells.append([x["source"], table_name(x),
                      "-" if expired else percent(x["remaining"]),
                      duration + (tr("menu_stale_record") if old and not expired else ""),
                      tr("menu_reset") if expired else format_date(x["reset"]),
                      "-" if expired else per_evening(x, now) +
                      (" ~" + percent(f"{share:.0f}") if share is not None and round(share) > 0
                       else "")])
        colors.append("gray" if expired or old else WARNING_COLORS.get(level(x, now), ""))
    widths = [max(display_width(s[i]) for s in cells) for i in range(6)]
    lines = [title, "---"]
    for line, color in zip(cells, colors):
        row = "  ".join(pad(v, widths[i], right=i == 2) if i < 5 else v
                        for i, v in enumerate(line))
        lines.append(row + " | font=Menlo size=12 trim=false" +
                     (f" color={color}" if color else ""))
    lines.append(tr("menu_unused_legend") + " | color=gray")
    ages = [tr("menu_age", name=name, age=format_duration(now - data[k].get("data_time", 0)))
            for k, name in SOURCES if data.get(k, {}).get("data_time")
            and any(x["source"] == name for x in items)]
    if ages:
        lines.append(tr("menu_measured", items=" · ".join(ages)) + " | color=gray")
    lines.extend(text(s) + " | color=gray" for s in source_notes(data))
    lines.extend(s + (f" | color={r}" if r else "") for s, r in zip(alerts, warning_colors))
    lines.extend(s + " | color=gray" for s in backlog_lines(data, now))
    if claude_stale_hint(data):
        lines.append(claude_stale_hint(data) + " | color=gray")
    params = ["--refresh", "--quiet"] + config_args()
    lines.extend(["---", f'{tr("menu_refresh")} | bash="{sys.executable}" '
                  f'param1="{ROOT / "tidewatch.py"}" ' +
                  "".join(f'param{i}="{v}" ' if " " in v else f"param{i}={v} "
                          for i, v in enumerate(params, 2)) +
                  "terminal=false refresh=true"])
    return "\n".join(lines)


def render_text(data, from_cache=False):
    now = time.time()
    age = format_duration(now - data.get("timestamp", now))
    lines = [tr("text_data_cache", age=age) if from_cache else tr("text_data_now")]
    items = visible_limits(data, now)
    for k, name in SOURCES:
        source_limits = [x for x in items if x["source"] == name]
        if source_limits:
            lines.append(name + ":")
        for x in source_limits:
            detail = (tr("text_reset_no_data") if x["reset"] <= now else
                      f"{percent(x['remaining'])} · {format_date(x['reset'])} "
                      f"({format_duration(x['reset'] - now)})")
            lines.append(f"  {x['name']} {window_label(x['window'])}: {detail}")
        if k == "agy":
            hello = last_hello()
            if hello:
                lines.append(tr("text_last_hello", group=hello["group"],
                                date=format_date(hello["time"]), model=hello["model"],
                                result=hello_result(hello["result"])))
    status = data.get("models_status", {})
    lines.append(tr("text_models", count=len(data.get("model_sources", {}).get("models", {}))))
    for source, count in status.get("baseline_created", {}).items():
        lines.append(tr("text_baseline", source=source, count=count))
    for k, v in status.get("new", {}).items():
        lines.append(tr("text_new", name=v, id=k))
    for k, v in status.get("removed", {}).items():
        lines.append(tr("text_removed", name=v, id=k))
    lines.extend(source_notes(data))
    if facts_line(data):
        lines.append(facts_line(data))
    lines.extend(guide_lines())
    lines.append(tr("text_warnings"))
    lines.extend(warnings(data) or [tr("text_none")])
    lines.extend(backlog_lines(data))
    if claude_stale_hint(data):
        lines.append(claude_stale_hint(data))
    return "\n".join(lines)


def short_output(data):
    alerts = short_warnings(warnings(data))
    selected = alerts[:3]
    if len(alerts) > 3:
        selected[-1] += tr("more_warnings", count=len(alerts) - 3)
    lines = ["; ".join(selected[:2])] if selected else []
    if len(selected) == 3:
        lines.append(selected[2])
    if SHORT_SUMMARY:
        if facts_line(data):
            lines.append(facts_line(data))
        lines.extend(guide_lines())
    if claude_stale_hint(data):
        lines.append(claude_stale_hint(data))
    return "\n".join(lines)


def json_data(data):
    now = time.time()
    result = dict(data, warnings=warnings(data), last_hello=last_hello(), outlook=outlook(data))
    for k, _ in SOURCES:
        result[k] = dict(data.get(k, {}))
        result[k]["limits"] = [dict(x, reset_text=format_date(x["reset"]),
                                    time_left=format_duration(x["reset"] - now),
                                    expired=x["reset"] <= now)
                               for x in data.get(k, {}).get("limits", [])]
    return result


def accept_models():
    if not acquire_lock():
        raise ValueError(tr("refresh_running"))
    try:
        data = models()
        status = model_status(data, True)
        cache = read_cache()
        if cache:
            # Notices of a source that failed were not accepted; keep them.
            old = cache.get("models_status", {})

            def keep(field):
                return {k: v for k, v in old.get(field, {}).items()
                        if k.partition(":")[0] not in data["successful"]}
            cache["models_status"] = {"new": keep("new"), "removed": keep("removed"),
                                      "baseline_created": status["baseline_created"]}
            # Keep the previous model list of a source that failed.
            previous = cache.get("model_sources", {}).get("models", {})
            kept = {k: v for k, v in previous.items() if k.partition(":")[0] in data["errors"]}
            kept.update(data["models"])
            cache["model_sources"] = dict(data, models=kept)
            atomic_json(CACHE, cache)
        return (tr("accept_result", sources=", ".join(data["successful"]),
                   added=", ".join(status["new"].values()) or tr("none"),
                   removed=", ".join(status["removed"].values()) or tr("none")) +
                "".join(f"\n{k}: {v}" for k, v in data["errors"].items()))
    finally:
        release_lock()


def set_data_dir(path):
    global DATA, CACHE, CLAUDE_CACHE, MODELS_FILE, HELLO_STATE, HELLO_LOG, LOCK
    global HISTORY, HISTORY_ARCHIVE
    DATA = path
    CACHE, CLAUDE_CACHE = DATA / "limits.json", DATA / "claude-usage.json"
    MODELS_FILE, LOCK = DATA / "known-models.json", DATA / "refreshing"
    HELLO_STATE, HELLO_LOG = DATA / "hello.json", DATA / "hello-log.jsonl"
    HISTORY, HISTORY_ARCHIVE = DATA / "history.jsonl", DATA / "history-archive"


def apply_settings(settings, base, data_dir=True):
    """Apply a settings dictionary; relative paths are resolved against base.
    data_dir is honoured only for an explicit settings file."""
    g = globals()
    if not isinstance(settings, dict):
        raise ValueError("Settings must be a JSON object")
    if "data_dir" in settings and data_dir:
        set_data_dir(base / Path(text(settings["data_dir"])).expanduser())
    if "timezone_offset_hours" in settings:
        g["TZ"] = timezone(timedelta(hours=number(settings["timezone_offset_hours"], -14, 14)))
    for key, name in (("work_hours", "WORK_HOURS"), ("night_hours", "NIGHT_HOURS")):
        if key in settings:
            hours = settings[key]
            if not isinstance(hours, list) or not all(
                    isinstance(pair, list) and len(pair) == 2 for pair in hours):
                raise ValueError(f"{key} must be a list of [start, end] pairs")
            # Validate and normalise to two-digit HH:MM.
            g[name] = [tuple(datetime.strptime(text(v), "%H:%M").strftime("%H:%M")
                             for v in pair) for pair in hours]
    for key, name in (("normal_rate", "NORMAL_RATE"), ("night_rate", "NIGHT_RATE"),
                      ("fast_rate", "FAST_RATE")):
        if key in settings:
            g[name] = dict(g[name], **{text(k): number(v) for k, v in settings[key].items()})
    for key, name in (("target_hours_before", "TARGET_HOURS_BEFORE"),
                      ("warning_horizon_days", "WARNING_HORIZON_DAYS"),
                      ("claude_5h_cap", "CLAUDE_5H_CAP")):
        if key in settings:
            g[name] = number(settings[key])
    if "subscription_end" in settings:
        g["SUBSCRIPTION_END"] = {text(k): text(v) for k, v in settings["subscription_end"].items()}
    for key, name in (("backlog_file", "BACKLOG_FILE"), ("roster_file", "ROSTER_FILE")):
        if key in settings:
            g[name] = (base / Path(text(settings[key])).expanduser()
                       if settings[key] is not None else None)
    if "hello_prompt" in settings:
        g["HELLO_PROMPT"] = text(settings["hello_prompt"])
    for key, name in (("short_summary", "SHORT_SUMMARY"),
                      ("claude_missing_is_stale", "CLAUDE_MISSING_IS_STALE"),
                      ("strict_codex_model_files", "STRICT_CODEX_MODEL_FILES")):
        if key in settings:
            if not isinstance(settings[key], bool):
                raise ValueError(f"{key} must be true or false")
            g[name] = settings[key]
    if "language" in settings:
        if settings["language"] is not None and not isinstance(settings["language"], str):
            raise ValueError("language must be a language code such as \"tr\"")
        g["LANGUAGE"] = settings["language"]
    if "messages" in settings:
        messages = settings["messages"]
        if isinstance(messages, str):
            messages = read_json(base / Path(messages).expanduser())
        if not isinstance(messages, dict) or not set(messages) <= set(ENGLISH):
            raise ValueError("Unknown message keys")
        g["MESSAGE_OVERRIDES"] = dict(messages)
    if "language" in settings or "messages" in settings:
        load_messages(LANGUAGE)


def configure(path, explicit=True):
    """Explicit file (--config, TIDEWATCH_CONFIG): data lives next to it by default.
    The standard file keeps the default data folder unless it sets data_dir."""
    global CONFIG, SETTINGS
    path = Path(path).expanduser().resolve()
    settings = read_json(path)
    if explicit:
        set_data_dir(path.parent)
    apply_settings(settings, path.parent)
    SETTINGS = path
    # Background refreshes find the standard file on their own.
    CONFIG = path if explicit else None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Show AI usage limits")
    for name, help_text in (
            ("json", "Print structured JSON output"),
            ("short", "Print up to three warnings without waiting for agy"),
            ("menubar", "Print the SwiftBar menu without waiting for agy"),
            ("refresh", "Refresh cached usage and model lists"),
            ("quiet", "Suppress output"),
            ("accept-models", "Accept current model lists as the baseline"),
            ("claude-save", "Save Claude usage JSON from standard input"),
            ("backfill-history", "Import past Codex measurements into the history once"),
            ("usage-pattern", "Report usage by hour from the history")):
        parser.add_argument("--" + name, action="store_true", help=help_text)
    parser.add_argument("--config", help="Settings file (default: ~/.config/tidewatch/"
                        "settings.json, then data/settings.json); also TIDEWATCH_CONFIG")
    args = parser.parse_args(argv)
    try:
        config = args.config or os.environ.get("TIDEWATCH_CONFIG")
        if config:
            configure(config)
        elif USER_SETTINGS.is_file():
            configure(USER_SETTINGS, explicit=False)
        elif SETTINGS.is_file():
            # The default settings file is optional; a broken one falls back to defaults.
            try:
                apply_settings(read_json(SETTINGS), SETTINGS.parent, data_dir=False)
            except (OSError, ValueError, TypeError, AttributeError, UnicodeError,
                    RecursionError):
                pass
        load_messages(os.environ.get("TIDEWATCH_LANG") or LANGUAGE or system_language())
        fast = args.short or args.menubar
        if args.claude_save and not fast:
            claude_save(sys.stdin.read())
            return 0
        if args.backfill_history and not fast:
            output = backfill_history()
        elif args.usage_pattern and not fast:
            output = usage_pattern()
        elif args.accept_models and not fast:
            output = accept_models()
        else:
            data, from_cache = prepare(args)
            if args.short:
                output = short_output(data)
            elif args.menubar:
                output = menubar(data)
            elif args.json:
                output = json.dumps(json_data(data), ensure_ascii=False, indent=2)
            else:
                output = render_text(data, from_cache)
        if output and not args.quiet:
            print(output)
        return 0
    except BaseException as error:
        if not args.quiet and not args.short:
            if args.menubar:
                print(f"AI\n---\n{tr('menu_error')} | color=gray")
            else:
                prefix = (tr("error_claude_save") if args.claude_save else
                          tr("error_accept") if args.accept_models else tr("error_other"))
                print(f"{prefix}: {str(error) or tr('error_no_details')}", file=sys.stderr)
        if args.short or args.menubar:
            return 0
        return 0 if args.quiet and not (args.accept_models or args.claude_save) else 1


if __name__ == "__main__":
    raise SystemExit(main())
