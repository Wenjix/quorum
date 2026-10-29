import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import copy
import json
import subprocess
import tempfile
import unittest

import validate_partition as vp

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = str(SCRIPTS / "validate_partition.py")
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
FINDINGS = json.loads((FIXTURES / "findings.json").read_text())
CLUSTERS = json.loads((FIXTURES / "clusters.json").read_text())
PRIORS_FIXTURE = str(FIXTURES / "priors.json")


def _run(findings, clusters, extra):
    """Run validate_partition.py; return (CompletedProcess, parsed output or None)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        fpath, cpath, out = tmp / "findings.json", tmp / "clusters.json", tmp / "scored.json"
        fpath.write_text(json.dumps(findings))
        args = [sys.executable, SCRIPT, str(fpath)]
        if clusters is not None:
            cpath.write_text(json.dumps(clusters))
            args.append(str(cpath))
        args += ["-o", str(out), *extra]
        proc = subprocess.run(args, capture_output=True, text=True)
        return proc, (json.loads(out.read_text()) if proc.returncode == 0 else None)


def run_validate_rc(findings, clusters, extra=()):
    """Return (exit code, stderr)."""
    proc, _ = _run(findings, clusters, extra)
    return proc.returncode, proc.stderr


def run_validate(findings, clusters, extra=()):
    """Return the parsed output document; fail the test if the script exits non-zero."""
    proc, doc = _run(findings, clusters, extra)
    if proc.returncode != 0:
        raise AssertionError(f"validate_partition.py exited {proc.returncode}: {proc.stderr}")
    return doc


def by_id(doc, cluster_id):
    return next(c for c in doc["clusters"] if c["cluster_id"] == cluster_id)


def strip_fields(clusters, cluster_id, fields):
    """Copy of the clusters document with `fields` removed from one cluster."""
    out = copy.deepcopy(clusters)
    for c in out["clusters"]:
        if c["cluster_id"] == cluster_id:
            for f in fields:
                c.pop(f, None)
    return out


class ValidatePartitionTest(unittest.TestCase):
    def test_lanes_p_tags_and_envelope(self):
        doc = run_validate(FINDINGS, CLUSTERS)
        self.assertEqual(doc["schema"], "quorum/triage@1")
        self.assertEqual((doc["pr"], doc["head"]), ("o/r#7", "abc1234"))
        self.assertEqual(doc["lanes"], {"agreed": ["c1"], "solo": ["c2", "c3"]})
        c1 = by_id(doc, "c1")
        self.assertEqual((c1["lane"], c1["p_tag"], c1["validation"]["mode"]), ("agreed", "P1", "escalate"))
        self.assertEqual(by_id(doc, "c3")["validation"], {"mode": "prove_or_kill"})
        self.assertEqual(doc["reviewers"], {"bugbot": 1, "devin": 2, "greptile": 1})
        self.assertEqual(doc["priors"], {})

    def test_locations_union_includes_bugbot_block(self):
        locs = by_id(run_validate(FINDINGS, CLUSTERS), "c1")["locations"]
        self.assertIn({"file": "src/a.ts", "lines": [10, 12], "from": "bugbot-1"}, locs)
        self.assertEqual(len(locs), 3)  # anchored bugbot-1, anchored devin-1, extra block

    def test_multi_member_cluster_requires_mechanism_fields(self):
        bad = strip_fields(CLUSTERS, "c1", ["mechanism_agreement"])
        rc, err = run_validate_rc(FINDINGS, bad)
        self.assertEqual(rc, 1)
        self.assertIn("c1: mechanism_agreement", err)

    def test_all_singletons_uses_stated_severity(self):
        doc = run_validate(FINDINGS, None, extra=["--all-singletons"])
        sev = {c["member_ids"][0]: c["severity"] for c in doc["clusters"]}
        self.assertEqual(sev, {"bugbot-1": "major", "devin-1": "major", "devin-2": "minor", "greptile-1": "minor"})
        self.assertTrue(all(c["lane"] == "solo" for c in doc["clusters"]))

    def test_priors_file_is_embedded(self):
        doc = run_validate(FINDINGS, CLUSTERS, extra=["--priors", PRIORS_FIXTURE])
        self.assertIn("bugbot", doc["priors"])

    def test_output_stays_a_superset_of_the_scored_shape(self):
        doc = run_validate(FINDINGS, CLUSTERS)
        self.assertIn("generated_at", doc)
        self.assertEqual(doc["totals"], {"findings": 4, "clusters": 3, "reviewer_denominator": 3, "gate_split": []})
        c1 = by_id(doc, "c1")
        self.assertEqual((c1["quorum"], c1["reviewers"]), (2, ["bugbot", "devin"]))
        self.assertEqual([m["id"] for m in c1["members"]], ["bugbot-1", "devin-1"])
        self.assertEqual(set(c1["members"][0]),
                         {"id", "reviewer", "file", "lines", "url", "comment_id", "node_id", "outdated"})
        self.assertEqual(c1["disposition_history"], [])
        self.assertEqual(c1["symptom"], "Bytes written to the pipe never reach the reader.")  # judge field passes through
        self.assertEqual((doc["deferred"], doc["timeline"], doc["orphaned_dispositions"]), ([], {}, []))

    def test_singleton_defaults_and_cheapest_check_lands_in_validation(self):
        c2 = by_id(run_validate(FINDINGS, CLUSTERS), "c2")
        self.assertEqual(c2["mechanisms_claimed"], [])
        self.assertNotIn("mechanism_agreement", c2)
        self.assertEqual(c2["validation"],
                         {"mode": "prove_or_kill", "cheapest_check": "Call retry(0) and count attempts."})
        self.assertNotIn("cheapest_check", c2)

    def test_mechanisms_claimed_needs_one_entry_per_member(self):
        short = copy.deepcopy(CLUSTERS)
        short["clusters"][0]["mechanisms_claimed"].pop()
        rc, err = run_validate_rc(FINDINGS, short)
        self.assertEqual(rc, 1)
        self.assertIn("c1: mechanisms_claimed must have one entry per member", err)
        bad_enum = copy.deepcopy(CLUSTERS)
        bad_enum["clusters"][0]["mechanism_agreement"] = "maybe"
        rc, err = run_validate_rc(FINDINGS, bad_enum)
        self.assertEqual(rc, 1)
        self.assertIn("c1: mechanism_agreement 'maybe' not in", err)

    def test_priors_accepts_the_priors_tool_output_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "priors.json"
            path.write_text(json.dumps({"generated_at": "2026-10-29T00:00:00+00:00", "repo": "o/r",
                                        "reviewers": {"devin": {"valid": 1.0, "n": 3}}}))
            doc = run_validate(FINDINGS, CLUSTERS, extra=["--priors", str(path)])
        self.assertEqual(doc["priors"], {"devin": {"valid": 1.0, "n": 3}})

    def test_confidence_gate_split_lands_in_the_solo_lane_with_parent_severity(self):
        low = copy.deepcopy(CLUSTERS)
        low["clusters"][0]["match_confidence"] = 0.4
        doc = run_validate(FINDINGS, low)
        self.assertEqual(doc["totals"]["gate_split"], ["c1"])
        split = [c for c in doc["clusters"] if c.get("gate_split_from") == "c1"]
        self.assertEqual(sorted(c["cluster_id"] for c in split), ["bugbot-1-solo", "devin-1-solo"])
        self.assertTrue(all(c["lane"] == "solo" and c["severity"] == "major" for c in split))
        self.assertEqual(doc["lanes"]["agreed"], [])


class LocationHelpersTest(unittest.TestCase):
    def test_lane_for(self):
        self.assertEqual([vp.lane_for(q) for q in (1, 2, 3)], ["solo", "agreed", "agreed"])

    def test_parse_extra_locations_range_single_line_and_noise(self):
        body = "text\n<!-- LOCATIONS START\nsrc/a.ts#L10-L12\n  lib/b.py#L7  \nnot a location\nLOCATIONS END -->"
        self.assertEqual(vp.parse_extra_locations(body), [
            {"file": "src/a.ts", "lines": [10, 12]},
            {"file": "lib/b.py", "lines": [7, 7]},
        ])
        self.assertEqual(vp.parse_extra_locations("src/a.ts#L1-L2 outside any block"), [])
        self.assertEqual(vp.parse_extra_locations(None), [])

    def test_union_locations_dedupes_on_file_and_lines_keeping_first_from(self):
        fmap = {
            "x-1": {"file": "src/a.ts", "lines": [10, 12], "body": ""},
            "y-1": {"file": "src/a.ts", "lines": [10, 12],
                    "body": "LOCATIONS START\nsrc/a.ts#L10-L12\nsrc/z.ts#L3\nLOCATIONS END"},
        }
        locs = vp.union_locations({"member_ids": ["x-1", "y-1"]}, fmap)
        self.assertEqual(locs, [
            {"file": "src/a.ts", "lines": [10, 12], "from": "x-1"},
            {"file": "src/z.ts", "lines": [3, 3], "from": "y-1"},
        ])


if __name__ == "__main__":
    unittest.main()
