"""Behaviour in Turkish (locales/tr.json) with evening and night hours, a UTC+6 zone,
automatic hello, the agent summary in --short and strict Codex model files.

All I/O uses a temporary directory; processes are mocked and forbidden by default."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import sys
import runpy
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import tidewatch as m  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SETTINGS = {"language": "tr", "timezone_offset_hours": 6, "work_hours": [["19:00", "22:30"]],
            "night_hours": [["22:30", "04:00"]], "hello_prompt": "merhaba", "short_summary": True,
            "claude_missing_is_stale": True, "strict_codex_model_files": True}
# Turkish spellings of the options, as a user might map them in a shell alias.
OPTIONS = {"--kisa": "--short", "--yenile": "--refresh", "--sessiz": "--quiet",
           "--kabul": "--accept-models", "--claude-yaz": "--claude-save",
           "--gecmis-doldur": "--backfill-history", "--aliskanlik": "--usage-pattern"}


class LimitTest(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.root = Path(temp_dir.name)
        env_patch = patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex")})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in ("TIDEWATCH_CONFIG", "TIDEWATCH_LANG"):
            os.environ.pop(name, None)
        for name in ("TZ", "WORK_HOURS", "NIGHT_HOURS", "HELLO_PROMPT", "SHORT_SUMMARY",
                     "CLAUDE_MISSING_IS_STALE", "STRICT_CODEX_MODEL_FILES", "LANGUAGE",
                     "MESSAGE_OVERRIDES", "CONFIG"):
            self.patch_value(name, getattr(m, name))
        self.patch_value("MESSAGES", dict(m.ENGLISH))
        self.patch_value("system_language", lambda: "en")
        self.patch_value("USER_SETTINGS", self.root / "config" / "settings.json")
        m.apply_settings(SETTINGS, self.root)
        self.now = 1794211200.0
        self.patch_value("SUBSCRIPTION_END", {})
        paths = dict(ROOT=self.root, HOME=self.root, DATA=self.root / "veri")
        for name, filename in (("CACHE", "limitler.json"), ("CLAUDE_CACHE", "claude-kullanim.json"),
                               ("MODELS_FILE", "bilinen-modeller.json"), ("LOCK", "yenileniyor"),
                               ("HELLO_STATE", "merhaba.json"), ("HELLO_LOG", "merhaba-log.jsonl"),
                               ("HISTORY", "gecmis.jsonl"), ("HISTORY_ARCHIVE", "gecmis-arsiv")):
            paths[name] = paths["DATA"] / filename
        paths["ROSTER_FILE"] = self.root / "roster.md"
        paths["BACKLOG_FILE"] = self.root / "backlog.md"
        for name, value in paths.items():
            self.patch_value(name, value)
        self.timestamp = patch.object(m.time, "time", return_value=self.now)
        self.timestamp.start()
        self.addCleanup(self.timestamp.stop)
        self.proc_patch = patch.object(m.subprocess, "Popen",
                                       side_effect=AssertionError("Gerçek süreç"))
        self.popen = self.proc_patch.start()
        self.addCleanup(self.proc_patch.stop)
        self.addCleanup(m.release_lock)
        # Auto hello is enabled in personal settings.
        self.patch_value("SETTINGS", self.root / "ayarlar.json")
        self.write_json(m.SETTINGS, {"auto_hello": True})

    def patch_value(self, name, value):
        patcher = patch.object(m, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def limit(self, **changes):
        return dict(dict(source="Codex", name="Codex", window="weekly", remaining=40,
                         reset=self.now + 3600, data_time=self.now, plan=None), **changes)

    def data(self, *limits, **other):
        return dict(dict(version=2, timestamp=self.now, codex={"limits": list(limits)}), **other)

    def write_json(self, path, payload):
        m.atomic_json(path, payload)

    def cli(self, *args):
        out, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(error):
            code = m.main([OPTIONS.get(a, a) for a in args])
        return code, out.getvalue(), error.getvalue()

    def models(self, source="agy", **items):
        return {"successful": [source], "models": {source + ":" + k: v for k, v in items.items()},
                "errors": {}}

    def fake_collect(self, agy=None, codex=None, claude=None, models=None):
        for name, value in (("agy_usage", agy), ("codex_usage", codex),
                            ("claude_usage", claude), ("models", models)):
            default = ({"successful": [], "models": {}, "errors": {}} if name == "models"
                       else {"error": "yok"})
            self.patch_value(name, Mock(return_value=default if value is None else value))

    def test_warning_texts_and_boundaries(self):
        for hours in (1, 12, 24):
            x = self.limit(reset=self.now + hours * 3600, remaining=30)
            self.assertTrue(m.warnings(self.data(x))[0].startswith("KURTAR:"))
        for x in (self.limit(reset=self.now, remaining=1),
                  self.limit(window="5-hour", remaining=100),
                  self.limit(reset=self.now + 7 * 86400)):
            self.assertEqual([], m.warnings(self.data(x)))
        x = self.limit(remaining=9, data_time=self.now - 10800)
        warning = m.warnings(self.data(x))[0]
        self.assertEqual("AZ: Codex haftalık %9 kaldı, sıfırlanma " +
                         m.format_date(x["reset"]) + " (1s 0dk) (ölçüm 3s 0dk önce)", warning)
        self.assertNotIn("Codex Codex", warning)
        self.assertTrue(m.warnings(self.data(self.limit(remaining=10))))
        new = {"models_status": {"new": {"agy:x": "X"}}}
        self.assertEqual(["YENİ MODEL: X (agy:x) — kullandığın modelleri gözden geçir, "
                          "sonra tidewatch.py --accept-models"],
                         m.warnings(new))

    def test_claude_warning_and_stale_hint(self):
        x = self.limit(source="Claude", name="Weekly", remaining=65, reset=self.now + 9 * 3600)
        data = self.data(claude={"saved_at": self.now, "limits": [x]})
        self.assertTrue(m.warnings(data)[0].startswith("KURTAR:"))
        self.assertEqual("", m.claude_stale_hint(data))
        for age in (21600, 21601):
            x["data_time"] = self.now - age
            data["claude"]["saved_at"] = x["data_time"]
            self.assertEqual(age == 21600, bool(m.warnings(data)))
            self.assertEqual(age > 21600, bool(m.claude_stale_hint(data)))
        data["claude"]["saved_at"] = self.now
        x["reset"] = self.now - 1
        self.assertTrue(m.claude_stale_hint(data))
        self.assertTrue(m.claude_stale_hint({}))

    def test_new_model_priority_age_and_color(self):
        data = self.data(*(self.limit(name=str(i), remaining=1) for i in range(4)),
                         timestamp=self.now - 10800,
                         models_status={"new": {"agy:x": "X"}})
        warnings = m.warnings(data)
        self.assertTrue(warnings[0].startswith("YENİ MODEL: X (agy:x)"))
        self.assertTrue(warnings[0].endswith("(ölçüm 3s 0dk önce)"))
        self.assertEqual("#0A84FF", m.warning_color(warnings[0]))
        self.patch_value("prepare", Mock(return_value=(data, True)))
        self.assertIn(warnings[0], self.cli("--kisa")[1])

    def test_short_truncation_hint_and_quiet(self):
        data = {"models_status": {"new": {str(i): "X" for i in range(5)}}}
        self.patch_value("prepare", Mock(return_value=(data, True)))
        code, out, error = self.cli("--kisa")
        self.assertEqual((0, ""), (code, error))
        self.assertEqual(1, out.count("YENİ MODEL"))
        self.assertIn("YENİ MODEL (5):", out)
        self.assertIn("CLAUDE VERİSİ ESKİ", out)
        self.assertEqual(2, len(out.splitlines()))
        self.assertEqual((0, "", ""), self.cli("--kisa", "--sessiz"))
        m.prepare.return_value = ({"claude": {"saved_at": self.now}}, True)
        self.assertEqual((0, "", ""), self.cli("--kisa"))
        m.prepare.side_effect = KeyboardInterrupt
        self.assertEqual((0, "", ""), self.cli("--kisa"))
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
        self.assertEqual("14.11 10:43", m.format_date(data["limits"][0]["reset"]))

    def test_agy_invalid_fields_source_error(self):
        sample = json.loads((FIXTURES / "agy-usage.json").read_text())
        bucket = sample["command"]["data"]["groups"][0]["buckets"][0]
        for field, value in (("remaining_fraction", None), ("remaining_fraction", float("nan")),
                             ("remaining_fraction", True), ("remaining_fraction", 2),
                             ("reset_time", "bozuk"), ("window", [])):
            raw = json.loads(json.dumps(sample))
            raw["command"]["data"]["groups"][0]["buckets"][0][field] = value
            with self.subTest(field=field, value=value), patch.object(
                    m, "run_agy", return_value=json.dumps(raw)):
                self.assertIn("error", m.safe(m.agy_usage))
        self.assertIn("remaining_fraction", bucket)

    def test_agy_process_and_timeout(self):
        proc = Mock(returncode=0, pid=43210)
        proc.communicate.return_value = ("yanit", "")
        self.popen.side_effect = None
        self.popen.return_value = proc
        self.assertEqual("yanit", m.run_agy(["models"], 30))
        kw = self.popen.call_args.kwargs
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(str(m.HOME), kw["cwd"])
        self.assertEqual(subprocess.DEVNULL, kw["stdin"])
        proc.returncode = 1
        with self.assertRaises(RuntimeError):
            m.run_agy(["models"], 1)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("agy", 1), ("", "")]
        with patch.object(m.os, "killpg") as kill_pg:
            with self.assertRaises(TimeoutError):
                m.run_agy(["models"], 1)
            kill_pg.assert_called_once_with(43210, signal.SIGKILL)
        proc.communicate.side_effect = subprocess.TimeoutExpired("agy", 1)
        with patch.object(m.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(TimeoutError):
                m.run_agy(["models"], 1)
        proc.stdout.close.assert_called_once()
        proc.stderr.close.assert_called_once()

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

    def test_codex_large_file_null_corrupt_line(self):
        path = self.write_codex("a.jsonl", "ğ" * 600000 + "\n" + self.codex_line() +
                                '\n{bozuk "rate_limits"\n{"rate_limits":{}}\n', age=100)
        with patch.object(m, "tail", wraps=m.tail) as read_tail:
            data = m.codex_usage()
            read_tail.assert_called_once_with(path)
        self.assertEqual(75, data["limits"][0]["remaining"])
        self.assertEqual(1, len(data["limits"]))
        self.assertEqual(self.now - 100, data["data_time"])
        self.assertLess(sum(map(len, m.tail(path))), 512 * 1024)

    def test_codex_file_error_and_newest_record(self):
        self.write_codex("a.jsonl", self.codex_line(), age=50)
        newest = json.loads(self.codex_line(used_percent=10))
        newest["timestamp"] = m.datetime.fromtimestamp(self.now, m.TZ).isoformat()
        self.write_codex("b.jsonl", json.dumps(newest), age=100)
        corrupt = self.write_codex("c.jsonl", "", age=0)
        corrupt.write_bytes(b'\xff"rate_limits"')
        self.assertEqual(90, m.codex_usage()["limits"][0]["remaining"])
        with patch.object(m, "tail", side_effect=OSError):
            self.assertIn("error", m.safe(m.codex_usage))

    def test_codex_five_files_and_time_budget(self):
        for i in range(8):
            self.write_codex(str(i) + ".jsonl", '"rate_limits"', age=i)
        with patch.object(m, "tail", wraps=m.tail) as read_tail:
            self.assertIn("error", m.safe(m.codex_usage))
            self.assertEqual(5, read_tail.call_count)
        with patch.object(m.time, "monotonic", side_effect=[0, 2, 2]):
            self.assertIn("error", m.safe(m.codex_usage))
        for mins, name in ((300, "5-hour"), (17, "17 minutes")):
            self.write_codex("0.jsonl", self.codex_line(window_minutes=mins))
            self.assertEqual(name, m.codex_usage()["limits"][0]["window"])

    def test_claude_validation_preserves_existing_record(self):
        self.write_json(m.CLAUDE_CACHE, {"ozgun": 1})
        sample = dict(label="Weekly", percentUsed=3, resetsAt="2027-01-01T00:00:00Z")
        windows = [[], None, [None], [dict(sample, label="")],
                   [dict(sample, percentUsed=None)], [dict(sample, percentUsed=True)],
                   [dict(sample, percentUsed=101)], [dict(sample, resetsAt="bozuk")],
                   [dict(sample, resetsAt=None)]]
        for items in windows:
            with self.subTest(items=items):
                with patch.object(m.sys, "stdin", io.StringIO(json.dumps({"windows": items}))):
                    self.assertEqual(1, self.cli("--claude-yaz")[0])
                self.assertEqual({"ozgun": 1}, m.read_json(m.CLAUDE_CACHE))
        with patch.object(m.sys, "stdin", io.StringIO("{}")):
            self.assertEqual((1, "", ""), self.cli("--claude-yaz", "--sessiz"))
        for wrapped in (False, True):
            data = {"windows": [dict(sample, resetsAt=None), sample]}
            m.claude_save(json.dumps({"plan": data} if wrapped else data))
            self.assertEqual(1, len(m.read_json(m.CLAUDE_CACHE)["windows"]))
            self.assertEqual(97, m.claude_usage()["limits"][0]["remaining"])

    def test_claude_missing_and_corrupt(self):
        self.assertIn("note", m.claude_usage())
        for data in ({}, {"saved_at": None}, {"saved_at": self.now, "windows": []}):
            self.write_json(m.CLAUDE_CACHE, data)
            self.assertIn("error", m.safe(m.claude_usage))

    def test_models_fixture_and_empty_source(self):
        with patch.object(m, "run_agy", return_value=(FIXTURES / "agy-models.txt").read_text()):
            self.assertEqual(13, len(m.agy_models()))
        for raw in ("", "Fetching available models...", "bilgi"):
            with patch.object(m, "run_agy", return_value=raw):
                with self.assertRaises(ValueError):
                    m.agy_models()
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

    def test_codex_missing_and_empty_model_file_keeps_baseline(self):
        names = ("models_cache.json", "models_cache.codex_work_desktop.json")
        paths = [m.HOME / ".codex" / name for name in names]
        for i, path in enumerate(paths):
            self.write_json(path, {"models": [{"slug": str(i)}]})
        m.model_status({"successful": ["codex"], "models": m.codex_models()}, True)
        before = m.MODELS_FILE.read_bytes()
        self.patch_value("agy_models", Mock(side_effect=ValueError("erişilemiyor")))
        exists, read_json = Path.exists, m.read_json
        for path in paths:
            for state in ("eksik", "boş"):
                with self.subTest(path=path.name, state=state), patch.object(
                        Path, "exists", lambda p: False if p == path and state == "eksik"
                        else exists(p)), patch.object(
                        m, "read_json", side_effect=lambda p: {"models": []}
                        if p == path and state == "boş" else read_json(p)):
                    result = m.models()
                    self.assertNotIn("codex", result["successful"])
                    self.assertIn("codex", result["errors"])
                    with self.assertRaises(ValueError):
                        m.model_status(result, True)
                    self.assertEqual(before, m.MODELS_FILE.read_bytes())

    def test_model_baseline_new_removed_and_partial_accept(self):
        data = self.models("codex", old="Old")
        self.assertEqual({"codex": 1}, m.model_status(data)["baseline_created"])
        self.assertIn("agy için taban oluşturuldu (1 model)",
                      m.render_text({"models_status": m.model_status(self.models(x="X"))}))
        data = self.models("codex", new="New")
        status = m.model_status(data)
        self.assertEqual({"codex:new": "New"}, status["new"])
        self.assertEqual({"codex:old": "Old"}, status["removed"])
        self.assertIn("Kaybolan model", m.render_text({"models_status": status}))
        m.model_status(data, True)
        self.assertEqual({"agy:x": "X", "codex:new": "New"},
                         m.read_json(m.MODELS_FILE)["models"])

    def test_corrupt_model_baseline_not_overwritten(self):
        for raw in ("{", "[]", '{"models": null}', '{"models": {"agy:x": null}}'):
            m.MODELS_FILE.parent.mkdir(parents=True, exist_ok=True)
            m.MODELS_FILE.write_text(raw)
            self.assertIn("error", m.safe(m.model_status, self.models(x="X")))
            self.assertEqual(raw, m.MODELS_FILE.read_text())
            self.assertIn("error", m.safe(m.model_status, self.models(x="X"), True))

    def test_accept_models_failure_and_success(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:old": "Old", "codex:x": "X"}})
        before = m.MODELS_FILE.read_bytes()
        self.patch_value("models", Mock(return_value={"successful": [], "models": {},
                                                      "errors": {"agy": "error"}}))
        self.assertEqual(1, self.cli("--kabul")[0])
        self.assertEqual((1, "", ""), self.cli("--kabul", "--sessiz"))
        self.assertEqual(before, m.MODELS_FILE.read_bytes())
        data = self.data(self.limit(), models_status={"new": {"agy:new": "New"}})
        self.write_json(m.CACHE, data)
        m.models.return_value = self.models(new="New")
        code, out, _ = self.cli("--kabul")
        self.assertEqual(0, code)
        self.assertIn("Karşılaştırılan kaynaklar: agy", out)
        self.assertIn("Eklenen: New", out)
        self.assertIn("Çıkarılan: Old", out)
        self.assertFalse(m.warnings(m.read_cache())[1:])
        self.assertNotIn("YENİ MODEL", str(m.warnings(m.read_cache())))
        self.assertIn("codex:x", m.read_json(m.MODELS_FILE)["models"])
        self.assertEqual(1, len(m.read_cache()["codex"]["limits"]))

    def test_lock_ownership_staleness_and_separate_fd(self):
        self.assertTrue(m.acquire_lock())
        self.assertFalse(m.acquire_lock())
        # Another call that forgets the same module's ownership state cannot acquire it either.
        owner = m._lock
        m._lock = None
        try:
            self.assertFalse(m.acquire_lock())
        finally:
            m._lock = owner
        m.LOCK.write_text("baska")
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

    def test_collect_last_good_data_and_lock_finally(self):
        stale = {"limits": [self.limit(source="Antigravity", name="Gemini")],
                 "data_time": self.now - 100}
        self.write_json(m.CACHE, self.data(self.limit(), agy=stale))
        self.fake_collect(codex={"limits": [self.limit()]})
        result = m.collect()
        self.assertEqual(stale["limits"], result["agy"]["limits"])
        self.assertEqual("yok", result["agy"]["last_error"])
        self.assertNotIn("warnings", m.read_json(m.CACHE))
        self.assertFalse(m.LOCK.exists())
        with patch.object(m, "atomic_json", side_effect=OSError):
            with self.assertRaises(OSError):
                m.collect()
        self.assertFalse(m.LOCK.exists())

    def test_refresh_skips_sources_when_locked(self):
        self.fake_collect()
        self.assertTrue(m.acquire_lock())
        self.assertEqual("yenileme zaten sürüyor", m.collect()["note"])
        m.agy_usage.assert_not_called()
        self.assertIn("yenileme zaten sürüyor", self.cli("--yenile")[1])
        self.assertEqual((0, "", ""), self.cli("--yenile", "--sessiz"))

    def test_cache_corrupt_fields_isolated_per_source(self):
        for raw in ("{", "[]", "{}", '{"timestamp":null,"version":2}',
                    '{"timestamp":100,"version":1}'):
            m.CACHE.parent.mkdir(parents=True, exist_ok=True)
            m.CACHE.write_text(raw)
            self.assertIsNone(m.read_cache())
        for corrupt in (None, [], {"limits": [None]}, {"limits": "x"},
                        {"data_time": None}, {"note": []}):
            self.write_json(m.CACHE, self.data(self.limit(), agy=corrupt))
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

    def test_fast_modes_never_wait_for_agy(self):
        self.fake_collect(codex={"limits": [self.limit()]})
        self.patch_value("refresh_background", Mock())
        start = time.perf_counter()
        for mode in ("--kisa", "--menubar"):
            for extra in ([], ["--yenile"], ["--kabul"], ["--claude-yaz"]):
                self.assertEqual(0, self.cli(mode, *extra)[0])
        self.assertLess(time.perf_counter() - start, 2)
        m.agy_usage.assert_not_called()
        m.models.assert_not_called()
        self.write_json(m.CACHE, self.data(self.limit()))
        m.refresh_background.reset_mock()
        self.cli("--kisa")
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
        self.fake_collect()
        self.popen.side_effect = OSError
        self.assertEqual(0, self.cli("--kisa")[0])

    def test_normal_json_fresh_and_refresh(self):
        self.fake_collect(codex={"limits": [self.limit()]})
        self.write_json(m.CACHE, self.data(self.limit()))
        self.assertEqual(0, self.cli("--json")[0])
        m.agy_usage.assert_not_called()
        code, out, _ = self.cli("--json", "--yenile")
        self.assertEqual(0, code)
        raw = json.loads(out)
        self.assertIn("warnings", raw)
        self.assertIn("reset_text", raw["codex"]["limits"][0])
        self.assertNotIn("warnings", m.read_json(m.CACHE))
        m.agy_usage.assert_called_once()
        self.assertIn("önbellek", self.cli()[1])

    def test_weekly_detection(self):
        x = self.limit(source="Antigravity", name="Gemini Models", remaining=100,
                       reset=self.now + 7 * 86400)
        stale = dict(x, reset=x["reset"] - 600, data_time=self.now - 600)
        data = self.models(**{"flash-low": "X"})
        cases = [
            ("kayan", stale, True),
            ("sabit", dict(stale, reset=x["reset"]), False),
            ("sabit kısa aralık", dict(stale, reset=x["reset"],
                                      data_time=self.now - 60), False),
            ("ilk sıfırlanma", dict(stale, reset=x["reset"] - 7 * 86400), False),
            ("önceki 99", dict(stale, remaining=99), False),
            ("başka grup", dict(stale, name="Gemini 4 Argon"), False),
            ("kısa aralık", dict(stale, data_time=self.now - 59), False),
            ("tolerans sınırı", dict(stale, reset=x["reset"] - 900), False),
            ("tam 60 saniye", dict(stale, reset=x["reset"] - 60,
                                   data_time=self.now - 60), True),
            ("eski biçim", {"name": "Gemini Models", "remaining": 100}, False),
            ("bozuk", None, False),
        ]
        for name, prev, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(expected, m.weekly_not_started(x, prev))
        for change in ({"remaining": 99}, {"window": "5-hour"}, {"source": "Codex"}):
            self.assertFalse(m.weekly_not_started(dict(x, **change), stale))
        with patch.object(m, "run_agy", return_value="{}") as call:
            for _, prev, expected in cases:
                if not expected:
                    m.send_hello([x], data, [prev])
            m.send_hello([dict(x, remaining=99)], data, [stale])
            m.send_hello([x], data, [])
            call.assert_not_called()
            m.send_hello([x], data, [stale])
            call.assert_called_once()

    def test_model_selection_and_fallbacks(self):
        data = self.models(**{"gemini-4-hydrogen-low": "H", "gemini-3.8-flash-low": "X",
                              "other-flash-low": "Y", "argon-high": "Z", "argon-low": "A",
                              "sonnet-low": "S", "gpt-oss-high": "G"})
        self.assertEqual("gemini-4-hydrogen-low", m.hello_model("Gemini Models", data))
        del data["models"]["agy:gemini-4-hydrogen-low"]
        data["models"]["agy:gemini-4-hydrogen-high"] = "HH"
        self.assertEqual("gemini-4-hydrogen-high", m.hello_model("Gemini Models", data))
        del data["models"]["agy:gemini-4-hydrogen-high"]
        self.assertEqual("argon-low", m.hello_model("Gemini 4 Argon", data))
        self.assertEqual("gpt-oss-high", m.hello_model("Claude and GPT models", data))
        del data["models"]["agy:gpt-oss-high"]
        self.assertEqual("sonnet-low", m.hello_model("Claude and GPT models", data))
        del data["models"]["agy:gemini-3.8-flash-low"]
        self.assertEqual("other-flash-low", m.hello_model("Gemini Models", data))
        self.assertIsNone(m.hello_model("Bilinmeyen", data))
        data["successful"] = []
        self.assertIsNone(m.hello_model("Gemini Models", data))

    def test_hello_cooldown_command_state_and_log_sync(self):
        x = self.limit(source="Antigravity", name="Gemini Models", remaining=100,
                       reset=self.now + 7 * 86400)
        stale = dict(x, reset=x["reset"] - 600, data_time=self.now - 600)
        data = self.models(**{"gemini-4-hydrogen-low": "X"})
        with patch.object(m, "run_agy", return_value="{}") as call:
            m.send_hello([x, x], data, [stale])
            m.send_hello([x], data, [stale])
            call.assert_called_once_with(
                ["-p", "merhaba", "--model", "gemini-4-hydrogen-low",
                 "--output-format", "json", "--print-timeout", "60s"], 90)
            self.write_json(m.HELLO_STATE, {"Gemini Models": self.now - 21600})
            m.send_hello([x], data, [stale])
            self.assertEqual(2, call.call_count)
        self.assertEqual({"Gemini Models": self.now}, m.read_json(m.HELLO_STATE))
        self.assertEqual("sent", m.last_hello()["result"])
        self.assertEqual(2, len(m.HELLO_LOG.read_text().splitlines()))
        self.assertIn("Son otomatik mesaj: Gemini Models", m.render_text({}))
        self.assertNotIn("Son otomatik mesaj", m.menubar({}))
        self.assertEqual(m.last_hello(), m.json_data({})["last_hello"])

    def test_hello_corrupt_state_no_model_and_error(self):
        x = self.limit(source="Antigravity", name="Gemini Models", remaining=100,
                       reset=self.now + 7 * 86400)
        stale = dict(x, reset=x["reset"] - 600, data_time=self.now - 600)
        for state in ([], {"Gemini Models": None}, {"Gemini Models": "yanlis"}):
            self.write_json(m.HELLO_STATE, state)
            with patch.object(m, "run_agy", side_effect=RuntimeError) as call:
                m.send_hello([x], self.models(), [stale])
                call.assert_not_called()
                self.assertEqual("no model", m.last_hello()["result"])
                m.send_hello([x], self.models(**{"flash-low": "X"}), [stale])
                self.assertEqual("error: RuntimeError", m.last_hello()["result"])
        with m.HELLO_LOG.open("a") as log_file:
            log_file.write('\n{\n[]\n{"time":null}\n')
        self.assertEqual("error: RuntimeError", m.last_hello()["result"])

    def test_hello_only_fresh_collect_and_under_lock(self):
        x = self.limit(source="Antigravity", name="Gemini Models", remaining=100,
                       reset=self.now + 7 * 86400)
        self.fake_collect(agy={"limits": [x]}, models=self.models(**{"flash-low": "X"}))
        def check(*_):
            self.assertTrue(m.LOCK.exists())
            self.assertIsNotNone(m._lock)
        with patch.object(m, "send_hello", side_effect=check) as send:
            m.collect()
            send.assert_called_once()
            self.assertEqual([], send.call_args.args[2])
            prev = dict(x, reset=x["reset"] - 600, data_time=self.now - 600)
            self.write_json(m.CACHE, self.data(agy={"limits": [prev]}))
            m.collect()
            self.assertEqual([prev], send.call_args.args[2])
            m.agy_usage.return_value = {"error": "kesildi"}
            send.reset_mock()
            result = m.collect()
            send.assert_not_called()
            self.assertEqual("kesildi", result["agy"]["last_error"])
            m.agy_usage.return_value = {"limits": [x]}
            m.collect()
            self.assertEqual([], send.call_args.args[2])
            send.reset_mock()
            self.write_json(m.SETTINGS, {"auto_hello": False})
            m.collect()
            send.assert_not_called()
        self.write_json(m.SETTINGS, {"auto_hello": True})
        with patch.object(m, "send_hello", side_effect=OSError):
            self.assertIn("codex", m.collect())

    def test_table_durations_boundaries(self):
        for duration, text in ((59 * 60, "59 dk"), (3600, "1 saat 0 dk"),
                               (86340, "23 saat 59 dk"), (86400, "1 gün 0 saat")):
            self.assertEqual(text, m.format_duration(duration, True))
        self.assertEqual("0dk", m.format_duration(-1))

    def test_menubar_table_alignment_names_colors(self):
        claude = [self.limit(source="Claude", name=name, reset=self.now + duration,
                             data_time=self.now - age)
                  for name, duration, age in (("5-hour limit", 3540, 0),
                                              ("Weekly · all models", 3600, 0),
                                              ("Weekly · Sonnet", 86340, 25200),
                                              ("Weekly · Opus", -1, 0))]
        agy = [self.limit(source="Antigravity", name=name, reset=self.now + 172800)
               for name in ("Gemini Models", "Claude and GPT models", "Gemini 4 Argon", "Diğer")]
        data = self.data(self.limit(remaining=9, reset=self.now + 86400),
                         claude={"limits": claude, "saved_at": self.now,
                                 "data_time": self.now - 7200},
                         agy={"limits": agy, "data_time": self.now - 180},
                         codex={"limits": [self.limit(remaining=9, reset=self.now + 86400)],
                                "data_time": self.now - 720})
        lines = m.menubar(data).splitlines()
        table = lines[2:12]
        header = table[0].split(" | ")[0]
        dur_start, date_start = header.index("Kalan süre"), header.index("Yenilenme")
        pct_end = header.index("Kalan") + len("Kalan")
        self.assertEqual("AI | color=red", lines[0])
        self.assertIn("color=gray", table[0])
        durations = ["Kalan süre", "59 dk", "1 saat 0 dk", "23 saat 59 dk (eski kayıt)",
                     "yeni veri yok", "1 gün 0 saat"] + ["2 gün 0 saat"] * 4
        for line, duration in zip(table, durations):
            self.assertIn(" | font=Menlo size=12 trim=false", line)
            text = line.split(" | ")[0]
            self.assertEqual(duration, text[dur_start:date_start].rstrip())
            self.assertEqual(text[date_start:], text[date_start:].rstrip())
        for line in table[1:]:
            self.assertTrue(line[:pct_end].endswith(("%40", "%9", "-")))
        for name in ("5 saatlik", "Haftalık (Sonnet)", "Haftalık (Opus)", "Gemini haftalık",
                     "Claude/GPT haftalık", "Argon haftalık", "Diğer haftalık"):
            self.assertIn(name, "\n".join(table))
        self.assertIn("color=gray", table[3])
        self.assertIn("color=gray", table[4])
        self.assertNotIn("color=", table[5])
        measured = next(s for s in lines if s.startswith("Ölçüm:"))
        self.assertIn("Claude 2s 0dk önce · Codex 12dk önce · Antigravity 3dk önce", measured)
        self.assertNotIn("ölçüm", "\n".join(table))
        self.assertIn("param2=--refresh param3=--quiet", lines[-1])

    def test_menubar_hint_not_in_title_and_expired(self):
        x = self.limit(source="Claude", name="Weekly Sonnet", remaining=1,
                       data_time=self.now - 25200, reset=self.now + 172800)
        data = self.data(self.limit(reset=self.now + 7 * 86400),
                         claude={"saved_at": self.now - 25200, "limits": [x]},
                         agy={"error": "kesildi"})
        out = m.menubar(data)
        self.assertEqual("AI", out.splitlines()[0])
        self.assertIn("agy: kesildi | color=gray", out)
        self.assertIn("CLAUDE VERİSİ ESKİ", out)
        self.assertIn("Haftalık (Sonnet)", out)
        self.assertTrue(next(s for s in out.splitlines() if s.startswith(
            "CLAUDE VERİSİ ESKİ")).endswith(" | color=gray"))
        expired = self.data(self.limit(reset=self.now - 1, remaining=4))
        self.assertIn("sıfırlandı, yeni veri yok", m.render_text(expired))
        self.assertNotIn("(0dk)", m.render_text(expired))
        self.assertEqual("AI", m.menubar(expired).splitlines()[0])
        self.assertTrue(m.json_data(expired)["codex"]["limits"][0]["expired"])

    def test_atomic_write_error_preserves_old_record(self):
        self.write_json(m.CACHE, {"ozgun": 1})
        with patch.object(m.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.write_json(m.CACHE, {"new": 2})
        self.assertEqual({"ozgun": 1}, m.read_json(m.CACHE))
        self.assertEqual([m.CACHE], list(m.DATA.iterdir()))

    def test_deep_corrupt_cache_does_not_block_fast_mode(self):
        m.CACHE.parent.mkdir(parents=True, exist_ok=True)
        m.CACHE.write_text("[" * 2000 + "]" * 2000)
        self.assertIsNone(m.read_cache())
        self.fake_collect(codex={"limits": [self.limit(remaining=1)]})
        self.patch_value("refresh_background", Mock())
        # Fast mode does not read Codex sessions; a corrupt cache only requests background refresh.
        self.assertEqual((0, ""), self.cli("--kisa")[::2])
        m.refresh_background.assert_called_once()
        m.codex_usage.assert_not_called()
        m.agy_usage.assert_not_called()

    def test_codex_expired_percent_and_quiet_error(self):
        self.write_codex("a.jsonl", self.codex_line(resets_at=self.now - 1))
        result = m.codex_usage()
        self.assertEqual(100, result["limits"][0]["remaining"])
        self.assertFalse(m.warnings({"codex": result}))
        self.patch_value("prepare", Mock(side_effect=OSError))
        self.assertEqual((0, "", ""), self.cli("--sessiz"))
        self.assertEqual(1, self.cli("--json")[0])

    def test_agy_path_fallback_and_resolved_path(self):
        for found in (None, "/test/bin/agy"):
            with patch.object(m.shutil, "which", return_value=found) as which_mock:
                module = runpy.run_path(m.__file__, run_name="agy_path_test")
            which_mock.assert_called_once_with("agy")
            expected = found or next((p for p in ("/opt/homebrew/bin/agy", "/usr/local/bin/agy")
                                      if os.path.isfile(p) and os.access(p, os.X_OK)), None)
            self.assertEqual(expected, module["AGY"])
        self.patch_value("AGY", "/opt/homebrew/bin/agy")
        self.popen.side_effect = None
        self.popen.return_value = Mock(returncode=0)
        self.popen.return_value.communicate.return_value = ("yanit", "")
        m.run_agy(["models"], 30)
        self.assertEqual(["/opt/homebrew/bin/agy", "models"], self.popen.call_args.args[0])

    def test_refresh_line_plain_paths(self):
        line = m.menubar({}).splitlines()[-1]
        self.assertEqual(
            f'Yenile | bash="{m.sys.executable}" param1="{m.ROOT / "tidewatch.py"}" '
            "param2=--refresh param3=--quiet terminal=false refresh=true", line)

    def test_refresh_line_paths_with_spaces(self):
        self.patch_value("ROOT", self.root / "boşluklu proje")
        with patch.object(m.sys, "executable", "/test/Python App/python3"):
            self.assertEqual(
                f'Yenile | bash="/test/Python App/python3" '
                f'param1="{m.ROOT / "tidewatch.py"}" param2=--refresh '
                'param3=--quiet terminal=false refresh=true',
                m.menubar({}).splitlines()[-1])

    def test_claude_weekly_prefixed_label(self):
        self.assertEqual("Haftalık (Opus)", m.table_name(
            self.limit(source="Claude", name="Claude Weekly · Opus")))

    def test_cli_errors_explained(self):
        with patch.object(m.sys, "stdin", io.StringIO('{"windows": []}')):
            code, out, error = self.cli("--claude-yaz")
        self.assertEqual((1, ""), (code, out))
        self.assertIn("Claude verisi kaydedilemedi: Claude pencereleri boş/geçersiz", error)
        self.assertNotIn("ValueError", error)
        self.assertTrue(m.acquire_lock())
        code, out, error = self.cli("--kabul")
        self.assertEqual((1, ""), (code, out))
        self.assertIn("yenileme zaten sürüyor", error)
        self.assertNotIn("ValueError", error)
        self.assertEqual((1, "", ""), self.cli("--kabul", "--sessiz"))

    def test_normal_mode_locked_cache_age(self):
        self.fake_collect()
        self.write_json(m.CACHE, self.data(timestamp=self.now - 3600))
        self.assertTrue(m.acquire_lock())
        for args in ([], ["--yenile"]):
            code, out, error = self.cli(*args)
            self.assertEqual((0, ""), (code, error))
            self.assertIn("Veri: önbellek, 1s 0dk önce", out)
            self.assertIn("yenileme zaten sürüyor", out)
            self.assertNotIn("Veri: şimdi", out)
        m.agy_usage.assert_not_called()

    def test_codex_partial_utf8_last_line_keeps_previous(self):
        path = self.write_codex("a.jsonl", self.codex_line() + "\n")
        with path.open("ab") as file_obj:
            file_obj.write(b'{"rate_limits": "\xc4')
        self.assertIn("\ufffd", m.tail(path)[-1])
        self.assertEqual(75, m.codex_usage()["limits"][0]["remaining"])

    def test_new_model_measurement_age(self):
        for age in (0, 7200, 7201, 10800):
            data = self.data(timestamp=self.now - age,
                             models_status={"new": {"agy:x": "X"}})
            warning = m.warnings(data)[0]
            self.assertEqual(age > 7200, " (ölçüm " in warning)
            if age > 7200:
                self.assertTrue(warning.endswith(f" (ölçüm {m.format_duration(age)} önce)"))

    def test_text_warning_header_and_new_model(self):
        self.assertIn("\nUyarılar\nYok\n", m.render_text({}) + "\n")
        data = self.data(models_status={"new": {"agy:x": "X"}})
        out = m.render_text(data)
        self.assertIn("\nYeni: X (agy:x)\n", out)
        self.assertIn("\nUyarılar\nYENİ MODEL: X", out)

    def test_menu_title_colored_by_warning(self):
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
        self.patch_value("prepare", Mock(side_effect=ValueError("bozuk")))
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
                    if warning.startswith("AZ:"):
                        self.assertIn(warning, lines)
                    elif warning.startswith("KURTAR:"):
                        self.assertIn(warning + " | color=red", lines)
                    elif warning.startswith("GİDİŞAT:"):
                        self.assertIn(warning + " | color=orange", lines)
                    else:
                        self.assertIn(warning + " | color=#0A84FF", lines)

    def test_line_length(self):
        for path in (Path(m.__file__), Path(__file__)):
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                self.assertLessEqual(len(line), 100, f"{path.name}:{line_no}")

    def epoch(self, time_str):
        return m.iso_epoch("2026-11-13T" + time_str + "+06:00")

    def test_work_midnight_overlap_tz(self):
        self.patch_value("WORK_HOURS", [("22:00", "02:00"), ("23:00", "01:00"),
                                        ("01:00", "03:00")])
        start = self.epoch("00:00:00")
        end = self.epoch("23:30:00")
        self.assertEqual([(start, start + 3 * 3600), (end - 5400, end)],
                         m.work_intervals(start, end))
        self.assertEqual(4.5, m.total_hours(m.work_intervals(start, end)))
        self.assertEqual([], m.work_intervals(end, start))
        self.assertEqual([], m.work_intervals(start, start))
        self.patch_value("WORK_HOURS", [("19:00", "22:30")])
        utc = m.iso_epoch("2026-11-13T13:00:00Z")
        self.assertEqual([(utc, utc + 12600)], m.work_intervals(utc, utc + 20000))

    def test_capacity_normal_fast_claude_cap(self):
        start, end = self.epoch("19:00:00"), self.epoch("22:30:00")
        x = self.limit(source="Claude")
        self.assertEqual(10.5, m.capacity(x, start, end, "normal"))
        self.assertEqual(20, m.capacity(x, start, end, "fast"))
        self.assertEqual(60, m.capacity(x, start, end + 86400, "fast"))
        self.assertEqual(70, m.capacity(self.limit(), start, end, "fast"))
        self.patch_value("WORK_HOURS", [("10:00", "22:00")])
        self.assertEqual(60, m.capacity(x, self.epoch("10:00:00"),
                                        self.epoch("22:00:00"), "fast"))

    def test_levels_equality_horizon_low_and_target_missed(self):
        self.patch_value("NIGHT_HOURS", [])
        self.patch_value("FAST_RATE", dict(m.FAST_RATE, Codex=8))
        now = self.epoch("19:00:00")
        x = self.limit(reset=self.epoch("22:30:00") + 86400, data_time=now)
        for remaining, expected in ((10, ""), (10.5, ""), (11, "pace"),
                                    (28, "pace"), (29, "rescue"), (9, "")):
            self.assertEqual(expected, m.level(dict(x, remaining=remaining), now))
        target, _ = m.target_and_last(x)
        self.assertEqual("rescue", m.level(x, target))
        msg = m.warnings(self.data(x), target)[0]
        for part in ("hedef", "kaçtı", "akşam 3,5 sa + gece 0,0 sa", "hızlı ~%28,0"):
            self.assertIn(part, msg)
        self.assertEqual("", m.level(dict(x, reset=now), now))
        self.assertEqual("", m.level(dict(x, window="5-hour"), now))
        self.assertEqual("", m.level(dict(x, reset=now + 6 * 86400 + 1), now))
        self.patch_value("FAST_RATE", {"Codex": 0})
        self.assertEqual("rescue", m.level(dict(x, reset=now + 6 * 86400), now))

    def test_no_subscription_and_new_measurement(self):
        self.patch_value("SUBSCRIPTION_END", {"Codex": "2026-11-09T13:00:00+06:00"})
        sub_end = m.iso_epoch(m.SUBSCRIPTION_END["Codex"])
        x = self.limit(data_time=sub_end)
        self.assertEqual([], m.warnings(self.data(x)))
        line = next(s for s in m.menubar(self.data(x)).splitlines()
                    if "abonelik yok (gizli)" in s)
        self.assertIn("abonelik yok", line)
        self.assertIn("color=gray", line)
        self.assertIn("abonelik yok", m.render_text(self.data(x)))
        x["data_time"] = sub_end + 1
        x["plan"] = "plus"
        self.assertFalse(m.no_subscription(x, self.now))
        self.assertEqual(x["reset"] - 86400, m.target_and_last(x)[0])
        self.assertTrue(m.warnings(self.data(x)))
        x["data_time"] = sub_end - 1
        self.assertEqual(sub_end - 86400, m.target_and_last(x)[0])

    def test_per_evening_six_columns(self):
        now = self.epoch("19:00:00")
        m.time.time.return_value = now
        x = self.limit(remaining=28, reset=self.epoch("22:30:00") + 2 * 86400,
                       data_time=now)
        self.assertEqual("%14", m.per_evening(x, now))
        self.assertEqual("kaçtı", m.per_evening(x, x["reset"]))
        self.assertEqual("-", m.per_evening(dict(x, window="5-hour"), now))
        table = m.menubar(self.data(x)).splitlines()[2:4]
        header, line = [s.partition(" | ")[0] for s in table]
        self.assertEqual("%14", line[header.index("Akşam başına"):])
        self.assertEqual("15.11 22:30",
                         line[header.index("Yenilenme"):header.index("Akşam başına")].strip())

    def test_backlog_first_line_gray_except_short(self):
        self.patch_value("prepare", Mock(return_value=(self.data(self.limit()), True)))
        data = self.data(self.limit())
        before = m.warnings(data)
        m.BACKLOG_FILE.write_text("- [Codex] ilk iş\n- [Codex] ikinci iş\n- [Claude] diğer\n")
        self.assertEqual(["Bekleyen iş (Codex): ilk iş"], m.backlog_lines(data))
        self.assertEqual(before, m.warnings(data))
        self.assertIn("Bekleyen iş (Codex): ilk iş | color=gray", m.menubar(data))
        self.assertIn("Bekleyen iş (Codex): ilk iş", m.render_text(data))
        self.assertNotIn("Bekleyen iş (", self.cli("--kisa")[1])
        self.assertIn("Bekleyen işler: 3 madde", self.cli("--kisa")[1])
        self.assertEqual([], m.backlog_lines(self.data(self.limit(remaining=1))))
        with patch.object(Path, "read_text", side_effect=OSError):
            self.assertEqual([], m.backlog_lines(data))

    def history(self):
        return list(m.history_records())

    def test_history_change_six_hours_and_tail(self):
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
        self.assertEqual(self.now, self.history()[-1]["observed_at"])
        self.assertEqual(self.now + 21602, self.history()[-1]["recorded_at"])

    def test_history_archive_immutable_same_second_collision(self):
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

    def test_history_collect_claude_lock_and_error_isolation(self):
        self.fake_collect(codex={"limits": [self.limit()]})
        m.collect()
        self.assertEqual(1, len(self.history()))
        raw = json.dumps({"windows": [dict(label="Weekly", percentUsed=40,
                                          resetsAt="2026-11-15T00:00:00+06:00")]})
        write_hist = m.write_history
        def check(*args):
            self.assertIsNotNone(m._lock)
            return write_hist(*args)
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

    def test_history_not_on_fast_path(self):
        self.fake_collect()
        self.patch_value("refresh_background", Mock())
        with patch.object(m, "history_records", side_effect=AssertionError), \
                patch.object(m, "write_history", side_effect=AssertionError), \
                patch.object(m, "backfill_history", side_effect=AssertionError), \
                patch.object(m, "usage_pattern", side_effect=AssertionError):
            for mode in ("--kisa", "--menubar"):
                for extra in ("--gecmis-doldur", "--aliskanlik"):
                    self.assertEqual(0, self.cli(mode, extra)[0])
        self.assertFalse(m.HISTORY.exists())

    def test_backfill_history_raw_percent_duplicate_archive(self):
        raw = json.loads(self.codex_line(used_percent=28.25, resets_at=self.now - 1))
        raw["timestamp"] = "2026-11-01T10:00:00Z"
        raw["payload"]["rate_limits"]["secondary"] = dict(
            used_percent=50, resets_at=self.now - 5, window_minutes=300)
        self.write_codex("a.jsonl", json.dumps(raw) + "\n" + json.dumps(raw))
        self.write_codex("b.jsonl", json.dumps(raw) + '\n{"rate_limits": null}\nbozuk')
        self.patch_value("HISTORY_LIMIT", 1)
        self.assertIn("2 satır", self.cli("--gecmis-doldur")[1])
        records = self.history()
        weekly = next(x for x in records if x["window"] == "weekly")
        self.assertEqual(71.75, weekly["remaining"])
        self.assertEqual(m.iso_epoch(raw["timestamp"]), weekly["observed_at"])
        self.assertIn("0 satır", self.cli("--gecmis-doldur")[1])
        self.assertEqual(records, self.history())
        self.assertIsNone(m._lock)

    def test_usage_pattern_pair_filtering_and_hourly_dist(self):
        start = self.epoch("16:00:00")
        reset = start + 7 * 86400
        rows = []
        # 100->80 valid (3h); increase and reset diff 600 invalid;
        # last 5-point drop is 1h, so it is excluded from the fastest rate calculation.
        for hour, remaining, diff in ((0, 100, 0), (3, 80, 0), (4, 90, 0),
                                      (5, 70, 600), (6, 65, 600)):
            rows.append(dict(source="Codex", name="Codex", window="weekly",
                             remaining=remaining, reset=reset + diff,
                             observed_at=start + hour * 3600, recorded_at=self.now))
        self.assertTrue(m.acquire_lock())
        m.append_history(rows + [rows[-1]])
        m.release_lock()
        report = self.cli("--aliskanlik")[1]
        self.assertIn("toplam %25,0 tüketim, 2 çift", report)
        self.assertIn("19:00–19:59: %80,0 pay, toplam %20,0", report)
        self.assertIn("19:00–22:30 payı: %100,0", report)
        self.assertIn("~%6,7/saat", report)
        self.assertEqual([("19:00", "22:30")], m.WORK_HOURS)

    def test_night_start_boundary_and_started_night(self):
        night = self.epoch("22:30:00")
        last = self.epoch("04:00:00") + 86400
        x = self.limit()
        for diff in (-1, 0, 1):
            start = night + diff
            normal = m.capacity(x, start, last, "normal")
            self.assertAlmostEqual(5.5 + 3 / 3600 if diff == -1 else 0, normal)
            self.assertAlmostEqual((last - start) / 3600 * 20,
                                   m.capacity(x, start, last, "fast"))
            self.assertEqual(bool(diff == -1), bool(m.work_intervals(start, last, night=True)))
        self.assertEqual([], m.night_intervals(last, last, "fast"))

    def test_night_claude_combined_cap_and_separate_normal_rate(self):
        start = self.epoch("19:00:00")
        last = self.epoch("04:00:00") + 86400
        x = self.limit(source="Claude")
        self.assertEqual(16, m.capacity(x, start, last, "normal"))
        self.assertEqual(40, m.capacity(x, start, last, "fast"))
        self.assertEqual(180, m.capacity(self.limit(), start, last, "fast"))
        # Applying separate caps would have yielded 20 + 33 = 53.
        self.assertEqual(3.5, m.total_hours(m.work_intervals(start, last)))

    def test_outlook_fresh_week_and_near_end(self):
        x = self.limit(remaining=98, reset=self.now + 7 * 86400)
        result = m.outlook(self.data(x))[0]
        self.assertEqual({"source", "name", "remaining", "reset", "last", "unused",
                          "daily", "stale", "data_time"},
                         set(result))
        self.assertLess(result["unused"], 10)
        self.assertEqual(14, result["daily"])
        self.assertEqual(x["reset"], result["last"])
        self.assertFalse(result["stale"])
        self.assertIn("günde ~%14", m.facts_line(self.data(x)))
        start = self.epoch("23:00:00")
        x = dict(x, remaining=28, reset=start + 11 * 3600)
        result = m.outlook(self.data(x), start)[0]
        self.assertGreater(result["unused"], 20)
        self.assertEqual(28, result["daily"])
        self.assertIn("11s 0dk kaldı, ~%28 yanar", m.facts_line(self.data(x), start))
        self.assertNotIn("günde", m.facts_line(self.data(x), start))

    def test_outlook_stale_sub_limit_and_past_end(self):
        x = self.limit(reset=self.now + 3 * 86400)
        c = dict(x, source="Claude", name="Weekly", remaining=42,
                 data_time=self.now - 21601)
        result = m.outlook(self.data(c))[0]
        self.assertTrue(result["stale"])
        self.assertIsNone(result["unused"])
        self.assertIsNone(result["daily"])
        self.assertTrue(m.facts_line(self.data(c)).endswith("Claude %42 (veri eski)"))
        self.assertFalse(m.outlook(self.data(dict(c, data_time=self.now - 21600)))[0]["stale"])
        self.assertEqual([], m.outlook(self.data(dict(c, name="Weekly · Fable"))))
        self.assertEqual([], m.outlook(self.data(dict(x, window="5-hour"))))
        sub_end = self.now + 3600
        self.patch_value("SUBSCRIPTION_END",
                         {"Codex": m.datetime.fromtimestamp(sub_end, m.TZ).isoformat()})
        self.assertEqual(sub_end, m.outlook(self.data(x))[0]["last"])
        self.assertEqual(x["reset"], m.outlook(self.data(x))[0]["reset"])
        self.assertEqual([], m.outlook(self.data(dict(x, reset=self.now - 1))))

    def test_facts_line_order_no_waste_and_daily_limit(self):
        x = self.limit(remaining=40, reset=self.now + 3 * 86400)
        g = dict(x, source="Antigravity", name="Gemini Models", reset=self.now + 3600)
        a = dict(g, name="Gemini 4 Argon", reset=self.now + 7200)
        c = dict(x, source="Claude", name="Weekly", reset=self.now + 86400)
        line = m.facts_line(self.data(x, c, a, g))
        self.assertLess(line.index("Gemini %"), line.index("Argon %"))
        self.assertLess(line.index("Argon %"), line.index("Claude %"))
        self.assertLess(line.index("Claude %"), line.index("Codex %"))
        for unused in (0, 0.49, 0.5):
            with patch.object(m, "capacity", return_value=40 - unused):
                line = m.facts_line(self.data(x))
                self.assertIn("yanmaz", line)
                self.assertNotIn("~%0", line)
        with patch.object(m, "capacity", return_value=40):
            self.assertTrue(m.facts_line(self.data(x)))
        self.assertNotIn("günde", m.facts_line(self.data(dict(x, reset=self.now + 2 * 86400))))
        self.assertEqual("", m.facts_line(self.data()))

    def test_guide_lines_backlog_count_and_roster(self):
        self.assertEqual([], m.guide_lines())
        m.BACKLOG_FILE.write_text("# İşler\n  - girintili sayılmaz\n")
        self.assertEqual([f"Bekleyen işler: boş, {m.BACKLOG_FILE}"], m.guide_lines())
        m.BACKLOG_FILE.write_text("# İşler\n- ilk\n- ikinci\nmetin\n")
        m.ROSTER_FILE.write_text("# Kadro\n")
        expected = [f"Kadro: {m.ROSTER_FILE}",
                    f"Bekleyen işler: 2 madde, {m.BACKLOG_FILE}"]
        self.assertEqual(expected, m.guide_lines())
        data = self.data(self.limit())
        self.patch_value("prepare", Mock(return_value=(data, True)))
        for out in (self.cli("--kisa")[1], m.render_text(data)):
            self.assertIn("\n".join(expected), out)
            self.assertLess(out.index("LİMİTLER"), out.index("Kadro:"))
        self.assertNotIn("Kadro:", m.menubar(data))
        self.assertNotIn("Bekleyen işler:", m.menubar(data))

    def test_outlook_hook_limit_json_and_menu(self):
        x = self.limit()
        data = self.data(*(dict(x, name=str(i), remaining=1) for i in range(5)))
        self.patch_value("prepare", Mock(return_value=(data, True)))
        out = self.cli("--kisa")[1]
        self.assertEqual(3, out.count("AZ:"))
        self.assertIn("(+2 uyarı", out)
        self.assertEqual(1, out.count("LİMİTLER"))
        self.assertLess(out.index("LİMİTLER"), out.index("CLAUDE VERİSİ ESKİ:"))
        menu = m.menubar(self.data(x, dict(x, window="5-hour")))
        self.assertIn("~%40", menu)
        self.assertIn("~% : bu gidişle yanacak pay (tahmin)", menu)
        self.assertEqual("-", menu.splitlines()[4].partition(" | ")[0].split()[-1])
        out = m.render_text(data)
        self.assertLess(out.index("LİMİTLER"), out.index("Uyarılar"))
        self.assertEqual(1, json.loads(self.cli("--json")[1])["outlook"][0]["unused"])
        json.dumps(m.json_data(data), allow_nan=False)
        with patch.object(m, "capacity", return_value=100):
            self.assertNotIn("~%40", m.menubar(self.data(x)))
        c = dict(x, source="Claude", name="Weekly", data_time=self.now - 21601)
        self.assertNotIn("~%40", m.menubar(self.data(c)))

    def test_outlook_past_reset_and_last_filtered(self):
        for diff in (-1, 0):
            with self.subTest(reset_diff=diff):
                data = self.data(self.limit(reset=self.now + diff))
                self.assertEqual([], m.outlook(data))
                self.assertEqual("", m.facts_line(data))
        # The last boundary must also be applied regardless of subscription visibility.
        with patch.object(m, "target_and_last", return_value=(self.now - 86400, self.now)):
            self.assertEqual([], m.outlook(self.data(self.limit())))
        self.assertEqual(1, len(m.outlook(self.data(self.limit(reset=self.now + 1)))))

    def test_rounded_zero_and_daily_budget(self):
        x = self.limit(remaining=1, reset=self.now + 4 * 86400)
        for unused, expected in ((0.49, "yanmaz"), (0.5, "yanmaz"), (0.51, "~%1 yanar")):
            with self.subTest(unused=unused), patch.object(m, "capacity", return_value=1 - unused):
                line = m.facts_line(self.data(x))
                self.assertIn(expected, line)
                self.assertNotIn("~%0", line)
                self.assertNotIn("günde", line)
                self.assertNotIn("~%0", m.menubar(self.data(x)))
        self.assertIn("günde ~%1", m.facts_line(self.data(dict(x, remaining=4))))

    def test_facts_measurement_age_threshold_and_sources(self):
        for source in ("Codex", "Antigravity"):
            for age in (7200, 7201, 10800):
                with self.subTest(source=source, age=age):
                    x = self.limit(source=source, data_time=self.now - age)
                    line = m.facts_line(self.data(x))
                    warning = " ".join(m.warnings(self.data(x)))
                    self.assertEqual(age > m.STALE_MEASUREMENT_SECONDS, "(ölçüm " in line)
                    self.assertEqual(age > m.STALE_MEASUREMENT_SECONDS, "(ölçüm " in warning)
                    if age > m.STALE_MEASUREMENT_SECONDS:
                        self.assertIn(f"(ölçüm {m.format_duration(age)} önce)", line)
        for age in (21600, 21601):
            x = self.limit(source="Claude", data_time=self.now - age)
            line = m.facts_line(self.data(x))
            self.assertNotIn("(ölçüm ", line)
            self.assertEqual(age > 21600, "(veri eski)" in line)

    def test_finished_backlog_items_not_counted_or_suggested(self):
        data = self.data(self.limit(), self.limit(source="Claude", name="Weekly"))
        m.BACKLOG_FILE.write_text(
            "# İşler\n- [Claude] aktif\n## Bitenler\n- [Codex] bitti\n"
            "## Başka\n- [Claude] yine biten\n")
        self.assertIn("1 madde", m.guide_lines()[0])
        self.assertEqual(["Bekleyen iş (Claude): aktif"], m.backlog_lines(data))
        m.BACKLOG_FILE.write_text("## Bitenler\n- [Codex] bitti\n")
        self.assertIn("boş", m.guide_lines()[0])
        self.assertEqual([], m.backlog_lines(data))

    def test_new_model_guidance(self):
        for new in ({"agy:a": "A"}, {"agy:a": "A", "agy:b": "B"}):
            data = self.data(models_status={"new": new})
            for line in m.warnings(data) + m.short_warnings(m.warnings(data)):
                self.assertIn("kullandığın modelleri gözden geçir, "
                              "sonra tidewatch.py --accept-models", line)

    def test_plan_read_cache_and_error_protection(self):
        raw = json.loads(self.codex_line())
        # In the test fixture, rate_limits may be inside an inner payload.
        def add_plan(obj):
            if isinstance(obj, dict):
                if isinstance(obj.get("rate_limits"), dict):
                    obj["rate_limits"]["plan_type"] = "plus"
                for child in obj.values():
                    add_plan(child)
        add_plan(raw)
        section = m.codex_record(raw, self.now)
        self.assertTrue(all(x["plan"] == "plus" for x in section["limits"]))
        self.write_json(m.CACHE, self.data(codex=section))
        stale = m.read_cache()["codex"]
        self.assertEqual(section, stale)
        self.assertEqual("plus", m.keep_last_good({"error": "yok"}, stale)["limits"][0]["plan"])
        self.assertTrue(all(x["plan"] is None for x in
                            m.codex_record(json.loads(self.codex_line()), self.now)["limits"]))

    def test_plan_hiding_equality_transitions_and_display(self):
        sub_end = self.now
        sub_end_iso = m.datetime.fromtimestamp(sub_end, m.TZ).isoformat()
        self.patch_value("SUBSCRIPTION_END", {"Codex": sub_end_iso})
        for plan in ("free", "plus", None, "yeni-plan"):
            x = self.limit(plan=plan)
            self.assertFalse(m.no_subscription(x, sub_end - 1))
            self.assertTrue(m.no_subscription(x, sub_end))
            new = dict(x, data_time=sub_end + 1)
            self.assertEqual(plan != "plus", m.no_subscription(new, sub_end + 2))
        for plan, visible in (("free", False), ("plus", True), (None, False)):
            x = self.limit(plan=plan, data_time=sub_end + 1)
            data = self.data(x, codex={"limits": [x], "data_time": sub_end + 1})
            self.assertEqual(visible, bool(m.visible_limits(data, sub_end + 2)))
        x = self.limit(plan="free")
        data = self.data(x, codex={"limits": [x], "data_time": sub_end})
        self.assertNotIn("Codex:", m.render_text(data).split("Modeller:")[0])
        self.assertNotIn("Ölçüm: Codex", m.menubar(data))
        self.assertIn("Codex: abonelik yok (gizli) | color=gray", m.menubar(data))
        self.assertEqual([], m.warnings(data))
        self.assertEqual([], m.outlook(data))
        self.assertEqual("", m.facts_line(data))
        self.assertTrue(m.acquire_lock())
        m.write_history(m.all_limits(data), self.now)
        self.assertEqual(1, len(list(m.history_records())))
        self.patch_value("SUBSCRIPTION_END", {})
        for plan, hidden in (("free", True), ("plus", False), (None, False), ("yeni-plan", False)):
            self.assertEqual(hidden, m.no_subscription(dict(x, plan=plan), sub_end))

    def test_fast_mode_does_not_read_codex_sessions(self):
        self.write_json(m.CACHE, self.data(self.limit(remaining=1)))
        self.patch_value("refresh_background", Mock())
        self.patch_value("codex_usage", Mock(return_value={"limits": [self.limit(remaining=2)]}))
        for mode in ("--kisa", "--menubar"):
            for extra in ([], ["--yenile"]):
                with self.subTest(mode=mode, extra=extra):
                    code, out, _ = self.cli(mode, *extra)
                    self.assertEqual(0, code)
                    self.assertIn("%1", out)
        m.codex_usage.assert_not_called()
        self.assertIn("%2", self.cli()[1])
        m.codex_usage.assert_called_once()

    def test_later_baselined_source_warns_until_accepted(self):
        self.fake_collect(models=self.models("codex", a="A"))
        m.collect()
        m.models.return_value = {"successful": ["codex", "agy"], "errors": {},
                                 "models": {"codex:a": "A", "agy:x": "X"}}
        m.collect()
        m.collect()
        data = m.read_cache()
        line = next(s for s in m.warnings(data) if s.startswith("YENİ KAYNAK: agy"))
        self.assertIn("(1 model)", line)
        self.assertIn("tidewatch.py --accept-models", line)
        self.assertIn(line + " | color=#0A84FF", m.menubar(data))
        self.assertEqual("AI | color=#0A84FF", m.menubar(data).splitlines()[0])
        with patch.object(m, "prepare", Mock(return_value=(data, True))):
            self.assertIn("YENİ KAYNAK: agy", self.cli("--kisa")[1])
        self.assertEqual(0, self.cli("--kabul")[0])
        self.assertNotIn("YENİ KAYNAK", str(m.warnings(m.read_cache())))
        m.collect()
        self.assertNotIn("YENİ KAYNAK", str(m.warnings(m.read_cache())))
        self.assertEqual({"codex:a": "A", "agy:x": "X"}, m.read_json(m.MODELS_FILE)["models"])

    def test_codex_model_file_missing_or_empty_fails_source(self):
        self.write_json(m.MODELS_FILE, {"models": {"codex:x": "x", "codex:y": "Y"}})
        before = m.MODELS_FILE.read_bytes()
        self.write_json(m.HOME / ".codex/models_cache.json", {"models": [{"slug": "x"}]})
        desktop = m.HOME / ".codex/models_cache.codex_work_desktop.json"
        for content in (None, {"models": []}):
            with self.subTest(content=content):
                if content is not None:
                    self.write_json(desktop, content)
                with self.assertRaises(Exception):
                    m.codex_models()
                self.assertIn("codex", m.models()["errors"])
                self.assertEqual(1, self.cli("--kabul")[0])
                self.assertEqual(before, m.MODELS_FILE.read_bytes())

    def test_partial_accept_preserves_failed_source_notice(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:old": "Old", "codex:x": "X"}})
        self.write_json(m.CACHE, self.data(self.limit(), models_status={
            "new": {"agy:new": "New", "codex:y": "Y"}, "removed": {"agy:old": "Old"}}))
        data = self.models("codex", x="X", y="Y")
        data["errors"] = {"agy": "error"}
        self.patch_value("models", Mock(return_value=data))
        self.assertEqual(0, self.cli("--kabul")[0])
        status = m.read_cache()["models_status"]
        self.assertEqual({"agy:new": "New"}, status["new"])
        self.assertEqual({"agy:old": "Old"}, status["removed"])
        self.assertTrue(m.warnings(m.read_cache())[0].startswith("YENİ MODEL: New (agy:new)"))
        self.assertIn("codex:y", m.read_json(m.MODELS_FILE)["models"])

    def test_collect_reads_stale_cache_under_lock(self):
        def agy(remaining, age):
            return {"limits": [self.limit(source="Antigravity", name="Gemini", remaining=remaining,
                                          data_time=self.now - age)],
                    "data_time": self.now - age}
        self.write_json(m.CACHE, self.data(agy=agy(80, 600)))
        new = agy(50, 60)
        acquire = m.acquire_lock
        def interleave():
            # Another refresh finished between the stale read and acquiring the lock.
            self.write_json(m.CACHE, self.data(agy=new))
            return acquire()
        self.patch_value("acquire_lock", interleave)
        self.fake_collect()
        result = m.collect()
        self.assertEqual(new["limits"], result["agy"]["limits"])
        self.assertEqual(new["limits"], m.read_cache()["agy"]["limits"])
        self.assertIsNone(m._lock)
        # If reading under the lock raises an error, the lock is still released.
        with patch.object(m, "read_cache", side_effect=OSError("okuma")):
            with self.assertRaises(OSError):
                m.collect()
        self.assertIsNone(m._lock)

    def test_partial_accept_preserves_failed_source_models(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:a": "A", "codex:x": "X"}})
        self.write_json(m.CACHE, self.data(self.limit(), model_sources={
            "models": {"agy:a": "A", "agy:b": "B", "codex:x": "X"}, "errors": {}}))
        data = self.models("codex", x="X", y="Y")
        data["errors"] = {"agy": "error"}
        self.patch_value("models", Mock(return_value=data))
        self.assertEqual(0, self.cli("--kabul")[0])
        sources = m.read_cache()["model_sources"]
        self.assertEqual({"agy:a": "A", "agy:b": "B", "codex:x": "X", "codex:y": "Y"},
                         sources["models"])
        self.assertEqual({"agy": "error"}, sources["errors"])
        self.assertIn("Modeller: 4", m.render_text(m.read_cache()))

    def test_corrupt_sub_timestamp_keeps_valid_rate_limits(self):
        raw = json.loads(self.codex_line(used_percent=30))
        raw["timestamp"] = "2026-11-01T10:00:00Z"
        raw["payload"]["timestamp"] = None
        self.write_codex("a.jsonl", json.dumps(raw))
        data = m.codex_usage()
        self.assertEqual(70, data["limits"][0]["remaining"])
        self.assertEqual(m.iso_epoch(raw["timestamp"]), data["data_time"])
        self.assertIn("1 satır", self.cli("--gecmis-doldur")[1])

    def test_backfill_history_adds_missing_window_at_same_time(self):
        raw = json.loads(self.codex_line(used_percent=20))
        raw["timestamp"] = "2026-11-01T10:00:00Z"
        raw["payload"]["rate_limits"]["secondary"] = dict(
            used_percent=50, resets_at=self.now + 60, window_minutes=300)
        self.write_codex("a.jsonl", json.dumps(raw))
        t = m.iso_epoch(raw["timestamp"])
        self.assertTrue(m.acquire_lock())
        m.append_history([dict(source="Codex", name="Codex", window="5-hour", remaining=50,
                            reset=self.now + 60, observed_at=t, recorded_at=self.now)])
        m.release_lock()
        self.assertIn("1 satır", self.cli("--gecmis-doldur")[1])
        self.assertEqual({"5-hour", "weekly"}, {x["window"] for x in self.history()})

    def test_collect_carries_failed_model_source_notice(self):
        self.write_json(m.MODELS_FILE, {"models": {"agy:a": "A", "codex:x": "X"}})
        self.write_json(m.CACHE, self.data(self.limit(), models_status={
            "new": {"agy:b": "B", "codex:y": "Y"}, "removed": {"agy:a": "A"}}))
        data = self.models("codex", x="X")
        data["errors"] = {"agy": "error"}
        self.fake_collect(models=data)
        status = m.collect()["models_status"]
        self.assertEqual({"agy:b": "B"}, status["new"])
        self.assertEqual({"agy:a": "A"}, status["removed"])


class ExtraAuditTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("MESSAGES", dict(m.ENGLISH)), ("MESSAGE_OVERRIDES", {})):
            patcher = patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        m.load_messages("tr")

    def test_short_order_and_sub_limit(self):
        warnings = ["YENİ MODEL: A (agy:a) — x", "YENİ MODEL: B (agy:b) — x",
                    "AZ: z", "GİDİŞAT: g", "KURTAR: k"]
        result = m.short_warnings(warnings)
        self.assertTrue(result[0].startswith("YENİ MODEL (2): A (agy:a), B (agy:b)"))
        self.assertEqual(["KURTAR: k", "GİDİŞAT: g", "AZ: z"], result[1:])
        self.assertTrue(m.sub_limit({"source": "Claude", "name": "Weekly · Fable"}))
        self.assertFalse(m.sub_limit({"source": "Claude", "name": "Weekly · all models"}))
        self.assertFalse(m.sub_limit({"source": "Antigravity", "name": "Gemini · x"}))


if __name__ == "__main__":
    unittest.main()
