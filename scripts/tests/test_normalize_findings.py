import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import json
import subprocess
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
JQ = str(SCRIPTS / "normalize_findings.jq")
FIXTURE = str(pathlib.Path(__file__).resolve().parent / "fixtures" / "raw_comments.json")


class NormalizeFindingsTest(unittest.TestCase):
    def run_jq(self, bots_re=r"cursor\[bot\]|copilot|devin|greptile|coderabbit"):
        out = subprocess.run(["jq", "--arg", "re", bots_re, "--arg", "pr", "o/r#7",
                              "--arg", "head", "abc1234", "-f", JQ, FIXTURE],
                             capture_output=True, text=True, check=True).stdout
        return json.loads(out)

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


if __name__ == "__main__":
    unittest.main()
