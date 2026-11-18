"""Language files and language selection."""
import contextlib
import io
import json
import os
import string
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tidewatch as m

LANGUAGES = ("ar", "de", "es", "fr", "ja", "ko", "pt", "ru", "tr", "zh")


def fields(value):
    if isinstance(value, list):
        return sorted(f for item in value for f in fields(item))
    if not isinstance(value, str):
        return [type(value).__name__]
    return sorted(f[1] for f in string.Formatter().parse(value) if f[1] is not None)


class LocaleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patchers = [patch.object(m, "MESSAGES", dict(m.ENGLISH)),
                    patch.object(m, "MESSAGE_OVERRIDES", {}),
                    patch.object(m, "LANGUAGE", None),
                    patch.object(m, "TZ", m.timezone(m.timedelta(hours=0))),
                    patch.object(m, "SUBSCRIPTION_END", {}),
                    patch.object(m, "BACKLOG_FILE", None),
                    patch.object(m, "ROSTER_FILE", None),
                    patch.object(m, "SETTINGS", self.root / "data" / "settings.json"),
                    patch.object(m, "USER_SETTINGS", self.root / "config" / "settings.json"),
                    patch.object(m, "HELLO_LOG", self.root / "data" / "hello-log.jsonl"),
                    patch.object(m, "HELLO_STATE", self.root / "data" / "hello.json"),
                    patch.dict(os.environ, {}),
                    patch.object(m.time, "time", return_value=1794211200.0)]
        for name in ("DATA", "CACHE", "CLAUDE_CACHE", "MODELS_FILE", "LOCK", "HISTORY",
                     "HISTORY_ARCHIVE", "CONFIG", "WORK_HOURS", "NIGHT_HOURS", "HELLO_PROMPT",
                     "SHORT_SUMMARY", "CLAUDE_MISSING_IS_STALE", "STRICT_CODEX_MODEL_FILES"):
            patchers.append(patch.object(m, name, getattr(m, name)))
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("TIDEWATCH_CONFIG", "TIDEWATCH_LANG", "LC_ALL", "LC_MESSAGES", "LANG"):
            os.environ.pop(name, None)

    def test_every_language_file_matches_english(self):
        self.assertEqual(sorted(LANGUAGES), [x for x in m.available_languages() if x != "en"])
        for code in LANGUAGES:
            translated = json.loads((m.LOCALES / f"{code}.json").read_text(encoding="utf-8"))
            with self.subTest(language=code):
                self.assertEqual(list(m.ENGLISH), list(translated))
                for key, value in m.ENGLISH.items():
                    if value is not None:
                        self.assertEqual(fields(value), fields(translated[key]), key)
                self.assertTrue(translated["backlog_done_heading"].startswith("## "))
                for key in ("warn_new_model", "warn_new_source", "claude_stale"):
                    self.assertIn("tidewatch.py --", translated[key])

    def test_every_language_renders_all_outputs(self):
        now = 1794211200.0
        limits = [dict(source="Codex", name="Codex", window="weekly", remaining=r,
                       reset=now + h * 3600, data_time=now - 10800, plan=None)
                  for r, h in ((9, 1), (80, 40), (99, 100))]
        data = dict(version=2, timestamp=now, codex={"limits": limits, "data_time": now},
                    models_status={"new": {"agy:x": "X"}, "removed": {"agy:y": "Y"}})
        for code in ("en",) + LANGUAGES:
            m.load_messages(code)
            with self.subTest(language=code):
                text = m.render_text(data)
                menu = m.menubar(data)
                self.assertIn("Codex", text)
                self.assertGreaterEqual(len(m.warnings(data)), 3)
                self.assertTrue(m.facts_line(data))
                self.assertIn("|", menu)
                m.format_duration(90061, True)
                m.format_date(now)

    def test_language_comes_from_settings_then_environment_then_system(self):
        config = self.root / "settings.json"
        config.write_text(json.dumps({"language": "de"}))
        data = {"version": 2, "timestamp": 1794211200.0, "codex": {"limits": [dict(
            source="Codex", name="Codex", window="weekly", remaining=9,
            reset=1794211200.0 + 3600, data_time=1794211200.0, plan=None)]}}

        def short(*args):
            with patch.object(m, "prepare", return_value=(data, True)), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                m.main(["--short"] + list(args))
            return output.getvalue()

        with patch.object(m, "system_language", return_value="fr"):
            french = json.loads((m.LOCALES / "fr.json").read_text(encoding="utf-8"))
            self.assertTrue(short().startswith(french["label_low"] + ": Codex"))
            self.assertIn("NIEDRIG:", short("--config", str(config)))
            # A real process reads its settings once.
            m.LANGUAGE, m.SETTINGS = None, self.root / "data" / "settings.json"
            os.environ["TIDEWATCH_LANG"] = "tr"
            self.assertIn("AZ:", short())
            os.environ["TIDEWATCH_LANG"] = "xx"
            self.assertIn("LOW:", short())
        with patch.object(m, "system_language", return_value="en"):
            os.environ.pop("TIDEWATCH_LANG")
            self.assertIn("LOW:", short())

    def test_overrides_apply_on_top_of_the_language(self):
        m.apply_settings({"language": "tr", "messages": {"label_low": "AZALDI"}}, self.root)
        self.assertEqual("AZALDI", m.MESSAGES["label_low"])
        self.assertEqual("KURTAR", m.MESSAGES["label_rescue"])
        m.load_messages("de")
        self.assertEqual("AZALDI", m.MESSAGES["label_low"])
        for bad in ({"language": 5}, {"messages": {"no_such_key": "x"}}):
            with self.assertRaises(ValueError):
                m.apply_settings(bad, self.root)

    def test_language_codes_and_system_language(self):
        for value, code in (("tr_TR.UTF-8", "tr"), ("pt-BR", "pt"), ("zh-Hans", "zh"),
                            ("de_DE@euro", "de"), ("EN", "en")):
            self.assertEqual(code, m.language_code(value))
        os.environ["LANG"] = "ja_JP.UTF-8"
        self.assertEqual("ja", m.system_language())
        os.environ["LANG"] = "C.UTF-8"
        with patch.object(m, "HOME", self.root), patch.object(m.sys, "platform", "darwin"):
            self.assertEqual("en", m.system_language())
            preferences = self.root / "Library" / "Preferences"
            preferences.mkdir(parents=True)
            with (preferences / ".GlobalPreferences.plist").open("wb") as file:
                m.plistlib.dump({"AppleLanguages": ["ko-KR", "en-US"]}, file)
            self.assertEqual("ko", m.system_language())

    def test_menu_columns_align_with_wide_characters(self):
        self.assertEqual(4, m.display_width("残量"))
        self.assertEqual("残量  ", m.pad("残量", 6))
        self.assertEqual("  %9", m.pad("%9", 4, right=True))

    def test_standard_settings_file(self):
        m.USER_SETTINGS.parent.mkdir(parents=True)
        m.USER_SETTINGS.write_text(json.dumps({"language": "es", "data_dir": "state"}))
        with patch.object(m, "set_data_dir") as set_data_dir, \
                patch.object(m, "prepare", return_value=({}, True)), \
                patch.object(m, "CONFIG", None), \
                contextlib.redirect_stdout(io.StringIO()):
            m.main(["--short"])
            set_data_dir.assert_called_once_with(m.USER_SETTINGS.parent.resolve() / "state")
            self.assertIsNone(m.CONFIG)
        self.assertEqual("BAJO", m.MESSAGES["label_low"])


if __name__ == "__main__":
    unittest.main()
