"""All I/O uses a temporary directory; processes are mocked and forbidden by default."""
from datetime import timezone
from datetime import timedelta
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import runpy
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import tidewatch as m

FIXTURES = Path(__file__).parent / "fixtures"


class LimitTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex")})
        environment.start()
        self.addCleanup(environment.stop)
        self.patch_value("AGY", "/test/bin/agy")
        self.now = 1794211200.0
        paths = dict(ROOT=self.root, HOME=self.root, DATA=self.root / "data")
        for name, file in (("CACHE", "limits.json"), ("CLAUDE_CACHE", "claude-usage.json"),
                          ("MODELS_FILE", "known-models.json"), ("LOCK", "refreshing"),
                          ("SETTINGS", "settings.json"), ("HELLO_STATE", "hello.json"),
                          ("HELLO_LOG", "hello-log.jsonl"), ("HISTORY", "history.jsonl"),
                          ("HISTORY_ARCHIVE", "history-archive")):
            paths[name] = paths["DATA"] / file
        for name, value in paths.items():
            self.patch_value(name, value)
        # Working hours are local time; pin the zone so results do not depend on the machine.
        self.patch_value("TZ", timezone(timedelta(hours=6)))
        self.patch_value("SUBSCRIPTION_END", {})
        self.patch_value("BACKLOG_FILE", None)
        self.patch_value("ROSTER_FILE", None)
        self.timestamp = patch.object(m.time, "time", return_value=self.now)
        self.timestamp.start()
        self.addCleanup(self.timestamp.stop)
        self.process = patch.object(m.subprocess, "Popen",
                                    side_effect=AssertionError("Real process"))
        self.popen = self.process.start()
        self.addCleanup(self.process.stop)
        self.addCleanup(m.release_lock)

    def patch_value(self, name, value):
        patcher = patch.object(m, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def limit(self, **changes):
        return dict(dict(source="Codex", name="Codex", window="weekly", remaining=40,
                         reset=self.now + 3600, data_time=self.now, plan=None), **changes)

    def data(self, *limits, **other):
        return dict(dict(version=2, timestamp=self.now, codex={"limits": list(limits)}), **other)

    def write_json(self, path, data):
        m.atomic_json(path, data)

    def cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = m.main(list(args))
        return code, output.getvalue(), error.getvalue()

    def models(self, source="agy", **items):
        return {"successful": [source], "models": {source + ":" + k: v for k, v in items.items()},
                "errors": {}}

    def mock_collection(self, agy=None, codex=None, claude=None, models=None):
        for name, value in (("agy_usage", agy), ("codex_usage", codex),
                          ("claude_usage", claude), ("models", models)):
            default = ({"successful": [], "models": {}, "errors": {}} if name == "models"
                          else {"error": "missing"})
            self.patch_value(name, Mock(return_value=default if value is None else value))

    def test_warning_messages_and_boundaries(self):
        for hours in (1, 12, 24):
            with self.subTest(hours=hours):
                x = self.limit(reset=self.now + hours * 3600, remaining=30)
                self.assertTrue(m.warnings(self.data(x))[0].startswith(
                    "AT RISK: Codex weekly 30% unused; target "))
        for x in (self.limit(reset=self.now, remaining=1),
                  self.limit(window="5-hour", remaining=100),
                  self.limit(reset=self.now + 7 * 86400)):
            self.assertEqual([], m.warnings(self.data(x)))
        x = self.limit(remaining=9, data_time=self.now - 10800)
        warning = m.warnings(self.data(x))[0]
        self.assertEqual("LOW: Codex weekly 9% left, resets " +
                         m.format_date(x["reset"]) + " (1h 0m) (measured 3h 0m ago)", warning)
        self.assertNotIn("Codex Codex", warning)
        self.assertTrue(m.warnings(self.data(self.limit(remaining=10))))
        new = {"models_status": {"new": {"agy:x": "X"}}}
        self.assertEqual(["NEW MODEL: X (agy:x): review which models you use, "
                          "then run tidewatch.py --accept-models"], m.warnings(new))

    def test_claude_warning_and_reminder(self):
        x = self.limit(source="Claude", name="Weekly", remaining=65, reset=self.now + 9 * 3600)
        data = self.data(claude={"saved_at": self.now, "limits": [x]})
        self.assertTrue(m.warnings(data)[0].startswith("AT RISK:"))
        self.assertEqual("", m.claude_stale_hint(data))
        for age in (21600, 21601):
            x["data_time"] = self.now - age
            data["claude"]["saved_at"] = x["data_time"]
            self.assertEqual(age == 21600, bool(m.warnings(data)))
            self.assertEqual(age > 21600, bool(m.claude_stale_hint(data)))
        data["claude"]["saved_at"] = self.now
        x["reset"] = self.now - 1
        self.assertTrue(m.claude_stale_hint(data))
        self.assertEqual("", m.claude_stale_hint({}))

    def test_new_model_priority_age_and_color(self):
        data = self.data(*(self.limit(name=str(i), remaining=1) for i in range(4)),
                         timestamp=self.now - 10800,
                         models_status={"new": {"agy:x": "X"}})
        warnings = m.warnings(data)
        self.assertTrue(warnings[0].startswith("NEW MODEL: X (agy:x)"))
        self.assertTrue(warnings[0].endswith("(measured 3h 0m ago)"))
        self.assertEqual("#0A84FF", m.warning_color(warnings[0]))
        self.patch_value("prepare", Mock(return_value=(data, True)))
        self.assertIn(warnings[0], self.cli("--short")[1])

    def test_short_truncation_reminder_and_silence(self):
        data = {"models_status": {"new": {str(i): "X" for i in range(5)}},
                "claude": {"saved_at": self.now - 25200}}
        self.patch_value("prepare", Mock(return_value=(data, True)))
        code, output, error = self.cli("--short")
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(1, output.count("NEW MODEL"))
        self.assertIn("NEW MODEL (5): X (0), X (1), X (2), X (3), X (4): review", output)
        self.assertIn("CLAUDE DATA STALE", output)
        self.assertEqual(2, len(output.splitlines()))
        self.assertEqual((0, "", ""), self.cli("--short", "--quiet"))
        m.prepare.return_value = ({"claude": {"saved_at": self.now}}, True)
        self.assertEqual((0, "", ""), self.cli("--short"))
        m.prepare.side_effect = KeyboardInterrupt
        self.assertEqual((0, "", ""), self.cli("--short"))
        self.assertNotIn("Traceback", self.cli("--menubar")[1])

    def test_agy_fixture_and_command(self):
        with patch.object(m, "run_agy", return_value=(FIXTURES / "agy-usage.json").read_text()
                          ) as call:
            data = m.agy_usage()
        self.assertEqual(5, len(data["limits"]))
        self.assertEqual(77, data["limits"][0]["remaining"])
        self.assertEqual(1, data["limits"][2]["remaining"])
        call.assert_called_once_with(
            ["-p", "/usage", "--output-format", "json", "--print-timeout", "60s"], 40)

    def test_agy_invalid_fields_source_error(self):
        fixtures = json.loads((FIXTURES / "agy-usage.json").read_text())
        bucket = fixtures["command"]["data"]["groups"][0]["buckets"][0]
        for field, value in (("remaining_fraction", None), ("remaining_fraction", float("nan")),
                            ("remaining_fraction", True), ("remaining_fraction", 2),
                            ("reset_time", "invalid"), ("window", [])):
            raw = json.loads(json.dumps(fixtures))
            raw["command"]["data"]["groups"][0]["buckets"][0][field] = value
            with self.subTest(field=field, value=value), patch.object(
                    m, "run_agy", return_value=json.dumps(raw)):
                self.assertIn("error", m.safe(m.agy_usage))
        self.assertIn("remaining_fraction", bucket)

    def test_agy_process_and_timeout(self):
        process = Mock(returncode=0, pid=43210)
        process.communicate.return_value = ("response", "")
        self.popen.side_effect = None
        self.popen.return_value = process
        self.assertEqual("response", m.run_agy(["models"], 30))
        kw = self.popen.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(str(m.HOME), kw["cwd"])
        self.assertEqual(subprocess.DEVNULL, kw["stdin"])
        process.returncode = 1
        with self.assertRaises(RuntimeError):
            m.run_agy(["models"], 1)
        process.communicate.side_effect = [subprocess.TimeoutExpired("agy", 1), ("", "")]
        with patch.object(m.os, "killpg") as kill:
            with self.assertRaises(TimeoutError):
                m.run_agy(["models"], 1)
            kill.assert_called_once_with(43210, signal.SIGKILL)
        process.communicate.side_effect = subprocess.TimeoutExpired("agy", 1)
        with patch.object(m.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(TimeoutError):
                m.run_agy(["models"], 1)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()

    def write_codex(self, name, lines, age=0):
        path = m.HOME / ".codex/sessions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(lines)
        os.utime(path, (self.now - age, self.now - age))
        return path

    def codex_line(self, **kw):
        window = dict(used_percent=25, resets_at=self.now + 3600, window_minutes=10080)
        window.update(kw)
        return json.dumps({"payload": {"rate_limits": {"primary": window,
                                                      "secondary": {"used_percent": None,
                                                                    "resets_at": None}}}})

    def test_codex_fixture(self):
        self.write_codex("a.jsonl", (FIXTURES / "codex-line.json").read_text())
        data = m.codex_usage()
        self.assertEqual(50, data["limits"][0]["remaining"])
        self.assertEqual(m.iso_epoch("2026-11-07T13:05:49.818Z"), data["data_time"])

    def test_codex_large_file_null_invalid_line(self):
        path = self.write_codex("a.jsonl", "é" * 600000 + "\n" + self.codex_line() +
                             '\n{invalid "rate_limits"\n{"rate_limits":{}}\n', age=100)
        with patch.object(m, "tail", wraps=m.tail) as read:
            data = m.codex_usage()
            read.assert_called_once_with(path)
        self.assertEqual(75, data["limits"][0]["remaining"])
        self.assertEqual(1, len(data["limits"]))
        self.assertEqual(self.now - 100, data["data_time"])
        self.assertLess(sum(map(len, m.tail(path))), 512 * 1024)

    def test_codex_file_error_and_most_new_record(self):
        self.write_codex("a.jsonl", self.codex_line(), age=50)
        newer = json.loads(self.codex_line(used_percent=10))
        newer["timestamp"] = m.datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        self.write_codex("b.jsonl", json.dumps(newer), age=100)
        invalid = self.write_codex("c.jsonl", "", age=0)
        invalid.write_bytes(b'\xff"rate_limits"')
        self.assertEqual(90, m.codex_usage()["limits"][0]["remaining"])
        with patch.object(m, "tail", side_effect=OSError):
            self.assertIn("error", m.safe(m.codex_usage))

    def test_codex_five_file_and_duration_budget(self):
        for i in range(8):
            self.write_codex(str(i) + ".jsonl", '"rate_limits"', age=i)
        with patch.object(m, "tail", wraps=m.tail) as read:
            self.assertIn("error", m.safe(m.codex_usage))
            self.assertEqual(5, read.call_count)
        with patch.object(m.time, "monotonic", side_effect=[0, 2, 2]):
            self.assertIn("error", m.safe(m.codex_usage))
        for minutes, name in ((300, "5-hour"), (17, "17 minutes")):
            self.write_codex("0.jsonl", self.codex_line(window_minutes=minutes))
            self.assertEqual(name, m.codex_usage()["limits"][0]["window"])

    def test_claude_validation_existing_record_preserves(self):
        self.write_json(m.CLAUDE_CACHE, {"original": 1})
        fixtures = dict(label="Weekly", percentUsed=3, resetsAt="2027-01-01T00:00:00Z")
        windows = [[], None, [None], [dict(fixtures, label="")],
                      [dict(fixtures, percentUsed=None)], [dict(fixtures, percentUsed=True)],
                      [dict(fixtures, percentUsed=101)], [dict(fixtures, resetsAt="invalid")],
                      [dict(fixtures, resetsAt=None)]]
        for items in windows:
            with self.subTest(items=items):
                with patch.object(m.sys, "stdin", io.StringIO(json.dumps({"windows": items}))):
                    self.assertEqual(1, self.cli("--claude-save")[0])
                self.assertEqual({"original": 1}, m.read_json(m.CLAUDE_CACHE))
        with patch.object(m.sys, "stdin", io.StringIO("{}")):
            self.assertEqual((1, "", ""), self.cli("--claude-save", "--quiet"))
        for wrapped in (False, True):
            data = {"windows": [dict(fixtures, resetsAt=None), fixtures]}
            m.claude_save(json.dumps({"plan": data} if wrapped else data))
            self.assertEqual(1, len(m.read_json(m.CLAUDE_CACHE)["windows"]))
            self.assertEqual(97, m.claude_usage()["limits"][0]["remaining"])

    def test_claude_missing_and_invalid(self):
        self.assertEqual({}, m.claude_usage())
        for data in ({}, {"saved_at": None}, {"saved_at": self.now, "windows": []}):
            self.write_json(m.CLAUDE_CACHE, data)
            self.assertIn("error", m.safe(m.claude_usage))

    def test_models_fixtures_and_empty_source(self):
        with patch.object(m, "run_agy", return_value=(FIXTURES / "agy-models.txt").read_text()):
            self.assertEqual(13, len(m.agy_models()))
        for raw in ("", "Fetching available models...", "info"):
            with patch.object(m, "run_agy", return_value=raw):
                with self.assertRaises(ValueError):
                    m.agy_models()
        (m.codex_home() / "sessions").mkdir(parents=True)
        with self.assertRaises(ValueError):
            m.codex_models()
        self.write_json(m.HOME / ".codex/models_cache.json", {"models": [{"slug": "x"}]})
        self.write_json(m.HOME / ".codex/models_cache.codex_work_desktop.json",
                 {"models": [{"slug": "y", "display_name": "Y"}]})
        self.assertEqual({"codex:x": "x", "codex:y": "Y"}, m.codex_models())
        self.patch_value("agy_models", Mock(side_effect=ValueError))
        result = m.models()
        self.assertEqual(["codex"], result["successful"])
        self.assertIn("agy", result["errors"])

    def test_model_baseline_new_removed_and_partial_accept_models(self):
        data = self.models("codex", old="Old")
        self.assertEqual({"codex": 1}, m.model_status(data)["baseline_created"])
        self.assertIn("agy baseline created (1 model)",
                      m.render_text({"models_status": m.model_status(self.models(x="X"))}))
        data = self.models("codex", new="New")
        status = m.model_status(data)
        self.assertEqual({"codex:new": "New"}, status["new"])
        self.assertEqual({"codex:old": "Old"}, status["removed"])
        self.assertIn("Removed model", m.render_text({"models_status": status}))
        m.model_status(data, True)
        self.assertEqual({"agy:x": "X", "codex:new": "New"},
                         m.read_json(m.MODELS_FILE)["models"])

    def test_invalid_model_baseline_preserved(self):
        for raw in ("{", "[]", '{"models": null}', '{"models": {"agy:x": null}}'):
            m.MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
            m.MODELS_FILE.write_text(raw)
            self.assertIn("error", m.safe(m.model_status, self.models(x="X")))
            self.assertEqual(raw, m.MODELS_FILE.read_text())
            self.assertIn("error", m.safe(m.model_status, self.models(x="X"), True))

    def test_accept_models_failed_and_successful(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:old": "Old", "codex:x": "X"}})
        before = m.MODELS_FILE.read_bytes()
        self.patch_value("models", Mock(return_value={"successful": [], "models": {},
                                                  "errors": {"agy": "error"}}))
        self.assertEqual(1, self.cli("--accept-models")[0])
        self.assertEqual((1, "", ""), self.cli("--accept-models", "--quiet"))
        self.assertEqual(before, m.MODELS_FILE.read_bytes())
        data = self.data(self.limit(), models_status={"new": {"agy:new": "New"}})
        self.write_json(m.CACHE, data)
        m.models.return_value = self.models(new="New")
        code, output, _ = self.cli("--accept-models")
        self.assertEqual(0, code)
        self.assertIn("Compared sources: agy", output)
        self.assertIn("Added: New", output)
        self.assertIn("Removed: Old", output)
        self.assertFalse(m.warnings(m.read_cache())[1:])
        self.assertNotIn("NEW MODEL", str(m.warnings(m.read_cache())))
        self.assertIn("codex:x", m.read_json(m.MODELS_FILE)["models"])
        self.assertEqual(1, len(m.read_cache()["codex"]["limits"]))

    def test_lock_ownership_staleness_and_separate_file_descriptor(self):
        self.assertTrue(m.acquire_lock())
        self.assertFalse(m.acquire_lock())
        # Another call cannot acquire the lock after forgetting modulee ownership.
        owner = m._lock
        m._lock = None
        try:
            self.assertFalse(m.acquire_lock())
        finally:
            m._lock = owner
        m.LOCK.write_text("other")
        m.release_lock()
        self.assertTrue(m.LOCK.exists())
        os.utime(m.LOCK, (self.now - 181, self.now - 181))
        self.assertTrue(m.acquire_lock())
        m.release_lock()
        self.assertFalse(m.LOCK.exists())
        self.assertIsNone(m._lock)

    def test_lock_active_stale_file_and_inode(self):
        self.assertTrue(m.acquire_lock())
        os.utime(m.LOCK, (self.now - 1000, self.now - 1000))
        owner = m._lock
        m._lock = None
        try:
            self.assertFalse(m.acquire_lock())
        finally:
            m._lock = owner
        m.LOCK.unlink()
        m.LOCK.write_text(str(os.getpid()))
        m.release_lock()
        self.assertTrue(m.LOCK.exists())

    def test_collection_last_good_data_and_lock_finally(self):
        old = {"limits": [self.limit(source="Antigravity", name="Gemini")],
                "data_time": self.now - 100}
        self.write_json(m.CACHE, self.data(self.limit(), agy=old))
        self.mock_collection(codex={"limits": [self.limit()]})
        result = m.collect()
        self.assertEqual(old["limits"], result["agy"]["limits"])
        self.assertEqual("missing", result["agy"]["last_error"])
        self.assertNotIn("warnings", m.read_json(m.CACHE))
        self.assertFalse(m.LOCK.exists())
        with patch.object(m, "atomic_json", side_effect=OSError):
            with self.assertRaises(OSError):
                m.collect()
        self.assertFalse(m.LOCK.exists())

    def test_refresh_locked_source_uncalled(self):
        self.mock_collection()
        self.assertTrue(m.acquire_lock())
        self.assertEqual("refresh already in progress", m.collect()["note"])
        m.agy_usage.assert_not_called()
        self.assertIn("refresh already in progress", self.cli("--refresh")[1])
        self.assertEqual((0, "", ""), self.cli("--refresh", "--quiet"))

    def test_cache_invalid_fields_source_per_isolated(self):
        for raw in ("{", "[]", "{}", '{"timestamp":null,"version":2}',
                    '{"timestamp":100,"version":1}'):
            m.CACHE.parent.mkdir(parents=True, exist_ok=True)
            m.CACHE.write_text(raw)
            self.assertIsNone(m.read_cache())
        for invalid in (None, [], {"limits": [None]}, {"limits": "x"},
                      {"data_time": None}, {"note": []}):
            self.write_json(m.CACHE, self.data(self.limit(), agy=invalid))
            data = m.read_cache()
            self.assertIn("error", data["agy"])
            self.assertEqual(1, len(data["codex"]["limits"]))
        self.write_json(m.CACHE, self.data(self.limit(), models_status={"new": []},
                                  model_sources=[]))
        data = m.read_cache()
        self.assertIn("error", data["models_status"])
        self.assertEqual(1, len(data["codex"]["limits"]))
        self.assertIn("Codex", m.menubar(data))

    def test_cache_freshness_reset_and_future(self):
        self.assertTrue(m.cache_fresh(self.data()))
        for data in (None, self.data(timestamp=self.now - 1800),
                     self.data(timestamp=self.now + 1),
                     self.data(agy={"limits": [self.limit(reset=self.now)]})):
            self.assertFalse(m.cache_fresh(data))

    def test_fast_modes_never_agy_waits(self):
        self.mock_collection(codex={"limits": [self.limit()]})
        self.patch_value("refresh_background", Mock())
        started = time.perf_counter()
        for mode in ("--short", "--menubar"):
            for suffix in ([], ["--refresh"], ["--accept-models"], ["--claude-save"]):
                self.assertEqual(0, self.cli(mode, *suffix)[0])
        self.assertLess(time.perf_counter() - started, 2)
        m.agy_usage.assert_not_called()
        m.models.assert_not_called()
        self.write_json(m.CACHE, self.data(self.limit()))
        m.refresh_background.reset_mock()
        self.cli("--short")
        m.refresh_background.assert_not_called()
        self.write_json(m.CACHE, self.data(timestamp=self.now - 1801))
        self.cli("--menubar")
        m.refresh_background.assert_called_once()

    def test_background_command_and_quiet_error(self):
        self.popen.side_effect = None
        m.refresh_background()
        self.assertEqual([m.sys.executable, str(m.ROOT / "tidewatch.py"), "--refresh",
                          "--quiet"], self.popen.call_args.args[0])
        kw = self.popen.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(subprocess.DEVNULL, kw[name])
        self.mock_collection()
        self.popen.side_effect = OSError
        self.assertEqual(0, self.cli("--short")[0])

    def test_normal_json_fresh_and_refresh(self):
        self.mock_collection(codex={"limits": [self.limit()]})
        self.write_json(m.CACHE, self.data(self.limit()))
        self.assertEqual(0, self.cli("--json")[0])
        m.agy_usage.assert_not_called()
        code, output, _ = self.cli("--json", "--refresh")
        self.assertEqual(0, code)
        raw = json.loads(output)
        self.assertIn("warnings", raw)
        self.assertIn("reset_text", raw["codex"]["limits"][0])
        self.assertNotIn("warnings", m.read_json(m.CACHE))
        m.agy_usage.assert_called_once()
        self.assertIn("cache", self.cli()[1])

    def test_table_durations_boundaries(self):
        for duration, render_text in ((59 * 60, "59m"), (3600, "1h 0m"),
                            (86340, "23h 59m"), (86400, "1d 0h")):
            self.assertEqual(render_text, m.format_duration(duration))
        self.assertEqual("0m", m.format_duration(-1))

    def test_menubar_table_alignment_names_colors(self):
        claude = [self.limit(source="Claude", name=name, reset=self.now + duration,
                             data_time=self.now - age)
                  for name, duration, age in (("5-hour limit", 3540, 0),
                                        ("Weekly · all models", 3600, 0),
                                        ("Weekly · Sonnet", 86340, 25200),
                                        ("Weekly · Opus", -1, 0))]
        agy = [self.limit(source="Antigravity", name=name, reset=self.now + 172800)
               for name in ("Gemini Models", "Claude and GPT models", "Gemini 4 Argon", "Other")]
        data = self.data(self.limit(remaining=9, reset=self.now + 86400),
                         claude={"limits": claude, "saved_at": self.now,
                                 "data_time": self.now - 7200},
                         agy={"limits": agy, "data_time": self.now - 180},
                         codex={"limits": [self.limit(remaining=9, reset=self.now + 86400)],
                                "data_time": self.now - 720})
        lines = m.menubar(data).splitlines()
        table = lines[2:12]
        title = table[0].split(" | ")[0]
        duration_start, format_date_start = title.index("Time left"), title.index("Resets")
        percent_last = title.index("Left") + len("Left")
        self.assertEqual("AI | color=red", lines[0])
        self.assertIn("color=gray", table[0])
        durations = ["Time left", "59m", "1h 0m", "23h 59m (stale record)",
                   "no fresh data", "1d 0h"] + ["2d 0h"] * 4
        for line, duration in zip(table, durations):
            self.assertIn(" | font=Menlo size=12 trim=false", line)
            render_text = line.split(" | ")[0]
            self.assertEqual(duration, render_text[duration_start:format_date_start].rstrip())
            self.assertEqual(render_text[format_date_start:],
                             render_text[format_date_start:].rstrip())
        for line in table[1:]:
            self.assertTrue(line[:percent_last].endswith(("40%", "9%", "-")))
        for name in ("5-hour", "Weekly (Sonnet)", "Weekly (Opus)", "Gemini Weekly",
                   "Claude/GPT Weekly", "Argon Weekly", "Other Weekly"):
            self.assertIn(name, "\n".join(table))
        self.assertIn("color=gray", table[3])
        self.assertIn("color=gray", table[4])
        self.assertNotIn("color=", table[5])
        measurement = next(s for s in lines if s.startswith("Measured:"))
        self.assertIn("Claude 2h 0m ago · Codex 12m ago · Antigravity 3m ago", measurement)
        self.assertNotIn("measured", "\n".join(table))
        self.assertIn("param2=--refresh param3=--quiet", lines[-1])

    def test_menubar_reminder_title_excluded_and_expired(self):
        x = self.limit(source="Claude", name="Weekly Sonnet", remaining=1,
                       data_time=self.now - 25200, reset=self.now + 172800)
        data = self.data(self.limit(reset=self.now + 7 * 86400),
                         claude={"saved_at": self.now - 25200, "limits": [x]},
                         agy={"error": "interrupted"})
        output = m.menubar(data)
        self.assertEqual("AI", output.splitlines()[0])
        self.assertIn("agy: interrupted | color=gray", output)
        self.assertIn("CLAUDE DATA STALE", output)
        self.assertIn("Weekly (Sonnet)", output)
        self.assertTrue(next(s for s in output.splitlines() if s.startswith(
            "CLAUDE DATA STALE")).endswith(" | color=gray"))
        expired = self.data(self.limit(reset=self.now - 1, remaining=4))
        self.assertIn("reset, no fresh data", m.render_text(expired))
        self.assertNotIn("(0m)", m.render_text(expired))
        self.assertEqual("AI", m.menubar(expired).splitlines()[0])
        self.assertTrue(m.json_data(expired)["codex"]["limits"][0]["expired"])

    def test_atomic_write_error_old_record_preserves(self):
        self.write_json(m.CACHE, {"original": 1})
        with patch.object(m.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.write_json(m.CACHE, {"new": 2})
        self.assertEqual({"original": 1}, m.read_json(m.CACHE))
        self.assertEqual([m.CACHE], list(m.DATA.iterdir()))

    def test_deep_invalid_cache_fast_local_data_unblocked(self):
        m.CACHE.parent.mkdir(parents=True, exist_ok=True)
        m.CACHE.write_text("[" * 2000 + "]" * 2000)
        self.assertIsNone(m.read_cache())
        self.mock_collection(codex={"limits": [self.limit(remaining=1)]})
        self.patch_value("refresh_background", Mock())
        # Fast modes do not read Codex sessions; an invalid cache only asks for a refresh.
        self.assertEqual((0, ""), self.cli("--short")[::2])
        m.refresh_background.assert_called_once()
        m.codex_usage.assert_not_called()
        m.agy_usage.assert_not_called()

    def test_codex_expired_percent_and_quiet_error(self):
        self.write_codex("a.jsonl", self.codex_line(resets_at=self.now - 1))
        result = m.codex_usage()
        self.assertEqual(100, result["limits"][0]["remaining"])
        self.assertFalse(m.warnings({"codex": result}))
        self.patch_value("prepare", Mock(side_effect=OSError))
        self.assertEqual((0, "", ""), self.cli("--quiet"))
        self.assertEqual(1, self.cli("--json")[0])

    def test_agy_path_fallback_and_found_path(self):
        for found, available, expected in (
                ("/test/bin/agy", (), "/test/bin/agy"),
                (None, ("/opt/homebrew/bin/agy",), "/opt/homebrew/bin/agy"),
                (None, ("/usr/local/bin/agy",), "/usr/local/bin/agy"),
                (None, (), None)):
            with patch.object(m.shutil, "which", return_value=found) as lookup, \
                    patch.object(m.os.path, "isfile", side_effect=lambda p: p in available), \
                    patch.object(m.os, "access", return_value=True):
                module = runpy.run_path(m.__file__, run_name="tidewatch_path_test")
            lookup.assert_called_once_with("agy")
            self.assertEqual(expected, module["AGY"])
        self.patch_value("AGY", "/opt/homebrew/bin/agy")
        self.popen.side_effect = None
        self.popen.return_value = Mock(returncode=0)
        self.popen.return_value.communicate.return_value = ("response", "")
        m.run_agy(["models"], 30)
        self.assertEqual(["/opt/homebrew/bin/agy", "models"], self.popen.call_args.args[0])

    def test_refresh_line_plain_paths(self):
        line = m.menubar({}).splitlines()[-1]
        self.assertEqual(
            f'Refresh | bash="{m.sys.executable}" param1="{m.ROOT / "tidewatch.py"}" '
            "param2=--refresh param3=--quiet terminal=false refresh=true", line)

    def test_refresh_line_spaces_paths(self):
        self.patch_value("ROOT", self.root / "project with spaces")
        with patch.object(m.sys, "executable", "/test/Python App/python3"):
            self.assertEqual(
                f'Refresh | bash="/test/Python App/python3" '
                f'param1="{m.ROOT / "tidewatch.py"}" param2=--refresh '
                'param3=--quiet terminal=false refresh=true',
                m.menubar({}).splitlines()[-1])

    def test_claude_weekly_prefix_added_label(self):
        self.assertEqual("Weekly (Opus)", m.table_name(
            self.limit(source="Claude", name="Claude Weekly · Opus")))

    def test_cli_errors_explained(self):
        with patch.object(m.sys, "stdin", io.StringIO('{"windows": []}')):
            code, output, error = self.cli("--claude-save")
        self.assertEqual((1, ""), (code, output))
        self.assertIn("Could not save Claude usage: Claude windows are empty or invalid", error)
        self.assertNotIn("ValueError", error)
        self.assertTrue(m.acquire_lock())
        code, output, error = self.cli("--accept-models")
        self.assertEqual((1, ""), (code, output))
        self.assertIn("refresh already in progress", error)
        self.assertNotIn("ValueError", error)
        self.assertEqual((1, "", ""), self.cli("--accept-models", "--quiet"))

    def test_normal_mode_locked_cache_age(self):
        self.mock_collection()
        self.write_json(m.CACHE, self.data(timestamp=self.now - 3600))
        self.assertTrue(m.acquire_lock())
        for args in ([], ["--refresh"]):
            code, output, error = self.cli(*args)
            self.assertEqual((0, ""), (code, error))
            self.assertIn("Data: cache, 1h 0m ago", output)
            self.assertIn("refresh already in progress", output)
            self.assertNotIn("Data: now", output)
        m.agy_usage.assert_not_called()

    def test_codex_partial_utf8_last_line_previous_record_preserves(self):
        path = self.write_codex("a.jsonl", self.codex_line() + "\n")
        with path.open("ab") as file:
            file.write(b'{"rate_limits": "\xc4')
        self.assertIn("\ufffd", m.tail(path)[-1])
        self.assertEqual(75, m.codex_usage()["limits"][0]["remaining"])

    def test_new_model_measurement_age(self):
        for age in (0, 7200, 7201, 10800):
            data = self.data(timestamp=self.now - age,
                             models_status={"new": {"agy:x": "X"}})
            warning = m.warnings(data)[0]
            self.assertEqual(age > 7200, " (measured " in warning)
            if age > 7200:
                self.assertTrue(warning.endswith(f" (measured {m.format_duration(age)} ago)"))

    def test_render_text_warning_title_and_new_model(self):
        self.assertIn("\nWarnings\nNone\n", m.render_text({}) + "\n")
        data = self.data(models_status={"new": {"agy:x": "X"}})
        output = m.render_text(data)
        self.assertIn("\nNew: X (agy:x)\n", output)
        self.assertIn("\nWarnings\nNEW MODEL: X", output)

    def test_menu_title_warning_by_colored(self):
        for data, title in (({}, "AI"),
                             (self.data(self.limit(reset=self.now + 7 * 86400)), "AI"),
                             (self.data(self.limit(remaining=1)), "AI"),
                             (self.data(self.limit()), "AI | color=red"),
                             (self.data(self.limit(reset=self.now + 3 * 86400)),
                              "AI | color=orange"),
                             ({"models_status": {"new": {"agy:x": "X"}}},
                              "AI | color=#0A84FF")):
            with self.subTest(title=title, data=data):
                self.assertEqual(title, m.menubar(data).splitlines()[0])
        self.patch_value("prepare", Mock(side_effect=ValueError("invalid")))
        self.assertEqual("AI", self.cli("--menubar")[1].splitlines()[0])

    def test_menu_warning_colors_and_priority(self):
        low = self.limit(remaining=9)
        red = self.limit(reset=self.now + 11 * 3600)
        orange = self.limit(reset=self.now + 3 * 86400)
        new = {"models_status": {"new": {"agy:x": "X"}}}
        for limits, color in (((low,), "#0A84FF"),
                               ((low, orange), "orange"),
                               ((orange, low, red), "red"),
                               ((red, low, orange), "red")):
            with self.subTest(limits=limits):
                data = self.data(*limits, **new)
                lines = m.menubar(data).splitlines()
                self.assertEqual(f"AI | color={color}", lines[0])
                for warning in m.warnings(data):
                    if warning.startswith("LOW:"):
                        self.assertIn(warning, lines)
                    elif warning.startswith("AT RISK:"):
                        self.assertIn(warning + " | color=red", lines)
                    elif warning.startswith("OFF PACE:"):
                        self.assertIn(warning + " | color=orange", lines)
                    else:
                        self.assertIn(warning + " | color=#0A84FF", lines)

    def test_absent_agy_usage_and_models_are_silent(self):
        self.patch_value("AGY", None)
        self.assertEqual({}, m.agy_usage())
        self.assertEqual({}, m.agy_models())
        data = m.collect()
        self.assertEqual([], m.source_notes(data))
        self.assertEqual([], data["model_sources"]["successful"])
        self.assertEqual({}, data["model_sources"]["errors"])
        self.assertNotIn("Antigravity", m.render_text(data))
        self.popen.assert_not_called()

    def test_absent_codex_sessions_are_silent(self):
        self.assertFalse((m.codex_home() / "sessions").exists())
        self.assertEqual({}, m.codex_usage())
        self.assertEqual({}, m.codex_models())
        self.assertEqual([], m.source_notes({"codex": m.codex_usage()}))
        self.assertNotIn("Codex:", m.render_text({"codex": m.codex_usage()}))

    def test_unsaved_claude_is_silent_and_saved_stale_record_has_hint(self):
        data = {"claude": m.claude_usage()}
        self.assertEqual({}, data["claude"])
        self.assertEqual([], m.source_notes(data))
        self.assertEqual("", m.claude_stale_hint(data))
        self.assertNotIn("Claude", m.render_text(data))
        self.assertNotIn("CLAUDE", m.menubar(data))
        m.claude_save(json.dumps({"windows": [
            {"label": "Weekly", "percentUsed": 42, "resetsAt": "2027-01-01T00:00:00Z"}]}))
        record = m.read_json(m.CLAUDE_CACHE)
        record["saved_at"] = self.now - 25200
        self.write_json(m.CLAUDE_CACHE, record)
        data["claude"] = m.claude_usage()
        self.assertEqual(
            "CLAUDE DATA STALE: last saved 7h 0m ago; "
            "save fresh usage with tidewatch.py --claude-save", m.claude_stale_hint(data))

    def test_codex_home_override_and_default(self):
        custom = self.root / "custom codex"
        with patch.dict(os.environ, {"CODEX_HOME": str(custom)}):
            self.assertEqual(custom, m.codex_home())
            self.write_json(custom / "models_cache.json", {"models": [{"slug": "custom"}]})
            sessions = custom / "sessions"
            sessions.mkdir()
            (sessions / "a.jsonl").write_text(self.codex_line())
            self.assertEqual(75, m.codex_usage()["limits"][0]["remaining"])
            self.assertEqual({"codex:custom": "custom"}, m.codex_models())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(self.root / ".codex", m.codex_home())

    def test_one_or_two_sources_can_be_used(self):
        self.patch_value("AGY", None)
        self.write_codex("a.jsonl", self.codex_line())
        self.write_json(m.codex_home() / "models_cache.json", {"models": [{"slug": "x"}]})
        data = m.collect()
        self.assertEqual(["Codex"], [x["source"] for x in m.all_limits(data)])
        self.assertEqual([], m.source_notes(data))
        m.claude_save(json.dumps({"plan": {"windows": [
            {"label": "Weekly", "percentUsed": 42, "resetsAt": "2027-01-01T00:00:00Z"}]}}))
        data = m.collect()
        self.assertEqual(["Claude", "Codex"], [x["source"] for x in m.all_limits(data)])
        self.assertEqual([], m.source_notes(data))

    def test_local_date_format_with_fixed_english_months(self):
        self.patch_value("TZ", None)
        epoch = m.iso_epoch("2026-11-14T04:43:20Z")
        try:
            for zone, expected in (("UTC0", "Nov 14 04:43"), ("EST5", "Nov 13 23:43")):
                with patch.dict(os.environ, {"TZ": zone}):
                    time.tzset()
                    self.assertEqual(expected, m.format_date(epoch))
            with patch.dict(os.environ, {"TZ": "UTC0"}):
                time.tzset()
                for month, name in enumerate(
                        ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                         "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1):
                    epoch = m.datetime(2026, month, 3, 4, 5, tzinfo=timezone.utc).timestamp()
                    self.assertEqual(f"{name} 03 04:05", m.format_date(epoch))
            local = Mock(month=3, day=3, hour=4, minute=5)
            local.strftime.side_effect = AssertionError("Locale-dependent formatting")
            with patch.object(m, "datetime") as dates:
                dates.fromtimestamp.return_value = local
                self.assertEqual("Mar 03 04:05", m.format_date(epoch))
                dates.fromtimestamp.assert_called_once_with(epoch)
                local.strftime.assert_not_called()
        finally:
            time.tzset()

    def test_all_agy_call_sites_only_request_usage_models_or_fixed_hello(self):
        tree = ast.parse(Path(m.__file__).read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "run_agy"]
        self.assertEqual(3, len(calls))
        commands = []
        for call in calls:
            command = call.args[0]
            if any(isinstance(node, ast.Name) for node in ast.walk(command)):
                self.assertEqual("send_hello", next(
                    node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                    and call in ast.walk(node)))
                self.assertEqual("HELLO_PROMPT", command.elts[1].id)
                self.assertEqual("model", command.elts[3].id)
                command.elts[1] = ast.Constant(value="hello")
                command.elts[3] = ast.Constant(value="test-model")
            commands.append(ast.literal_eval(command))
        self.assertIn(["models"], commands)
        self.assertIn(["-p", "/usage", "--output-format", "json",
                       "--print-timeout", "60s"], commands)
        self.assertIn(["-p", "hello", "--model", "test-model", "--output-format", "json",
                       "--print-timeout", "60s"], commands)

    def test_agy_rejects_arbitrary_or_incomplete_prompt_commands_before_spawning(self):
        for args in (["-p", "hello"], ["-p", "/usage", "--model", "x"], [],
                     ["chat"], ["models", "--other"],
                     ["-p", "another prompt", "--model", "x", "--output-format", "json",
                      "--print-timeout", "60s"],
                     ["-p", "hello", "--model", "x", "--output-format", "json",
                      "--print-timeout", "60s", "--other"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                m.run_agy(args, 1)
        self.popen.assert_not_called()

    def test_cli_help_describes_each_flag(self):
        with self.assertRaises(SystemExit) as exit_code:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                m.main(["--help"])
        self.assertEqual(0, exit_code.exception.code)
        for flag, description in (
                ("--json", "Print structured JSON"), ("--short", "Print up to three warnings"),
                ("--menubar", "Print the SwiftBar menu"), ("--refresh", "Refresh cached usage"),
                ("--quiet", "Suppress output"), ("--accept-models", "Accept current model lists"),
                ("--claude-save", "Save Claude usage JSON")):
            self.assertIn(flag, output.getvalue())
            self.assertIn(description, output.getvalue())
        self.popen.assert_not_called()

    def hello_pair(self):
        x = self.limit(source="Antigravity", name="Gemini Models", remaining=100,
                       reset=self.now + 7 * 86400)
        return x, dict(x, reset=x["reset"] - 600, data_time=self.now - 600)

    def enable_hello(self):
        self.write_json(m.SETTINGS, {"auto_hello": True})

    def test_weekly_not_started_detection(self):
        self.enable_hello()
        x, old = self.hello_pair()
        data = self.models(**{"flash-low": "X"})
        cases = [
            ("sliding", old, True),
            ("fixed", dict(old, reset=x["reset"]), False),
            ("fixed short interval", dict(old, reset=x["reset"],
                                          data_time=self.now - 60), False),
            ("first reset", dict(old, reset=x["reset"] - 7 * 86400), False),
            ("previous 99", dict(old, remaining=99), False),
            ("another group", dict(old, name="Gemini 4 Argon"), False),
            ("short interval", dict(old, data_time=self.now - 59), False),
            ("tolerance boundary", dict(old, reset=x["reset"] - 900), False),
            ("exactly 60 seconds", dict(old, reset=x["reset"] - 60,
                                        data_time=self.now - 60), True),
            ("old schema", {"name": "Gemini Models", "remaining": 100}, False),
            ("invalid", None, False),
        ]
        for name, previous, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, m.weekly_not_started(x, previous))
        for change in ({"remaining": 99}, {"window": "5-hour"}, {"source": "Codex"}):
            self.assertFalse(m.weekly_not_started(dict(x, **change), old))
        with patch.object(m, "run_agy", return_value="{}") as call:
            for _, previous, expected in cases:
                if not expected:
                    m.send_hello([x], data, [previous])
            m.send_hello([dict(x, remaining=99)], data, [old])
            m.send_hello([x], data, [])
            call.assert_not_called()
            m.send_hello([x], data, [old])
            call.assert_called_once()

    def test_hello_model_selection_and_fallbacks(self):
        data = self.models(**{"gemini-4-hydrogen-low": "H", "gemini-3.8-flash-low": "X",
                              "other-flash-low": "Y", "argon-high": "Z", "argon-low": "A",
                              "sonnet-low": "S", "gpt-oss-high": "G"})
        self.assertEqual("gemini-4-hydrogen-low", m.hello_model("Gemini Models", data))
        del data["models"]["agy:gemini-4-hydrogen-low"]
        data["models"]["agy:gemini-4-hydrogen-high"] = "HH"
        self.assertEqual("gemini-4-hydrogen-high", m.hello_model("Gemini Models", data))
        del data["models"]["agy:gemini-4-hydrogen-high"]
        self.assertEqual("gemini-3.8-flash-low", m.hello_model("Gemini Models", data))
        self.assertEqual("argon-low", m.hello_model("Gemini 4 Argon", data))
        self.assertEqual("gpt-oss-high", m.hello_model("Claude and GPT models", data))
        del data["models"]["agy:gpt-oss-high"]
        self.assertEqual("sonnet-low", m.hello_model("Claude and GPT models", data))
        del data["models"]["agy:gemini-3.8-flash-low"]
        self.assertEqual("other-flash-low", m.hello_model("Gemini Models", data))
        self.assertIsNone(m.hello_model("Unknown", data))
        for group, model in (("Gemini 4 Argon", "gemini-4-argon-low"),
                             ("Claude and GPT models", "gpt-oss-120b-medium")):
            data["models"]["agy:" + model] = "Preferred"
            self.assertEqual(model, m.hello_model(group, data))
        data["successful"] = []
        self.assertIsNone(m.hello_model("Gemini Models", data))

    def test_hello_cooldown_command_state_and_log(self):
        self.enable_hello()
        x, old = self.hello_pair()
        data = self.models(**{"gemini-4-hydrogen-low": "X"})
        with patch.object(m, "run_agy", return_value="{}") as call:
            m.send_hello([x, x], data, [old])
            m.send_hello([x], data, [old])
            call.assert_called_once_with(
                ["-p", "hello", "--model", "gemini-4-hydrogen-low",
                 "--output-format", "json", "--print-timeout", "60s"], 90)
            self.write_json(m.HELLO_STATE, {"Gemini Models": self.now - 21599})
            m.send_hello([x], data, [old])
            self.assertEqual(1, call.call_count)
            self.write_json(m.HELLO_STATE, {"Gemini Models": self.now - 21600})
            m.send_hello([x], data, [old])
            self.assertEqual(2, call.call_count)
        self.assertEqual({"Gemini Models": self.now}, m.read_json(m.HELLO_STATE))
        self.assertEqual("sent", m.last_hello()["result"])
        self.assertEqual(2, len(m.HELLO_LOG.read_text().splitlines()))
        self.assertIn("\nLast automatic message: Gemini Models " +
                      m.format_date(self.now) + " (gemini-4-hydrogen-low, sent)",
                      m.render_text({}))
        self.assertNotIn("Last automatic message", m.menubar({}))
        self.assertEqual(m.last_hello(), m.json_data({})["last_hello"])
        self.write_json(m.SETTINGS, {"auto_hello": False})
        self.assertIsNone(m.last_hello())
        self.assertIsNone(m.json_data({})["last_hello"])
        self.assertNotIn("Last automatic message", m.render_text({}))

    def test_hello_invalid_state_no_model_and_error(self):
        self.enable_hello()
        x, old = self.hello_pair()
        for state in ([], {"Gemini Models": None}, {"Gemini Models": "invalid"},
                      {"Gemini Models": True}, {"Gemini Models": float("nan")}):
            m.HELLO_STATE.write_text(json.dumps(state))
            with patch.object(m, "run_agy", side_effect=RuntimeError) as call:
                m.send_hello([x], self.models(), [old])
                call.assert_not_called()
                self.assertEqual("no model", m.last_hello()["result"])
                m.send_hello([x], self.models(**{"flash-low": "X"}), [old])
                self.assertEqual("error: RuntimeError", m.last_hello()["result"])
        with m.HELLO_LOG.open("a") as file:
            file.write('\n{\n[]\n{"time":null}\n')
        self.assertEqual("error: RuntimeError", m.last_hello()["result"])

    def test_hello_only_fresh_collection_and_under_lock(self):
        self.enable_hello()
        x, previous = self.hello_pair()
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))

        def check(*_):
            self.assertTrue(m.LOCK.exists())
            self.assertIsNotNone(m._lock)

        with patch.object(m, "send_hello", side_effect=check) as send:
            m.collect()
            send.assert_called_once()
            self.assertEqual([], send.call_args.args[2])
            self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
            m.collect()
            self.assertEqual([previous], send.call_args.args[2])
            m.agy_usage.return_value = {"error": "interrupted"}
            send.reset_mock()
            result = m.collect()
            send.assert_not_called()
            self.assertEqual("interrupted", result["agy"]["last_error"])
            m.agy_usage.return_value = {"limits": [x]}
            m.collect()
            self.assertEqual([], send.call_args.args[2])
            for error in ("error", "last_error"):
                self.write_json(m.CACHE, self.data(agy={"limits": [previous], error: "failed"}))
                m.collect()
                self.assertEqual([], send.call_args.args[2])
            send.reset_mock()
            self.write_json(m.SETTINGS, {"auto_hello": False})
            m.collect()
            send.assert_not_called()
        self.enable_hello()
        with patch.object(m, "send_hello", side_effect=OSError):
            self.assertIn("codex", m.collect())

    def test_auto_hello_default_off_even_when_weekly_not_started(self):
        x, previous = self.hello_pair()
        self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))
        with patch.object(m, "run_agy") as call:
            self.assertFalse(m.auto_hello_enabled())
            m.collect()
            m.send_hello([x], self.models(**{"flash-low": "X"}), [previous])
            call.assert_not_called()
        self.assertFalse(m.HELLO_STATE.exists())
        self.assertFalse(m.HELLO_LOG.exists())
        self.assertIsNone(m.json_data({})["last_hello"])
        self.assertNotIn("Last automatic message", m.render_text({}))

    def test_auto_hello_invalid_settings_are_silently_off(self):
        x, previous = self.hello_pair()
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))
        m.DATA.mkdir(parents=True, exist_ok=True)
        settings = ["{", "[]", "null", "true", "1", '\"true\"', "{}",
                    '{"auto_hello": false}', '{"auto_hello": 1}', '{"auto_hello": 1.0}',
                    '{"auto_hello": "true"}', '{"auto_hello": null}',
                    '{"auto_hello": []}', '{"auto_hello": {}}',
                    "[" * 2000 + "]" * 2000]
        for raw in settings:
            with self.subTest(raw=raw[:60]), patch.object(m, "run_agy") as call:
                m.SETTINGS.write_text(raw)
                self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
                self.assertFalse(m.auto_hello_enabled())
                m.collect()
                call.assert_not_called()
                self.assertFalse(m.HELLO_STATE.exists())
                self.assertFalse(m.HELLO_LOG.exists())

    def test_auto_hello_enabled_collection_sends(self):
        x, previous = self.hello_pair()
        self.mock_collection(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))
        self.write_json(m.CACHE, self.data(agy={"limits": [previous]}))
        self.enable_hello()
        with patch.object(m, "run_agy", return_value="{}") as call:
            m.collect()
            call.assert_called_once()
        self.assertEqual("sent", m.last_hello()["result"])
        self.assertTrue(m.HELLO_STATE.exists())
        self.assertTrue(m.HELLO_LOG.exists())

    def test_agy_accepts_exact_hello_command(self):
        self.popen.side_effect = None
        self.popen.return_value = Mock(returncode=0)
        self.popen.return_value.communicate.return_value = ("{}", "")
        args = ["-p", "hello", "--model", "flash-low", "--output-format", "json",
                "--print-timeout", "60s"]
        self.assertEqual("{}", m.run_agy(args, 90))
        self.assertEqual([m.AGY] + args, self.popen.call_args.args[0])

    def test_line_length(self):
        for path in (Path(m.__file__), Path(__file__)):
            for number, line in enumerate(path.read_text().splitlines(), 1):
                self.assertLessEqual(len(line), 100, f"{path.name}:{number}")


    # Capacity levels, the facts line, history, plans, backlog and settings.

    def use_guide_files(self):
        self.patch_value("BACKLOG_FILE", self.root / "backlog.md")
        self.patch_value("ROSTER_FILE", self.root / "roster.md")

    def epoch(self, clock):
        return m.iso_epoch("2026-11-13T" + clock + "+06:00")

    def history(self):
        return list(m.history_records())

    def test_work_intervals_midnight_overlap_and_zone(self):
        self.patch_value("WORK_HOURS", [("22:00", "02:00"), ("23:00", "01:00"),
                                        ("01:00", "03:00")])
        start, end = self.epoch("00:00:00"), self.epoch("23:30:00")
        self.assertEqual([(start, start + 3 * 3600), (end - 5400, end)],
                         m.work_intervals(start, end))
        self.assertEqual(4.5, m.total_hours(m.work_intervals(start, end)))
        self.assertEqual([], m.work_intervals(end, start))
        self.assertEqual([], m.work_intervals(start, start))
        self.patch_value("WORK_HOURS", [("19:00", "22:30")])
        utc = m.iso_epoch("2026-11-13T13:00:00Z")
        self.assertEqual([(utc, utc + 12600)], m.work_intervals(utc, utc + 20000))

    def test_capacity_normal_fast_and_claude_cap(self):
        start, end = self.epoch("19:00:00"), self.epoch("22:30:00")
        x = self.limit(source="Claude")
        self.assertEqual(10.5, m.capacity(x, start, end, "normal"))
        self.assertEqual(20, m.capacity(x, start, end, "fast"))
        self.assertEqual(60, m.capacity(x, start, end + 86400, "fast"))
        self.assertEqual(70, m.capacity(self.limit(), start, end, "fast"))
        self.patch_value("WORK_HOURS", [("10:00", "22:00")])
        self.assertEqual(60, m.capacity(x, self.epoch("10:00:00"),
                                        self.epoch("22:00:00"), "fast"))

    def test_levels_boundaries_horizon_low_and_missed_target(self):
        self.patch_value("NIGHT_HOURS", [])
        self.patch_value("FAST_RATE", dict(m.FAST_RATE, Codex=8))
        now = self.epoch("19:00:00")
        x = self.limit(reset=self.epoch("22:30:00") + 86400, data_time=now)
        for remaining, expected in ((10, ""), (10.5, ""), (11, "pace"),
                                    (28, "pace"), (29, "rescue"), (9, "")):
            self.assertEqual(expected, m.level(dict(x, remaining=remaining), now))
        target, _ = m.target_and_last(x)
        self.assertEqual("rescue", m.level(x, target))
        message = m.warnings(self.data(x), target)[0]
        for part in ("target", "missed", "evening 3.5 h + night 0.0 h", "fast ~28.0%"):
            self.assertIn(part, message)
        self.assertEqual("", m.level(dict(x, reset=now), now))
        self.assertEqual("", m.level(dict(x, window="5-hour"), now))
        self.assertEqual("", m.level(dict(x, reset=now + 6 * 86400 + 1), now))
        self.patch_value("FAST_RATE", {"Codex": 0})
        self.assertEqual("rescue", m.level(dict(x, reset=now + 6 * 86400), now))

    def test_night_start_boundary_and_started_night(self):
        night, last = self.epoch("22:30:00"), self.epoch("04:00:00") + 86400
        x = self.limit()
        for difference in (-1, 0, 1):
            start = night + difference
            normal = m.capacity(x, start, last, "normal")
            self.assertAlmostEqual(5.5 + 3 / 3600 if difference == -1 else 0, normal)
            self.assertAlmostEqual((last - start) / 3600 * 20, m.capacity(x, start, last, "fast"))
            self.assertEqual(difference == -1, bool(m.work_intervals(start, last, night=True)))
        self.assertEqual([], m.night_intervals(last, last, "fast"))

    def test_claude_evening_and_night_share_one_cap(self):
        start, last = self.epoch("19:00:00"), self.epoch("04:00:00") + 86400
        x = self.limit(source="Claude")
        self.assertEqual(16, m.capacity(x, start, last, "normal"))
        # Capping the evening and the night separately would give 20 + 33 = 53.
        self.assertEqual(40, m.capacity(x, start, last, "fast"))
        self.assertEqual(180, m.capacity(self.limit(), start, last, "fast"))
        self.assertEqual(3.5, m.total_hours(m.work_intervals(start, last)))

    def test_subscription_end_hides_and_new_measurement_returns(self):
        self.patch_value("SUBSCRIPTION_END", {"Codex": "2026-11-09T13:00:00+06:00"})
        end = m.iso_epoch(m.SUBSCRIPTION_END["Codex"])
        x = self.limit(data_time=end)
        self.assertEqual([], m.warnings(self.data(x)))
        self.assertIn("Codex: no subscription (hidden) | color=gray", m.menubar(self.data(x)))
        self.assertIn("no subscription", m.render_text(self.data(x)))
        x.update(data_time=end + 1, plan="plus")
        self.assertFalse(m.no_subscription(x, self.now))
        self.assertEqual(x["reset"] - 86400, m.target_and_last(x)[0])
        self.assertTrue(m.warnings(self.data(x)))
        x["data_time"] = end - 1
        self.assertEqual(end - 86400, m.target_and_last(x)[0])

    def test_plan_hiding_boundaries_and_views(self):
        end = self.now
        self.patch_value("SUBSCRIPTION_END",
                         {"Codex": m.datetime.fromtimestamp(end, m.TZ).isoformat()})
        for plan in ("free", "plus", None, "new-plan"):
            x = self.limit(plan=plan)
            self.assertFalse(m.no_subscription(x, end - 1))
            self.assertTrue(m.no_subscription(x, end))
            self.assertEqual(plan != "plus",
                             m.no_subscription(dict(x, data_time=end + 1), end + 2))
        for plan, visible in (("free", False), ("plus", True), (None, False)):
            x = self.limit(plan=plan, data_time=end + 1)
            data = self.data(x, codex={"limits": [x], "data_time": end + 1})
            self.assertEqual(visible, bool(m.visible_limits(data, end + 2)))
        x = self.limit(plan="free")
        data = self.data(x, codex={"limits": [x], "data_time": end})
        self.assertNotIn("Codex:", m.render_text(data).split("Models:")[0])
        self.assertNotIn("Measured: Codex", m.menubar(data))
        self.assertEqual(([], [], ""), (m.warnings(data), m.outlook(data), m.facts_line(data)))
        self.assertTrue(m.acquire_lock())
        m.write_history(m.all_limits(data), self.now)
        self.assertEqual(1, len(self.history()))
        self.patch_value("SUBSCRIPTION_END", {})
        for plan, hidden in (("free", True), ("plus", False), (None, False), ("new-plan", False)):
            self.assertEqual(hidden, m.no_subscription(dict(x, plan=plan), end))

    def test_plan_is_read_cached_and_kept(self):
        raw = json.loads(self.codex_line())
        raw["payload"]["rate_limits"]["plan_type"] = "plus"
        section = m.codex_record(raw, self.now)
        self.assertTrue(all(x["plan"] == "plus" for x in section["limits"]))
        self.write_json(m.CACHE, self.data(codex=section))
        cached = m.read_cache()["codex"]
        self.assertEqual(section, cached)
        self.assertEqual("plus", m.keep_last_good({"error": "x"}, cached)["limits"][0]["plan"])
        self.assertTrue(all(x["plan"] is None for x in
                            m.codex_record(json.loads(self.codex_line()), self.now)["limits"]))

    def test_per_evening_column(self):
        now = self.epoch("19:00:00")
        m.time.time.return_value = now
        x = self.limit(remaining=28, reset=self.epoch("22:30:00") + 2 * 86400, data_time=now)
        self.assertEqual("14%", m.per_evening(x, now))
        self.assertEqual("missed", m.per_evening(x, x["reset"]))
        self.assertEqual("-", m.per_evening(dict(x, window="5-hour"), now))
        header, line = [s.partition(" | ")[0] for s in m.menubar(self.data(x)).splitlines()[2:4]]
        self.assertEqual("14%", line[header.index("Per evening"):])
        self.assertEqual("Nov 15 22:30",
                         line[header.index("Resets"):header.index("Per evening")].strip())

    def test_outlook_fresh_week_and_near_end(self):
        x = self.limit(remaining=98, reset=self.now + 7 * 86400)
        result = m.outlook(self.data(x))[0]
        self.assertEqual({"source", "name", "remaining", "reset", "last", "unused", "daily",
                          "stale", "data_time"}, set(result))
        self.assertLess(result["unused"], 10)
        self.assertEqual((14, x["reset"], False),
                         (result["daily"], result["last"], result["stale"]))
        self.assertIn("~14% a day", m.facts_line(self.data(x)))
        start = self.epoch("23:00:00")
        x = dict(x, remaining=28, reset=start + 11 * 3600)
        result = m.outlook(self.data(x), start)[0]
        self.assertGreater(result["unused"], 20)
        self.assertEqual(28, result["daily"])
        self.assertIn("11h 0m left, ~28% goes unused", m.facts_line(self.data(x), start))
        self.assertNotIn("a day", m.facts_line(self.data(x), start))

    def test_outlook_stale_sub_limit_and_subscription_end(self):
        x = self.limit(reset=self.now + 3 * 86400)
        c = dict(x, source="Claude", name="Weekly", remaining=42, data_time=self.now - 21601)
        result = m.outlook(self.data(c))[0]
        self.assertEqual((True, None, None), (result["stale"], result["unused"], result["daily"]))
        self.assertTrue(m.facts_line(self.data(c)).endswith("Claude 42% (stale data)"))
        self.assertFalse(m.outlook(self.data(dict(c, data_time=self.now - 21600)))[0]["stale"])
        self.assertEqual([], m.outlook(self.data(dict(c, name="Weekly · Sonnet"))))
        self.assertEqual([], m.outlook(self.data(dict(x, window="5-hour"))))
        end = self.now + 3600
        self.patch_value("SUBSCRIPTION_END",
                         {"Codex": m.datetime.fromtimestamp(end, m.TZ).isoformat()})
        self.assertEqual((end, x["reset"]), (m.outlook(self.data(x))[0]["last"],
                                             m.outlook(self.data(x))[0]["reset"]))
        self.assertEqual([], m.outlook(self.data(dict(x, reset=self.now - 1))))

    def test_outlook_filters_past_reset_and_last_use(self):
        for difference in (-1, 0):
            data = self.data(self.limit(reset=self.now + difference))
            self.assertEqual(([], ""), (m.outlook(data), m.facts_line(data)))
        with patch.object(m, "target_and_last", return_value=(self.now - 86400, self.now)):
            self.assertEqual([], m.outlook(self.data(self.limit())))
        self.assertEqual(1, len(m.outlook(self.data(self.limit(reset=self.now + 1)))))

    def test_facts_line_order_rounding_and_daily_limit(self):
        x = self.limit(remaining=40, reset=self.now + 3 * 86400)
        g = dict(x, source="Antigravity", name="Gemini Models", reset=self.now + 3600)
        a = dict(g, name="Gemini 4 Argon", reset=self.now + 7200)
        c = dict(x, source="Claude", name="Weekly", reset=self.now + 86400)
        line = m.facts_line(self.data(x, c, a, g))
        self.assertTrue(line.startswith("LIMITS: Gemini 40%"))
        self.assertLess(line.index("Argon 40%"), line.index("Claude 40%"))
        self.assertLess(line.index("Claude 40%"), line.index("Codex 40%"))
        for unused in (0, 0.49, 0.5):
            with patch.object(m, "capacity", return_value=40 - unused):
                line = m.facts_line(self.data(x))
                self.assertIn("nothing goes unused", line)
                self.assertNotIn("~0%", line)
        x = self.limit(remaining=1, reset=self.now + 4 * 86400)
        with patch.object(m, "capacity", return_value=0.49):
            self.assertIn("~1% goes unused", m.facts_line(self.data(x)))
        self.assertNotIn("a day", m.facts_line(self.data(x)))
        self.assertIn("~1% a day", m.facts_line(self.data(dict(x, remaining=4))))
        self.assertNotIn("a day", m.facts_line(self.data(dict(x, reset=self.now + 2 * 86400))))
        self.assertEqual("", m.facts_line(self.data()))

    def test_facts_line_measurement_age(self):
        for source in ("Codex", "Antigravity"):
            for age in (7200, 7201, 10800):
                x = self.limit(source=source, data_time=self.now - age)
                line = m.facts_line(self.data(x))
                self.assertEqual(age > 7200, f"(measured {m.format_duration(age)} ago)" in line)
                self.assertEqual(age > 7200, "(measured " in " ".join(m.warnings(self.data(x))))
        for age in (21600, 21601):
            line = m.facts_line(self.data(self.limit(source="Claude", data_time=self.now - age)))
            self.assertNotIn("(measured ", line)
            self.assertEqual(age > 21600, "(stale data)" in line)

    def test_short_summary_order_limit_and_json(self):
        self.patch_value("SHORT_SUMMARY", True)
        self.patch_value("CLAUDE_MISSING_IS_STALE", True)
        x = self.limit()
        data = self.data(*(dict(x, name=str(i), remaining=1) for i in range(5)))
        self.patch_value("prepare", Mock(return_value=(data, True)))
        output = self.cli("--short")[1]
        self.assertEqual((3, 1), (output.count("LOW:"), output.count("LIMITS")))
        self.assertIn("(+2 warnings", output)
        self.assertLess(output.index("LIMITS"),
                        output.index("CLAUDE DATA STALE: last saved unknown"))
        self.assertLess(m.render_text(data).index("LIMITS"), m.render_text(data).index("Warnings"))
        self.assertEqual(1, json.loads(self.cli("--json")[1])["outlook"][0]["unused"])
        json.dumps(m.json_data(data), allow_nan=False)
        self.patch_value("SHORT_SUMMARY", False)
        self.assertNotIn("LIMITS", self.cli("--short")[1])

    def test_menu_unused_share(self):
        x = self.limit()
        menu = m.menubar(self.data(x, dict(x, window="5-hour")))
        self.assertIn("~40%", menu)
        self.assertIn("~% : share likely to go unused at this pace (estimate)", menu)
        self.assertEqual("-", menu.splitlines()[4].partition(" | ")[0].split()[-1])
        with patch.object(m, "capacity", return_value=100):
            self.assertNotIn("~40%", m.menubar(self.data(x)))
        stale = dict(x, source="Claude", name="Weekly", data_time=self.now - 21601)
        self.assertNotIn("~40%", m.menubar(self.data(stale)))

    def test_backlog_first_item_shown_gray_not_in_short(self):
        self.use_guide_files()
        data = self.data(self.limit())
        self.patch_value("prepare", Mock(return_value=(data, True)))
        before = m.warnings(data)
        m.BACKLOG_FILE.write_text("- [Codex] first task\n- [Codex] second\n- [Claude] other\n")
        self.assertEqual(["Backlog (Codex): first task"], m.backlog_lines(data))
        self.assertEqual(before, m.warnings(data))
        self.assertIn("Backlog (Codex): first task | color=gray", m.menubar(data))
        self.assertIn("Backlog (Codex): first task", m.render_text(data))
        self.assertNotIn("Backlog", self.cli("--short")[1])
        self.patch_value("SHORT_SUMMARY", True)
        self.assertNotIn("Backlog (", self.cli("--short")[1])
        self.assertIn("Backlog: 3 items", self.cli("--short")[1])
        self.assertEqual([], m.backlog_lines(self.data(self.limit(remaining=1))))
        with patch.object(Path, "read_text", side_effect=OSError):
            self.assertEqual([], m.backlog_lines(data))
        self.patch_value("BACKLOG_FILE", None)
        self.assertEqual(([], []), (m.backlog_lines(data), m.guide_lines()))

    def test_backlog_done_section_and_guide_lines(self):
        self.use_guide_files()
        self.assertEqual([], m.guide_lines())
        m.BACKLOG_FILE.write_text("# Tasks\n  - indented is not counted\n")
        self.assertEqual([f"Backlog: empty, {m.BACKLOG_FILE}"], m.guide_lines())
        m.BACKLOG_FILE.write_text("# Tasks\n- active\n## Done\n- [Codex] done\n")
        m.ROSTER_FILE.write_text("# Roster\n")
        self.assertEqual([f"Roster: {m.ROSTER_FILE}", f"Backlog: 1 items, {m.BACKLOG_FILE}"],
                         m.guide_lines())
        data = self.data(self.limit())
        self.assertEqual([], m.backlog_lines(data))
        self.assertLess(m.render_text(data).index("LIMITS"), m.render_text(data).index("Roster:"))
        self.assertNotIn("Roster:", m.menubar(data))

    def test_history_changes_six_hours_and_tail(self):
        self.assertTrue(m.acquire_lock())
        x = self.limit()
        m.write_history([x], self.now)
        with patch.object(m, "tail", wraps=m.tail) as tail:
            m.write_history([x], self.now + 21599)
            tail.assert_called_once()
        self.assertEqual(1, len(self.history()))
        m.write_history([x], self.now + 21600)
        m.write_history([dict(x, remaining=39)], self.now + 21601)
        m.write_history([dict(x, remaining=39, reset=x["reset"] + 1)], self.now + 21602)
        self.assertEqual(4, len(self.history()))
        self.assertEqual((self.now, self.now + 21602),
                         (self.history()[-1]["observed_at"], self.history()[-1]["recorded_at"]))
        m.release_lock()
        with self.assertRaises(RuntimeError):
            m.write_history([x], self.now)

    def test_history_archive_is_kept_on_same_second(self):
        self.patch_value("HISTORY_LIMIT", 1)
        self.assertTrue(m.acquire_lock())
        x = self.limit()
        m.write_history([x], self.now)
        archive = list(m.HISTORY_ARCHIVE.iterdir())
        before = archive[0].read_bytes()
        m.write_history([x], self.now + 1)
        self.assertEqual(1, len(self.history()))
        m.write_history([dict(x, remaining=38)], self.now + 1)
        self.assertEqual(2, len(list(m.HISTORY_ARCHIVE.iterdir())))
        self.assertEqual(before, archive[0].read_bytes())
        self.assertEqual(2, len(self.history()))

    def test_history_from_collection_and_claude_under_lock(self):
        self.mock_collection(codex={"limits": [self.limit()]})
        m.collect()
        self.assertEqual(1, len(self.history()))
        raw = json.dumps({"windows": [dict(label="Weekly", percentUsed=40,
                                           resetsAt="2026-11-15T00:00:00+06:00")]})
        write = m.write_history

        def check(*args):
            self.assertIsNotNone(m._lock)
            return write(*args)
        with patch.object(m, "write_history", side_effect=check):
            m.claude_save(raw)
        self.assertEqual(2, len(self.history()))
        with patch.object(m, "write_history", side_effect=OSError):
            self.assertIn("codex", m.collect())
            self.assertIn("windows", m.claude_save(raw))
        self.assertIsNone(m._lock)
        self.assertTrue(m.acquire_lock())
        with self.assertRaises(ValueError):
            m.claude_save(raw)

    def test_fast_modes_skip_history(self):
        self.mock_collection()
        self.patch_value("refresh_background", Mock())
        with patch.object(m, "history_records", side_effect=AssertionError), \
                patch.object(m, "write_history", side_effect=AssertionError), \
                patch.object(m, "backfill_history", side_effect=AssertionError), \
                patch.object(m, "usage_pattern", side_effect=AssertionError):
            for mode in ("--short", "--menubar"):
                for extra in ("--backfill-history", "--usage-pattern"):
                    self.assertEqual(0, self.cli(mode, extra)[0])
        self.assertFalse(m.HISTORY.exists())

    def test_backfill_raw_percent_no_duplicates(self):
        raw = json.loads(self.codex_line(used_percent=28.25, resets_at=self.now - 1))
        raw["timestamp"] = "2026-11-01T10:00:00Z"
        raw["payload"]["rate_limits"]["secondary"] = dict(
            used_percent=50, resets_at=self.now - 5, window_minutes=300)
        self.write_codex("a.jsonl", json.dumps(raw) + "\n" + json.dumps(raw))
        self.write_codex("b.jsonl", json.dumps(raw) + '\n{"rate_limits": null}\ninvalid')
        self.patch_value("HISTORY_LIMIT", 1)
        self.assertIn("Added 2 lines", self.cli("--backfill-history")[1])
        records = self.history()
        weekly = next(x for x in records if x["window"] == "weekly")
        self.assertEqual(71.75, weekly["remaining"])
        self.assertEqual(m.iso_epoch(raw["timestamp"]), weekly["observed_at"])
        self.assertIn("Added 0 lines", self.cli("--backfill-history")[1])
        self.assertEqual(records, self.history())
        self.assertIsNone(m._lock)

    def test_backfill_adds_missing_window_at_same_time(self):
        raw = json.loads(self.codex_line(used_percent=20))
        raw["timestamp"] = "2026-11-01T10:00:00Z"
        raw["payload"]["timestamp"] = None
        raw["payload"]["rate_limits"]["secondary"] = dict(
            used_percent=50, resets_at=self.now + 60, window_minutes=300)
        self.write_codex("a.jsonl", json.dumps(raw))
        observed = m.iso_epoch(raw["timestamp"])
        self.assertEqual(observed, m.codex_usage()["data_time"])
        self.assertTrue(m.acquire_lock())
        m.append_history([dict(source="Codex", name="Codex", window="5-hour", remaining=50,
                               reset=self.now + 60, observed_at=observed, recorded_at=self.now)])
        m.release_lock()
        self.assertIn("Added 1 lines", self.cli("--backfill-history")[1])
        self.assertEqual({"5-hour", "weekly"}, {x["window"] for x in self.history()})

    def test_usage_pattern_pairs_and_hours(self):
        start = self.epoch("16:00:00")
        reset = start + 7 * 86400
        # 100 -> 80 is valid (3 h); a rise and a 600 s reset shift are not;
        # the last 5-point drop took 1 h, so it does not count as the fastest rate.
        lines = [dict(source="Codex", name="Codex", window="weekly", remaining=remaining,
                      reset=reset + shift, observed_at=start + hour * 3600,
                      recorded_at=self.now)
                 for hour, remaining, shift in ((0, 100, 0), (3, 80, 0), (4, 90, 0),
                                                (5, 70, 600), (6, 65, 600))]
        self.assertTrue(m.acquire_lock())
        m.append_history(lines + [lines[-1]])
        m.release_lock()
        report = self.cli("--usage-pattern")[1]
        self.assertIn("Codex: 25.0% used in total, 2 pairs", report)
        self.assertIn("19:00–19:59: 80.0% share, 20.0% used", report)
        self.assertIn("19:00–22:30 share: 100.0%", report)
        self.assertIn("~6.7%/hour", report)
        m.HISTORY.unlink()
        self.assertIn("No weekly observations", self.cli("--usage-pattern")[1])

    def test_fast_modes_do_not_read_codex_sessions(self):
        self.write_json(m.CACHE, self.data(self.limit(remaining=1)))
        self.patch_value("refresh_background", Mock())
        self.patch_value("codex_usage", Mock(return_value={"limits": [self.limit(remaining=2)]}))
        for mode in ("--short", "--menubar"):
            for extra in ([], ["--refresh"]):
                code, output, _ = self.cli(mode, *extra)
                self.assertEqual(0, code)
                self.assertIn("1%", output)
        m.codex_usage.assert_not_called()
        self.assertIn("2%", self.cli()[1])
        m.codex_usage.assert_called_once()

    def test_new_source_baseline_warns_until_accepted(self):
        self.mock_collection(models=self.models("codex", a="A"))
        m.collect()
        m.models.return_value = {"successful": ["codex", "agy"], "errors": {},
                                 "models": {"codex:a": "A", "agy:x": "X"}}
        m.collect()
        m.collect()
        data = m.read_cache()
        line = next(s for s in m.warnings(data) if s.startswith("NEW SOURCE: agy"))
        self.assertIn("(1 found)", line)
        self.assertIn("tidewatch.py --accept-models", line)
        self.assertIn(line + " | color=#0A84FF", m.menubar(data))
        self.assertEqual("AI | color=#0A84FF", m.menubar(data).splitlines()[0])
        with patch.object(m, "prepare", Mock(return_value=(data, True))):
            self.assertIn("NEW SOURCE: agy", self.cli("--short")[1])
        self.assertEqual(0, self.cli("--accept-models")[0])
        self.assertNotIn("NEW SOURCE", str(m.warnings(m.read_cache())))
        m.collect()
        self.assertNotIn("NEW SOURCE", str(m.warnings(m.read_cache())))
        self.assertEqual({"codex:a": "A", "agy:x": "X"}, m.read_json(m.MODELS_FILE)["models"])

    def test_strict_codex_model_files(self):
        self.write_json(m.MODELS_FILE, {"models": {"codex:x": "x", "codex:y": "Y"}})
        before = m.MODELS_FILE.read_bytes()
        self.write_json(m.HOME / ".codex/models_cache.json", {"models": [{"slug": "x"}]})
        desktop = m.HOME / ".codex/models_cache.codex_work_desktop.json"
        self.assertEqual({"codex:x": "x"}, m.codex_models())
        self.patch_value("STRICT_CODEX_MODEL_FILES", True)
        self.patch_value("agy_models", Mock(side_effect=ValueError("unreachable")))
        for content in (None, {"models": []}):
            with self.subTest(content=content):
                if content is not None:
                    self.write_json(desktop, content)
                with self.assertRaises(ValueError):
                    m.codex_models()
                self.assertIn("codex", m.models()["errors"])
                self.assertEqual(1, self.cli("--accept-models")[0])
                self.assertEqual(before, m.MODELS_FILE.read_bytes())

    def test_partial_accept_keeps_failed_source_notices_and_models(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:old": "Old", "codex:x": "X"}})
        self.write_json(m.CACHE, self.data(self.limit(), models_status={
            "new": {"agy:new": "New", "codex:y": "Y"}, "removed": {"agy:old": "Old"}},
            model_sources={"models": {"agy:old": "Old", "agy:b": "B", "codex:x": "X"},
                           "errors": {}}))
        data = self.models("codex", x="X", y="Y")
        data["errors"] = {"agy": "error"}
        self.patch_value("models", Mock(return_value=data))
        self.assertEqual(0, self.cli("--accept-models")[0])
        cache = m.read_cache()
        self.assertEqual({"agy:new": "New"}, cache["models_status"]["new"])
        self.assertEqual({"agy:old": "Old"}, cache["models_status"]["removed"])
        self.assertTrue(m.warnings(cache)[0].startswith("NEW MODEL: New (agy:new)"))
        self.assertIn("codex:y", m.read_json(m.MODELS_FILE)["models"])
        self.assertEqual({"agy:old": "Old", "agy:b": "B", "codex:x": "X", "codex:y": "Y"},
                         cache["model_sources"]["models"])
        self.assertIn("Models: 4", m.render_text(cache))

    def test_collection_keeps_failed_source_notices(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:a": "A", "codex:x": "X"}})
        self.write_json(m.CACHE, self.data(self.limit(), models_status={
            "new": {"agy:b": "B", "codex:y": "Y"}, "removed": {"agy:a": "A"}}))
        data = self.models("codex", x="X")
        data["errors"] = {"agy": "error"}
        self.mock_collection(models=data)
        status = m.collect()["models_status"]
        self.assertEqual(({"agy:b": "B"}, {"agy:a": "A"}), (status["new"], status["removed"]))

    def test_collection_reads_old_cache_under_lock(self):
        def agy(remaining, age):
            return {"limits": [self.limit(source="Antigravity", name="Gemini",
                                          remaining=remaining, data_time=self.now - age)],
                    "data_time": self.now - age}
        self.write_json(m.CACHE, self.data(agy=agy(80, 600)))
        new = agy(50, 60)
        lock = m.acquire_lock

        def interleaved():
            # Another refresh finished between an early read and taking the lock.
            self.write_json(m.CACHE, self.data(agy=new))
            return lock()
        self.patch_value("acquire_lock", interleaved)
        self.mock_collection()
        self.assertEqual(new["limits"], m.collect()["agy"]["limits"])
        self.assertEqual(new["limits"], m.read_cache()["agy"]["limits"])
        with patch.object(m, "read_cache", side_effect=OSError("read")):
            with self.assertRaises(OSError):
                m.collect()
        self.assertIsNone(m._lock)

    def test_short_order_and_sub_limit(self):
        alerts = ["NEW MODEL: A (agy:a): review x", "NEW MODEL: B (agy:b): review x",
                  "LOW: z", "OFF PACE: g", "AT RISK: k"]
        result = m.short_warnings(alerts)
        self.assertTrue(result[0].startswith("NEW MODEL (2): A (agy:a), B (agy:b): review"))
        self.assertEqual(["AT RISK: k", "OFF PACE: g", "LOW: z"], result[1:])
        self.assertTrue(m.sub_limit({"source": "Claude", "name": "Weekly · Sonnet"}))
        self.assertFalse(m.sub_limit({"source": "Claude", "name": "Weekly · all models"}))
        self.assertFalse(m.sub_limit({"source": "Antigravity", "name": "Gemini · x"}))

    def test_config_file_settings_and_messages(self):
        config = self.root / "personal" / "config.json"
        config.parent.mkdir()
        (config.parent / "strings.json").write_text(json.dumps({
            "percent": "%{}", "label_low": "ALMOST EMPTY", "decimal_comma": True}))
        self.write_json(config, {
            "data_dir": "state", "messages": "strings.json", "timezone_offset_hours": 6,
            "work_hours": [["18:00", "20:00"]], "night_hours": [],
            "subscription_end": {"Codex": "2026-11-20T00:00:00+06:00"},
            "backlog_file": "backlog.md", "roster_file": "roster.md", "hello_prompt": "hi",
            "short_summary": True, "claude_missing_is_stale": True,
            "strict_codex_model_files": True, "fast_rate": {"Codex": 5}})
        names = ("DATA", "CACHE", "CLAUDE_CACHE", "MODELS_FILE", "LOCK", "HELLO_STATE",
                 "HELLO_LOG", "HISTORY", "HISTORY_ARCHIVE", "SETTINGS", "CONFIG", "WORK_HOURS",
                 "NIGHT_HOURS", "BACKLOG_FILE", "ROSTER_FILE", "HELLO_PROMPT", "SHORT_SUMMARY",
                 "CLAUDE_MISSING_IS_STALE", "STRICT_CODEX_MODEL_FILES", "FAST_RATE")
        for name in names:
            self.patch_value(name, getattr(m, name))
        self.patch_value("MESSAGES", dict(m.MESSAGES))
        m.configure(config)
        self.assertEqual(config.parent / "state" / "limits.json", m.CACHE)
        self.assertEqual(config.parent / "state" / "history.jsonl", m.HISTORY)
        self.assertEqual((config.resolve(), config.resolve()), (m.CONFIG, m.SETTINGS))
        self.assertEqual(([("18:00", "20:00")], []), (m.WORK_HOURS, m.NIGHT_HOURS))
        self.assertEqual(config.parent / "backlog.md", m.BACKLOG_FILE)
        self.assertEqual(("hi", True, True, True),
                         (m.HELLO_PROMPT, m.SHORT_SUMMARY, m.CLAUDE_MISSING_IS_STALE,
                          m.STRICT_CODEX_MODEL_FILES))
        self.assertEqual(dict(m.FAST_RATE, Codex=5), m.FAST_RATE)
        self.assertEqual("ALMOST EMPTY: Codex weekly %9 left", m.warnings(self.data(
            self.limit(remaining=9)))[0].split(", resets")[0])
        self.assertEqual("2,5", m.decimal(2.5))
        m.apply_settings({"work_hours": [["9:05", "21:00"]]}, config.parent)
        self.assertEqual([("09:05", "21:00")], m.WORK_HOURS)
        for bad in ({"messages": {"no_such_key": "x"}}, {"short_summary": 1}, [],
                    {"work_hours": [["7pm", "22:00"]]}, {"work_hours": 5},
                    {"night_hours": [["22:00"]]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                m.apply_settings(bad, config.parent)

    def test_config_is_passed_to_background_and_menu_refresh(self):
        config = self.root / "my config.json"
        self.patch_value("CONFIG", config)
        self.popen.side_effect = None
        m.refresh_background()
        self.assertEqual([m.sys.executable, str(m.ROOT / "tidewatch.py"), "--refresh",
                          "--quiet", "--config", str(config)], self.popen.call_args.args[0])
        self.assertTrue(m.menubar({}).splitlines()[-1].endswith(
            f'param2=--refresh param3=--quiet param4=--config param5="{config}" '
            "terminal=false refresh=true"))

    def test_config_from_flag_and_environment(self):
        config = self.root / "config.json"
        self.write_json(config, {"short_summary": False})
        with patch.object(m, "configure") as configure, \
                patch.object(m, "prepare", return_value=({}, True)):
            self.assertEqual(0, self.cli("--short", "--config", str(config))[0])
            configure.assert_called_once_with(str(config))
            configure.reset_mock()
            with patch.dict(os.environ, {"TIDEWATCH_CONFIG": str(config)}):
                self.cli("--short")
            configure.assert_called_once_with(str(config))
            configure.reset_mock()
            self.cli("--short")
            configure.assert_not_called()
            configure.side_effect = ValueError("invalid")
            self.assertEqual((0, "", ""), self.cli("--short", "--config", str(config)))
            self.assertEqual(1, self.cli("--json", "--config", str(config))[0])
        # A broken default settings file is ignored, like before.
        m.DATA.mkdir(parents=True, exist_ok=True)
        for raw in ("{", "[]", '{"work_hours": 5}', "[" * 2000 + "]" * 2000):
            m.SETTINGS.write_text(raw)
            with patch.object(m, "prepare", return_value=({}, True)):
                code, output, _ = self.cli()
                self.assertEqual((0, "Data: cache, 0m ago"), (code, output.splitlines()[0]))


if __name__ == "__main__":
    unittest.main()
