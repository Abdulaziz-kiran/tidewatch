# tidewatch

*AI limits ebb during the week and refill at a fixed hour, like a tide. This watches the tide so none of it goes to waste.*

See how much of your Claude, Codex and Antigravity limits is left, in one place: terminal, JSON, or the macOS menu bar. It compares what is left of each weekly limit with the hours you actually plan to work before it resets, warns you when quota is likely to go unused, when a limit runs low, and when a new model appears.

A single Python file with no dependencies. By default it never sends a prompt to any model.

> Unofficial. Not affiliated with Anthropic, OpenAI or Google. It relies on local files and CLI output that are not stable public interfaces, so a tool update can break it.

## Status of each source

| Tool | How it is read | Automatic? |
|---|---|---|
| Antigravity | `agy -p /usage` and `agy models` | Yes |
| Codex | The latest `rate_limits` record in `~/.codex/sessions` | Yes, as of your last Codex session |
| Claude | JSON you save with `--claude-save` | **No** |

The Claude row is the main limitation. I have not found a way to read plan usage from a plain shell, so Claude data is only as fresh as your last save. Pull requests are welcome.

You do not need all three tools. Anything not installed is skipped.

## Install

Requires Python 3.9+ on macOS or Linux. Developed and used on macOS; Linux is untested.

```bash
git clone https://github.com/Abdulaziz-kiran/tidewatch.git
cd tidewatch
python3 tidewatch.py --refresh
```

## Usage

```bash
python3 tidewatch.py                 # readable summary (30-minute cache)
python3 tidewatch.py --refresh       # measure now
python3 tidewatch.py --short         # warnings only; prints nothing if all is well
python3 tidewatch.py --json          # for scripts
python3 tidewatch.py --menubar       # SwiftBar format
python3 tidewatch.py --accept-models # treat the current model list as known
python3 tidewatch.py --backfill-history  # import past Codex measurements once
python3 tidewatch.py --usage-pattern     # when do you use your limits, by hour
python3 tidewatch.py --config my.json    # use another settings file
```

## Warnings

Each weekly limit has a target: 24 hours before it resets (or before a subscription ends, if you set one). Within five days of the target, what is left is compared with the capacity of your planned working hours until then: evenings (default 19:00–22:30) at a normal pace, and nights (default 22:30–04:00) standing for unattended agent work at a slower pace. The paces are rough estimates you can change.

| Label | Meaning |
|---|---|
| `AT RISK` | Even a fast pace would not use up what is left before the target, or the target has passed |
| `OFF PACE` | A normal pace would not use it up, a fast pace would |
| `LOW` | Under 10% left |
| `NEW MODEL` | A model appeared that was not in your known list. Clear it with `--accept-models` |
| `NEW SOURCE` | A model source was seen for the first time and became the baseline |
| `CLAUDE DATA STALE` | Your saved Claude data is over 6 hours old or has passed its reset |

Claude's per-model weekly limits (such as `Weekly · Sonnet`) get no level, because that usage also counts against the all-models limit.

The plain output also has a facts line, for example `LIMITS: Gemini 62%, 3d 4h left, ~12% goes unused, ~19% a day`: what is left, the time until last use, the share likely to go unused at a normal pace, and a daily budget. It states facts only; deciding what to do is up to you (or your agent).

## Saving Claude usage

Pipe JSON to `--claude-save`. `resetsAt` needs a timezone, and windows with a `null` reset are ignored. A `{"plan": {"windows": [...]}}` wrapper is also accepted.

```bash
python3 tidewatch.py --claude-save <<'EOF'
{"windows": [
  {"label": "5-hour", "percentUsed": 42, "resetsAt": "2026-11-09T20:00:00Z"},
  {"label": "Weekly · All models", "percentUsed": 18, "resetsAt": "2026-11-14T08:00:00Z"}
]}
EOF
```

## Menu bar (macOS)

Install [SwiftBar](https://github.com/swiftbar/SwiftBar), then set its plugin folder to this repository's `swiftbar/` directory. Keep only the plugin in that folder, because SwiftBar treats every file there as a plugin.

The menu title is `AI`. It turns red for `AT RISK`, orange for `OFF PACE` and blue for a new model or source. The table shows each limit, what is left, when it resets and how much you would need to use per evening; `~N%` after a weekly limit is the share likely to go unused at this pace.

## Warnings at Claude Code session start (optional)

Add this to `~/.claude/settings.json`, using your own path:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "python3 /path/to/tidewatch.py --short", "timeout": 5}]}
    ]
  }
}
```

`--short` only reads cached files and refreshes in the background, so it does not slow session start.

## Automatic hello for Antigravity (optional, off by default)

Antigravity's weekly counter appears to start only at a group's first message, so an unused window can sit idle. To have the tool start it for you, create `data/settings.json`:

```json
{"auto_hello": true}
```

When a group's weekly counter looks unstarted across two measurements, a background refresh sends it the single word `hello` (or your `hello_prompt`) with the cheapest available model, at most once per group every 6 hours. Attempts are logged to `data/hello-log.jsonl`.

This is experimental. It uses a small amount of your quota, it also starts that group's 5-hour window, and the assumption about the weekly counter has not been confirmed.

## Settings

Settings live in `data/settings.json`, or in any file you pass with `--config` or the `TIDEWATCH_CONFIG` environment variable. With an explicit settings file, data is kept next to it (or in `data_dir`), and background refreshes and the menu's Refresh item use the same file. Every key is optional:

```json
{
  "data_dir": "data",
  "timezone_offset_hours": 0,
  "work_hours": [["19:00", "22:30"]],
  "night_hours": [["22:30", "04:00"]],
  "normal_rate": {"Claude": 3, "Codex": 3, "Antigravity": 3},
  "night_rate": {"Claude": 1, "Codex": 1, "Antigravity": 1},
  "fast_rate": {"Claude": 6, "Codex": 20, "Antigravity": 10},
  "claude_5h_cap": 20,
  "target_hours_before": 24,
  "warning_horizon_days": 5,
  "subscription_end": {"Codex": "2026-12-01T00:00:00+00:00"},
  "backlog_file": "backlog.md",
  "roster_file": "models.md",
  "short_summary": false,
  "claude_missing_is_stale": false,
  "strict_codex_model_files": false,
  "auto_hello": false,
  "hello_prompt": "hello",
  "messages": "messages.json"
}
```

- Rates are percent of a limit per hour. `claude_5h_cap` caps Claude's fast pace per started 5-hour window.
- `subscription_end`: after this moment a source is hidden until a newer measurement from a paid plan arrives. Without it, only an explicit `free` plan is hidden.
- `backlog_file`: a Markdown list of spare tasks, one per line as `- [Claude] task`; for each source with a warning, the first task is shown in the menu and the plain output. Items under `## Done` are ignored.
- `roster_file`: a file you keep about which model does what; `--short` and the plain output point to it.
- `short_summary`: also print the facts line and these file pointers in `--short`, which is handy for a coding agent's session-start hook.
- `strict_codex_model_files`: treat a missing or empty Codex model cache file as a failed source, so that `--accept-models` cannot drop the other file's models.
- `messages`: a JSON file (or object) that replaces any of the output texts, for example to translate them. The keys are in `MESSAGES` at the top of `tidewatch.py`.
- `timezone_offset_hours`: a fixed UTC offset for dates and working hours; by default the system's local time is used.

## History

Every refresh and `--claude-save` appends changed values (or one every six hours) to `data/history.jsonl`; files over 5 MB move to `data/history-archive/` and are never deleted. `--backfill-history` imports the raw percentages from all your Codex session files once. `--usage-pattern` reports, from that history, how much each source dropped in each hour of the day, the share used in your first working window and the fastest rate seen over at least two hours. Fast modes (`--short`, `--menubar`) never read or write the history.

## What it touches

- Writes only to its data folder (`data/` next to the script by default). Delete that folder to reset.
- Reads the end of the five most recent Codex session files, and parses only lines containing `rate_limits` (`--backfill-history` reads all of them once). Those logs hold your conversations, but only limit numbers are stored. Fast modes use the cached Codex numbers, at most about 30 minutes old.
- Reads Codex's model cache files. Honours `CODEX_HOME`.
- Runs `agy` only for `/usage` and `models`, at most once every 30 minutes unless you ask for `--refresh`. The optional automatic hello is the only other call allowed. Anything else is refused in code.
- Makes no network requests itself. `agy` does its own.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary directories and fake processes.

## License

[MIT](LICENSE)
