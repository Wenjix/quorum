import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json
import subprocess
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
JQ = str(SCRIPTS / "normalize_findings.jq")
FIXTURE = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "raw_comments.json")
DEFAULT_BOTS_RE = r"cursor\[bot\]|copilot|devin|greptile|coderabbit"


class NormalizeFindingsTest(unittest.TestCase):
    def run_jq(self, bots_re=DEFAULT_BOTS_RE, raw=FIXTURE):
        out = subprocess.run(["jq", "--arg", "re", bots_re, "--arg", "pr", "o/r#7",
                              "--arg", "head", "abc1234", "-f", JQ, raw],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def run_jq_on(self, comments):
        """Run the jq program on an inline list of raw comments."""
        with tempfile.TemporaryDirectory() as tmp:
            raw = pathlib.Path(tmp) / "raw.json"
            raw.write_text(json.dumps(comments))
            return self.run_jq(raw=str(raw))

    def test_stated_severity_per_reviewer(self):
        by = {f["reviewer"]: f for f in self.run_jq()}
        self.assertEqual(by["bugbot"]["stated_severity"], "major")
        self.assertEqual(by["devin"]["stated_severity"], "major")
        self.assertEqual(by["greptile"]["stated_severity"], "major")
        self.assertIsNone(by["copilot"]["stated_severity"])

    def test_pr_head_and_short_names(self):
        rows = self.run_jq()
        self.assertTrue(all(r["pr"] == "o/r#7" and r["head"] == "abc1234" for r in rows))
        self.assertEqual(sorted(r["reviewer"] for r in rows), ["bugbot", "copilot", "devin", "greptile"])

    def test_outdated_copilot_keeps_null_lines(self):
        cop = next(r for r in self.run_jq() if r["reviewer"] == "copilot")
        self.assertTrue(cop["outdated"])
        self.assertEqual(cop["lines"], [None, None])

    def test_human_account_matching_regex_is_excluded(self):
        # The fixture holds a human ("User") account whose login contains "devin".
        rows = self.run_jq()
        self.assertEqual(len(rows), 4)
        self.assertNotIn("devin-fan", [r["login"] for r in rows])

    def test_stated_severity_table(self):
        absent = object()  # key left out of the comment entirely
        long_prefix = "x" * 200
        cases = [
            # (label, login, body, expected reviewer, expected stated_severity)
            ("bugbot high", "cursor[bot]", "**High Severity** boom", "bugbot", "critical"),
            ("bugbot low", "cursor[bot]", "**Low Severity** meh", "bugbot", "minor"),
            ("devin analysis", "devin-ai-integration[bot]",
             '<!-- devin-review-comment {"id": "ANALYSIS_q_1"} --> note', "devin", "minor"),
            ("greptile p0", "greptile-apps[bot]", "**P0** data loss", "greptile", "critical"),
            ("greptile p2", "greptile-apps[bot]", "**P2** style", "greptile", "minor"),
            ("greptile p3", "greptile-apps[bot]", "**P3** typo", "greptile", "nit"),
            ("greptile P10 is not a P-level", "greptile-apps[bot]", "P10 items", "greptile", None),
            ("greptile MP1 is not a P-level", "greptile-apps[bot]", "see MP1 here", "greptile", None),
            ("greptile P1 after 200 chars", "greptile-apps[bot]", long_prefix + " P1", "greptile", None),
            ("null body", "greptile-apps[bot]", None, "greptile", None),
            ("absent body", "greptile-apps[bot]", absent, "greptile", None),
            ("coderabbit short name", "coderabbitai[bot]", "**High Severity** P0", "coderabbit", None),
        ]
        for label, login, body, reviewer, severity in cases:
            with self.subTest(label):
                comment = {"id": 1, "node_id": "n", "user": {"login": login, "type": "Bot"},
                           "path": "a.ts", "line": 1}
                if body is not absent:
                    comment["body"] = body
                (row,) = self.run_jq_on([comment])
                self.assertEqual(row["reviewer"], reviewer)
                self.assertEqual(row["stated_severity"], severity)


if __name__ == "__main__":
    unittest.main()
