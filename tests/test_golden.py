"""Golden test: 150 random scenarios rendered in Turkish must match the recorded outputs.

The outputs were recorded from the earlier, separate implementation of these features and
mapped to the public Turkish texts; any change in warnings, the facts line, the plain text,
the menu, the backlog lines or --short shows up here.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tidewatch as m

GOLDEN = Path(__file__).parent / "fixtures" / "golden-tr.json"
GLOBALS = ("DATA", "CACHE", "CLAUDE_CACHE", "MODELS_FILE", "LOCK", "HELLO_STATE", "HELLO_LOG",
           "HISTORY", "HISTORY_ARCHIVE", "SETTINGS", "USER_SETTINGS", "CONFIG", "TZ",
           "WORK_HOURS", "NIGHT_HOURS", "SUBSCRIPTION_END", "BACKLOG_FILE", "ROSTER_FILE",
           "HELLO_PROMPT", "SHORT_SUMMARY", "CLAUDE_MISSING_IS_STALE",
           "STRICT_CODEX_MODEL_FILES", "LANGUAGE", "MESSAGE_OVERRIDES")


class GoldenTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in GLOBALS:
            patcher = patch.object(m, name, getattr(m, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch.object(m, "MESSAGES", dict(m.ENGLISH)),
                        patch.dict(os.environ, {"CODEX_HOME": str(self.root / ".codex")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("TIDEWATCH_CONFIG", "TIDEWATCH_LANG"):
            os.environ.pop(name, None)
        self.golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.config = self.root / "settings.json"
        self.config.write_text(json.dumps(self.golden["settings"]), encoding="utf-8")
        (self.root / "backlog.md").write_text(self.golden["backlog"], encoding="utf-8")
        (self.root / "roster.md").write_text("# Roster\n", encoding="utf-8")
        m.configure(self.config)
        m.HELLO_LOG.write_text(json.dumps(self.golden["hello_log"]) + "\n", encoding="utf-8")

    def test_recorded_outputs(self):
        scenarios = self.golden["scenarios"]
        self.assertGreaterEqual(len(scenarios), 100)
        for i, scenario in enumerate(scenarios):
            data = scenario["data"]
            expected = json.loads(json.dumps(scenario["expected"]).replace(
                "{TMP}", str(self.root)))
            with patch.object(m.time, "time", return_value=scenario["now"]), \
                    patch.object(m, "prepare", return_value=(data, True)), \
                    contextlib.redirect_stdout(io.StringIO()) as short:
                m.main(["--short", "--config", str(self.config)])
                actual = dict(warnings=m.warnings(data), facts=m.facts_line(data),
                              text=m.render_text(data),
                              menubar=m.menubar(data).splitlines()[:-1],
                              backlog=m.backlog_lines(data))
            actual["short"] = short.getvalue()
            for field in expected:
                with self.subTest(scenario=i, field=field):
                    self.assertEqual(expected[field], actual[field])


if __name__ == "__main__":
    unittest.main()
