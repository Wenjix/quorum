import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import contextlib
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from fakes import FakeClient, temp_cwd
from quorum_doc import MARKER, SCHEMA, extract_doc, now_iso
from quorum_ledger import LEDGER_REF, main
from quorum_render import load_protocol, minimal_doc, render_agent_layer, render_brief, render_comment

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
PLAN = str(FIXTURES / "triage.json")
COMMENTS = "repos/o/r/issues/7/comments"
COMMENT_ID = 555


def load(name):
    return json.loads((FIXTURES / name).read_text())


def cluster(doc, cluster_id):
    return next(c for c in doc["clusters"] if c["cluster_id"] == cluster_id)


def posted(body, comment_id=COMMENT_ID):
    """The issue-comment listing GitHub returns when `body` is the ledger."""
    return [{"id": comment_id, "body": body, "author_association": "OWNER", "user": {"login": "me"}}]


def ledger_body(doc):
    return render_comment(doc, load_protocol(LEDGER_REF))


def fake(comments=(), **extra):
    responses = {
        ("GET", COMMENTS): list(comments),
        ("GET", "user"): {"login": "me"},
        ("POST", COMMENTS): {"id": 999},
    }
    responses.update(extra.get("responses") or {})
    return FakeClient(responses)


def run(argv, client):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv, client=client)
    return code, out.getvalue(), err.getvalue()


def writes(client):
    """(method, path, body) of every call that writes, in order."""
    return [(m, p, b) for m, p, b, _ in client.calls if m in ("POST", "PATCH", "GRAPHQL")]


def comment_writes(client):
    return [(m, p, b) for m, p, b in writes(client)
            if p == COMMENTS or p.startswith("repos/o/r/issues/comments/")]


def written_doc(client):
    (_, _, body), = comment_writes(client)
    return extract_doc(body["body"])


def truncated_body(doc):
    """The comment as posted when only the minimal level of `doc` fits."""
    return "\n".join(render_brief(doc) + render_agent_layer(load_protocol(LEDGER_REF), minimal_doc(doc)))


def temp_json(test, data):
    """Path of a temporary JSON file holding `data`, removed after `test`."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = pathlib.Path(folder.name) / "data.json"
    path.write_text(json.dumps(data))
    return str(path)


def reacted(client):
    return sorted(int(p.split("/")[-2]) for m, p, _ in writes(client) if p.endswith("/reactions"))


class PostPlanTest(unittest.TestCase):
    def test_post_plan_posts_new_comment_and_reacts(self):
        client = fake()
        code, out, err = run(["post-plan", "o/r", "7", PLAN], client)
        self.assertEqual(code, 0, err)
        (method, path, body), = comment_writes(client)
        self.assertEqual((method, path), ("POST", COMMENTS))
        self.assertTrue(body["body"].startswith(MARKER))
        # Eyes on the members of the agreed clusters (c1, c4) only.
        self.assertEqual(reacted(client), [1001, 1002, 1005, 1006])
        self.assertFalse([w for w in writes(client) if w[0] == "GRAPHQL"])
        doc = written_doc(client)
        self.assertEqual(doc["schema"], SCHEMA)
        self.assertEqual(doc["gate"]["state"], "pending")
        self.assertEqual(doc["deferred"], [])
        self.assertTrue(doc["timeline"]["plan_at"])

    def in_temp_cwd(self, git=False):
        """Run the test from an empty working directory (where the priors cache
        lives outside a git repository), or from a fresh repository's top level."""
        return pathlib.Path(temp_cwd(self, git=git))

    def write_priors(self, cwd, generated_at, reviewers):
        path = cwd / ".quorum" / "priors" / "o-r.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"generated_at": generated_at, "repo": "o/r", "reviewers": reviewers}))

    def test_post_plan_embeds_fresh_cached_priors(self):
        cwd = self.in_temp_cwd()
        priors = load("priors.json")
        self.write_priors(cwd, now_iso(), priors)
        client = fake()
        code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(written_doc(client)["priors"], priors)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_post_plan_run_from_a_subdirectory_reads_the_repository_cache(self):
        top = self.in_temp_cwd(git=True)
        priors = load("priors.json")
        self.write_priors(top, now_iso(), priors)
        (top / "scratch").mkdir()
        os.chdir(top / "scratch")  # the skill's scratch directory inside the checkout
        client = fake()
        code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(written_doc(client)["priors"], priors)

    def test_post_plan_ignores_stale_cached_priors(self):
        cwd = self.in_temp_cwd()
        eight_days_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=8)).isoformat()
        self.write_priors(cwd, eight_days_ago, load("priors.json"))
        client = fake()
        code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(written_doc(client)["priors"], {})

    def test_priors_already_in_the_scored_file_beat_the_cache(self):
        cwd = self.in_temp_cwd()
        self.write_priors(cwd, now_iso(), load("priors.json"))
        plan = load("triage.json")
        embedded = {"devin": {"valid": 0.5, "partly": 0.25, "invalid": 0.25, "n": 8}}
        plan["priors"] = embedded
        client = fake()
        code, _, err = run(["post-plan", "o/r", "7", temp_json(self, plan), "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(written_doc(client)["priors"], embedded)

    def test_post_plan_refuses_a_plan_for_another_pull_request(self):
        for argv in (["o/r", "8"], ["o/other", "7"]):  # the plan's pr is o/r#7
            for dry_run in ([], ["--dry-run"]):
                with self.subTest(argv=argv, dry_run=dry_run):
                    client = fake()
                    code, out, err = run(["post-plan", *argv, PLAN, *dry_run], client)
                    self.assertEqual(code, 2)
                    self.assertIn("o/r#7", err)
                    self.assertIn(f"{argv[0]}#{argv[1]}", err)
                    self.assertEqual(client.calls, [])  # refused before any read or write
                    self.assertEqual(out, "")

    def test_post_plan_matches_the_pr_case_insensitively_and_without_one(self):
        plan = load("triage.json")
        del plan["pr"]
        for argv in (["O/R", "7", PLAN], ["o/r", "8", temp_json(self, plan)]):
            with self.subTest(argv=argv):
                code, out, err = run(["post-plan", *argv, "--dry-run"], fake())
                self.assertEqual(code, 0, err)
                self.assertTrue(out.startswith(MARKER))

    def test_post_plan_dry_run_makes_no_writes(self):
        client = fake()
        code, out, err = run(["post-plan", "o/r", "7", PLAN, "--dry-run", "--minimize"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(writes(client), [])
        self.assertTrue({m for m, *_ in client.calls} <= {"GET"})
        self.assertTrue(out.startswith(MARKER))
        self.assertIn("## 🤝 Quorum · triage brief", out)
        self.assertIn("post new comment", err)
        self.assertIn("reactions 4: 1001, 1002, 1005, 1006", err)
        self.assertIn("minimize 2: devin-1, greptile-2", err)

    def test_post_plan_dry_run_survives_a_failed_read(self):
        client = FakeClient({("GET", COMMENTS): RuntimeError("HTTP 404")})
        code, out, err = run(["post-plan", "o/r", "7", PLAN, "--dry-run"], client)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith(MARKER))
        self.assertIn("HTTP 404", err)

    def test_post_plan_rerun_patches_and_carries_disposition(self):
        prior = load("triage.ledger.json")
        client = fake(posted(ledger_body(prior)))
        code, out, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        (method, path, _), = comment_writes(client)
        self.assertEqual((method, path), ("PATCH", f"repos/o/r/issues/comments/{COMMENT_ID}"))
        self.assertEqual(reacted(client), [])
        doc = written_doc(client)
        self.assertEqual(cluster(doc, "c1")["disposition"], cluster(prior, "c1")["disposition"])
        self.assertEqual(cluster(doc, "c1")["escalation"]["invariant"],
                         cluster(prior, "c1")["escalation"]["invariant"])
        self.assertEqual(doc["timeline"]["plan_at"], prior["timeline"]["plan_at"])
        self.assertEqual(doc["gate"]["state"], "clear")
        self.assertEqual(doc["deferred"], ["c3"])
        # The head did not move, so nothing was compared.
        self.assertFalse([p for _, p, _, _ in client.calls if "/compare/" in p])

    def test_post_plan_head_moved_marks_touched_dispositions_stale(self):
        prior = load("triage.ledger.json")
        prior["head"] = "0000000"
        compare = "repos/o/r/compare/0000000...abc1234"
        cases = {
            "touched files": ({"files": [{"filename": "src/a.ts"}]}, {"c1"}),
            "compare failed": (RuntimeError("HTTP 500"), {"c1", "c2", "c3", "c4"}),
            "capped at 300": ({"files": [{"filename": f"x{i}.ts"} for i in range(300)]},
                              {"c1", "c2", "c3", "c4"}),
        }
        for name, (answer, stale) in cases.items():
            with self.subTest(name):
                client = fake(posted(ledger_body(prior)), responses={("GET", compare): answer})
                code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
                self.assertEqual(code, 0, err)
                doc = written_doc(client)
                got = {c["cluster_id"] for c in doc["clusters"] if c["disposition"].get("stale")}
                self.assertEqual(got, stale)

    def test_the_fix_push_keeps_the_gate_clear(self):
        # The ledger was posted at 0000000; the owner confirms c1, pushes the fix
        # fee1234 (touching c1's files) and records it with `dispose --commit`.
        ledger = load("triage.ledger.json")
        ledger["head"] = "0000000"
        del cluster(ledger, "c1")["disposition"]["commit"]
        client = fake(posted(ledger_body(ledger)))
        code, _, err = run(["dispose", "o/r", "7", "c1", "--commit", "fee1234"], client)
        self.assertEqual(code, 0, err)
        disposed = comment_writes(client)[0][2]["body"]
        self.assertEqual(extract_doc(disposed)["gate"]["state"], "clear")

        # The next post-plan sees head abc1234; the compare touches c1's files.
        compare = "repos/o/r/compare/0000000...abc1234"
        fix_sha, later_sha = "fee1234" + "0" * 33, "abc1234" + "0" * 33
        touched = [{"filename": "src/a.ts"}, {"filename": "src/b.ts"}]
        for commits, gate, stale in (([fix_sha, later_sha], "clear", None),   # the fix is in the push
                                     ([later_sha], "blocked", True)):         # it is not: re-check
            with self.subTest(commits=commits):
                answer = {"files": touched, "commits": [{"sha": sha} for sha in commits]}
                client = fake(posted(disposed), responses={("GET", compare): answer})
                code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
                self.assertEqual(code, 0, err)
                (_, _, body), = comment_writes(client)
                doc = extract_doc(body["body"])
                self.assertEqual(cluster(doc, "c1")["disposition"].get("stale"), stale)
                self.assertEqual(doc["gate"]["state"], gate)
                brief = body["body"].split("<details>")[0]
                if gate == "clear":
                    self.assertIn("**✅ Merge gate clear**", brief)
                    self.assertNotIn("re-check", brief)
                else:
                    self.assertIn("**🛑 Blocked", brief)

    def test_post_plan_over_a_truncated_ledger_carries_dispositions_by_comment_id(self):
        ledger = load("triage.ledger.json")
        del cluster(ledger, "c4")["disposition"]
        plan = load("triage.json")
        cluster(plan, "c3")["members"][0]["comment_id"] = 2004  # the finding behind c3 was replaced
        client = fake(posted(truncated_body(ledger)))
        code, _, err = run(["post-plan", "o/r", "7", temp_json(self, plan), "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        self.assertNotIn("warning", err)
        doc = written_doc(client)
        self.assertNotIn("truncated", doc)
        for cid in ("c1", "c2"):
            self.assertEqual(cluster(doc, cid)["disposition"], cluster(ledger, cid)["disposition"])
        self.assertEqual(cluster(doc, "c1")["escalation"]["invariant"],
                         cluster(ledger, "c1")["escalation"]["invariant"])
        self.assertNotIn("disposition", cluster(doc, "c3"))
        (orphan,) = doc["orphaned_dispositions"]
        self.assertEqual((orphan["cluster_id"], orphan["member_comment_ids"]), ("c3", [1004]))
        self.assertEqual(orphan["disposition"]["verdict"], "deferred")
        self.assertEqual(doc["timeline"]["plan_at"], ledger["timeline"]["plan_at"])

    def test_post_plan_warns_when_no_prior_disposition_survives(self):
        client = fake(posted(ledger_body(load("triage.ledger.json"))))
        with mock.patch("quorum_ledger.inherit", side_effect=lambda new, prior, files: new):
            code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        self.assertIn("warning: none of the 4 dispositions in the posted ledger", err)

    def test_post_plan_over_legacy_comment_upgrades_in_place(self):
        legacy = load("triage.json")
        for key in ("schema", "lanes", "deferred", "timeline", "orphaned_dispositions", "priors"):
            legacy.pop(key)
        body = "\n".join([MARKER, "## 🤝 Quorum — review synthesis", "",
                          "<details><summary>clusters.scored.json</summary>", "", "```json",
                          json.dumps(legacy, indent=2), "```", "</details>"])
        client = fake(posted(body, comment_id=321))
        code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        (method, path, body), = comment_writes(client)
        self.assertEqual((method, path), ("PATCH", "repos/o/r/issues/comments/321"))
        self.assertIn("## 🤝 Quorum · triage brief", body["body"])
        self.assertEqual(extract_doc(body["body"])["schema"], SCHEMA)

    def test_post_plan_minimizes_non_primary_members_only_when_asked(self):
        client = fake()
        code, _, err = run(["post-plan", "o/r", "7", PLAN, "--minimize", "--no-reactions"], client)
        self.assertEqual(code, 0, err)
        minimized = [b["id"] for m, _, b in writes(client) if m == "GRAPHQL"]
        self.assertEqual(sorted(minimized), ["PRRC_devin-1", "PRRC_greptile-2"])
        self.assertEqual(reacted(client), [])

    def test_failed_reactions_are_counted_not_fatal(self):
        client = fake(responses={("POST", "repos/o/r/pulls/comments/"): RuntimeError("HTTP 403")})
        code, out, err = run(["post-plan", "o/r", "7", PLAN], client)
        self.assertEqual(code, 0, err)
        self.assertIn("reactions: 0/4", out)

    def test_rejected_body_names_its_length(self):
        client = fake(responses={("POST", COMMENTS): RuntimeError("HTTP 422 body is too long")})
        code, _, err = run(["post-plan", "o/r", "7", PLAN, "--no-reactions"], client)
        self.assertEqual(code, 1)
        self.assertRegex(err, r"\d+ characters")
        self.assertIn("HTTP 422", err)


class DisposeTest(unittest.TestCase):
    def plan_client(self, doc=None):
        return fake(posted(ledger_body(doc or load("triage.json"))))

    def test_dispose_requires_note_for_new_verdict(self):
        client = self.plan_client()
        code, _, err = run(["dispose", "o/r", "7", "c2", "--verdict", "valid"], client)
        self.assertEqual(code, 2)
        self.assertIn("--note", err)
        self.assertEqual(comment_writes(client), [])

    def test_dispose_unknown_cluster(self):
        client = self.plan_client()
        code, _, err = run(["dispose", "o/r", "7", "cX", "--verdict", "valid", "--note", "n"], client)
        self.assertEqual(code, 2)
        self.assertIn("unknown cluster_id: cX", err)
        self.assertEqual(writes(client), [])

    def test_dispose_rejects_bad_inputs(self):
        base = ["dispose", "o/r", "7", "c2", "--verdict", "valid", "--note", "n"]
        cases = {
            "commit": ["--commit", "NOT-A-SHA"],
            "long note": ["--note", "x" * 2001],
            "long evidence": ["--evidence", "x" * 2001],
            "long tests": ["--tests", "x" * 2001],
            "foreign member": ["--member", "bugbot-1:valid"],
            "member verdict": ["--member", "devin-2:maybe"],
            "member shape": ["--member", "devin-2"],
        }
        for name, extra in cases.items():
            with self.subTest(name):
                client = self.plan_client()
                code, _, err = run(base + extra, client)
                self.assertEqual(code, 2, err)
                self.assertEqual(comment_writes(client), [])

    def test_dispose_records_members_severity_and_warns_without_evidence(self):
        client = self.plan_client()
        code, out, err = run(["dispose", "o/r", "7", "c3", "--verdict", "partly", "--note", "half right",
                              "--member", "greptile-1:partly", "--severity", "major", "--by", "owner"],
                             client)
        self.assertEqual(code, 0, err)
        self.assertIn("warning: c3: partly recorded without --evidence", err)
        doc = written_doc(client)
        d = cluster(doc, "c3")["disposition"]
        self.assertEqual((d["verdict"], d["note"], d["by"], d["severity"]), ("partly", "half right", "owner", "major"))
        self.assertEqual(d["members"], {"greptile-1": "partly"})
        self.assertEqual(d["evidence"], [])
        self.assertTrue(d["at"])
        # The corrected severity makes c3 gate-relevant: partly without a commit blocks.
        self.assertIn("c3", doc["gate"]["blocking"])
        self.assertEqual(doc["timeline"]["first_disposition_at"], d["at"])

    def test_dispose_commit_only_keeps_prior_verdict(self):
        ledger = load("triage.ledger.json")
        prior = cluster(ledger, "c2")["disposition"]
        del prior["commit"]
        client = self.plan_client(ledger)
        with mock.patch("quorum_ledger.now_iso", return_value="2026-10-29T14:00:00+00:00"):
            code, _, err = run(["dispose", "o/r", "7", "c2", "--commit", "abc1234",
                                "--tests", "npm test: 42 pass"], client)
        self.assertEqual(code, 0, err)
        c2 = cluster(written_doc(client), "c2")
        d = c2["disposition"]
        self.assertEqual((d["verdict"], d["note"], d["evidence"]), (prior["verdict"], prior["note"], prior["evidence"]))
        self.assertEqual((d["commit"], d["tests"]), ("abc1234", "npm test: 42 pass"))
        self.assertEqual(d["at"], "2026-10-29T14:00:00+00:00")
        self.assertEqual(c2["disposition_history"][-1], prior)

    def test_commit_only_refresh_keeps_the_prior_author_and_clears_stale(self):
        for by, expected in ((None, "owner"), ("ci", "ci")):
            with self.subTest(by=by):
                ledger = load("triage.ledger.json")
                prior = cluster(ledger, "c2")["disposition"]
                del prior["commit"]
                prior.update(by="owner", stale=True)
                client = self.plan_client(ledger)
                argv = ["dispose", "o/r", "7", "c2", "--commit", "abc1234"] + (["--by", by] if by else [])
                code, _, err = run(argv, client)
                self.assertEqual(code, 0, err)
                d = cluster(written_doc(client), "c2")["disposition"]
                self.assertEqual((d["by"], d["commit"], d["verdict"]), (expected, "abc1234", "valid"))
                self.assertNotIn("stale", d)

    def test_dispose_from_file_rejects_disposition_flags(self):
        source = str(FIXTURES / "dispositions.json")
        for flag in (["--verdict", "valid"], ["--note", "n"], ["--evidence", "e"], ["--member", "devin-2:valid"],
                     ["--severity", "major"], ["--commit", "abc1234"], ["--tests", "t"]):
            with self.subTest(flag[0]):
                client = self.plan_client()
                code, _, err = run(["dispose", "o/r", "7", "--from", source] + flag, client)
                self.assertEqual(code, 2)
                self.assertIn(flag[0], err)
                self.assertEqual(writes(client), [])

    def test_a_cluster_listed_twice_gets_one_reply_and_the_last_verdict(self):
        twice = {"dispositions": [
            {"cluster_id": "c2", "verdict": "valid", "note": "first look", "evidence": ["e"]},
            {"cluster_id": "c2", "verdict": "invalid", "note": "second look"},
        ]}
        client = self.plan_client()
        code, _, err = run(["dispose", "o/r", "7", "--from", temp_json(self, twice), "--reply"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(cluster(written_doc(client), "c2")["disposition"]["note"], "second look")
        replies = [(p, b["body"]) for m, p, b in writes(client) if p.endswith("/replies")]
        self.assertEqual(replies, [("repos/o/r/pulls/7/comments/1003/replies",
                                    "**Quorum** · dismissed · second look")])

    def test_dispose_commit_only_needs_a_prior_disposition(self):
        client = self.plan_client()
        code, _, err = run(["dispose", "o/r", "7", "c2", "--commit", "abc1234"], client)
        self.assertEqual(code, 2)
        self.assertIn("--verdict", err)
        self.assertEqual(comment_writes(client), [])

    def test_dispose_from_file_and_reply(self):
        client = self.plan_client()
        code, out, err = run(["dispose", "o/r", "7", "--from", str(FIXTURES / "dispositions.json"), "--reply"],
                             client)
        self.assertEqual(code, 0, err)
        doc = written_doc(client)
        c2, c3 = cluster(doc, "c2")["disposition"], cluster(doc, "c3")["disposition"]
        self.assertEqual((c2["verdict"], c2["by"], c2["members"]), ("valid", "agent", {"devin-2": "valid"}))
        self.assertEqual(c3["verdict"], "invalid")
        self.assertNotIn("QUORUM_DISPOSITION", c2)
        replies = {p: b["body"] for m, p, b in writes(client) if p.endswith("/replies")}
        self.assertEqual(replies, {
            "repos/o/r/pulls/7/comments/1003/replies": "**Quorum** · valid · off by one confirmed",
            "repos/o/r/pulls/7/comments/1004/replies": "**Quorum** · dismissed · the shadowed name is never read",
        })
        self.assertIn("replies: 2/2", out)

    def test_reply_names_the_fixing_commit(self):
        client = self.plan_client()
        code, _, err = run(["dispose", "o/r", "7", "c2", "--verdict", "valid", "--note", "fixed",
                            "--evidence", "e", "--commit", "abc1234", "--reply"], client)
        self.assertEqual(code, 0, err)
        replies = [b["body"] for m, p, b in writes(client) if p.endswith("/replies")]
        self.assertEqual(replies, ["**Quorum** · valid · fixed in `abc1234` · fixed"])

    def test_truncated_ledger_needs_scored_to_rebuild(self):
        ledger = load("triage.ledger.json")
        body = truncated_body(ledger)
        argv = ["dispose", "o/r", "7", "c3", "--verdict", "invalid", "--note", "not reproducible"]

        client = fake(posted(body))
        code, _, err = run(argv, client)
        self.assertEqual(code, 2)
        self.assertIn("--scored", err)
        self.assertEqual(comment_writes(client), [])

        client = fake(posted(body))
        code, _, err = run(argv + ["--scored", PLAN], client)
        self.assertEqual(code, 0, err)
        doc = written_doc(client)
        self.assertNotIn("truncated", doc)
        c1, c4, c3 = cluster(doc, "c1"), cluster(doc, "c4"), cluster(doc, "c3")
        self.assertEqual(c1["disposition"]["commit"], "ba814e4")
        self.assertEqual(c1["canonical_title"], cluster(ledger, "c1")["canonical_title"])
        self.assertEqual(c1["escalation"]["invariant"], cluster(ledger, "c1")["escalation"]["invariant"])
        self.assertNotIn("escalation", c4)  # the minimal doc's null escalation counts as absent
        self.assertEqual(c3["disposition"]["verdict"], "invalid")
        self.assertEqual(c3["disposition_history"][-1]["verdict"], "deferred")

    def test_dispose_dry_run_prints_the_body_and_planned_replies(self):
        client = self.plan_client()
        code, out, err = run(["dispose", "o/r", "7", "c2", "--verdict", "invalid", "--note", "cannot reproduce",
                              "--reply", "--dry-run"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(writes(client), [])
        self.assertEqual(cluster(extract_doc(out), "c2")["disposition"]["verdict"], "invalid")
        self.assertIn(f"update comment {COMMENT_ID}", err)
        self.assertIn("replies 1: 1003", err)

    def test_dispose_without_a_ledger_comment(self):
        client = fake()
        code, _, err = run(["dispose", "o/r", "7", "c2", "--verdict", "valid", "--note", "n"], client)
        self.assertEqual(code, 1)
        self.assertIn("post-plan", err)
        self.assertEqual(writes(client), [])


class EscalateTest(unittest.TestCase):
    def test_escalate_writes_escalation_and_valid_disposition(self):
        client = fake(posted(ledger_body(load("triage.json"))))
        code, _, err = run(["escalate", "o/r", "7", "c1", "--from", str(FIXTURES / "escalation.json")], client)
        self.assertEqual(code, 0, err)
        (method, _, body), = comment_writes(client)
        self.assertEqual(method, "PATCH")
        c1 = cluster(extract_doc(body["body"]), "c1")
        source = load("escalation.json")
        self.assertNotIn("QUORUM_ESCALATION", c1["escalation"])
        self.assertEqual(c1["escalation"]["chokepoint"], source["chokepoint"])
        self.assertEqual(c1["escalation"]["by"], "agent")
        self.assertTrue(c1["escalation"]["at"])
        d = c1["disposition"]
        # The note names the chokepoint; the invariant has its own line in the brief.
        self.assertEqual((d["verdict"], d["note"], d["evidence"]), ("valid", source["chokepoint"], source["evidence"]))
        self.assertEqual(d["at"], c1["escalation"]["at"])
        brief = body["body"].split("<details>")[0]
        self.assertIn("  ✅ valid · src/pipe.ts writeAll(): the one function every writer calls.", brief)
        self.assertEqual(brief.count("Every write to the pipe"), 1)
        self.assertIn("  Invariant: Every write to the pipe", brief)
        self.assertNotIn("members", d)  # none given, none recorded

    def test_escalation_without_a_chokepoint_notes_the_invariant_once(self):
        source = load("escalation.json")
        del source["chokepoint"]
        client = fake(posted(ledger_body(load("triage.json"))))
        code, _, err = run(["escalate", "o/r", "7", "c1", "--from", temp_json(self, source)], client)
        self.assertEqual(code, 0, err)
        (_, _, body), = comment_writes(client)
        self.assertEqual(cluster(extract_doc(body["body"]), "c1")["disposition"]["note"], source["invariant"])
        brief = body["body"].split("<details>")[0]
        self.assertEqual(brief.count("Every write to the pipe"), 1)  # the note, with no Invariant: line
        self.assertNotIn("Invariant:", brief)

    def test_escalate_records_member_verdicts_on_a_split_cluster(self):
        source = load("escalation.json")
        source["members"] = {"devin-1": "partly"}  # devin named the wrong mechanism
        client = fake(posted(ledger_body(load("triage.json"))))
        code, _, err = run(["escalate", "o/r", "7", "c1", "--from", temp_json(self, source)], client)
        self.assertEqual(code, 0, err)
        d = cluster(written_doc(client), "c1")["disposition"]
        self.assertEqual((d["verdict"], d["members"]), ("valid", {"devin-1": "partly"}))

    def test_escalate_rejects_bad_member_verdicts(self):
        for members, expected in (({"copilot-9": "partly"}, "copilot-9"), ({"devin-1": "maybe"}, "maybe")):
            with self.subTest(members=members):
                source = load("escalation.json")
                source["members"] = members
                client = fake(posted(ledger_body(load("triage.json"))))
                code, _, err = run(["escalate", "o/r", "7", "c1", "--from", temp_json(self, source)], client)
                self.assertEqual(code, 2)
                self.assertIn(expected, err)
                self.assertEqual(writes(client), [])

    def test_escalate_pushes_the_prior_disposition_to_history(self):
        ledger = load("triage.ledger.json")
        client = fake(posted(ledger_body(ledger)))
        code, _, err = run(["escalate", "o/r", "7", "c1", "--from", str(FIXTURES / "escalation.json")], client)
        self.assertEqual(code, 0, err)
        c1 = cluster(written_doc(client), "c1")
        self.assertEqual(c1["disposition_history"][-1], cluster(ledger, "c1")["disposition"])
        self.assertEqual(c1["disposition"]["note"], load("escalation.json")["chokepoint"])

    def test_escalate_dry_run_makes_no_writes(self):
        client = fake(posted(ledger_body(load("triage.json"))))
        code, out, err = run(["escalate", "o/r", "7", "c1", "--from", str(FIXTURES / "escalation.json"),
                              "--dry-run"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(writes(client), [])
        self.assertEqual(cluster(extract_doc(out), "c1")["disposition"]["verdict"], "valid")

    def test_escalate_rebuilds_a_truncated_ledger_from_scored(self):
        ledger = load("triage.ledger.json")
        argv = ["escalate", "o/r", "7", "c1", "--from", str(FIXTURES / "escalation.json")]
        client = fake(posted(truncated_body(ledger)))
        code, _, err = run(argv, client)
        self.assertEqual(code, 2)
        self.assertIn("--scored", err)
        client = fake(posted(truncated_body(ledger)))
        code, _, err = run(argv + ["--scored", PLAN], client)
        self.assertEqual(code, 0, err)
        doc = written_doc(client)
        self.assertNotIn("truncated", doc)
        self.assertEqual(cluster(doc, "c1")["escalation"]["siblings"], load("escalation.json")["siblings"])
        self.assertEqual(cluster(doc, "c2")["disposition"]["verdict"], "valid")

    def test_escalate_reports_an_over_long_invariant(self):
        source = load("escalation.json")
        source["invariant"] = "x" * 2001
        client = fake(posted(ledger_body(load("triage.json"))))
        code, _, err = run(["escalate", "o/r", "7", "c1", "--from", temp_json(self, source)], client)
        self.assertEqual(code, 2)
        self.assertIn("invariant", err)
        self.assertNotIn("--note", err)
        self.assertEqual(writes(client), [])

    def test_escalate_takes_agreed_clusters_only(self):
        plan = load("triage.json")
        cluster(plan, "c1")["lane"] = "contested"
        client = fake(posted(ledger_body(plan)))
        code, _, err = run(["escalate", "o/r", "7", "c1", "--from", str(FIXTURES / "escalation.json")], client)
        self.assertEqual(code, 2)
        self.assertIn("agreed", err)
        self.assertEqual(writes(client), [])

    def test_escalate_rejects_solo_cluster(self):
        client = fake(posted(ledger_body(load("triage.json"))))
        code, _, err = run(["escalate", "o/r", "7", "c2", "--from", str(FIXTURES / "escalation.json")], client)
        self.assertEqual(code, 2)
        self.assertIn("solo", err)
        self.assertEqual(writes(client), [])


class MainTest(unittest.TestCase):
    def test_usage_errors_return_2(self):
        for argv in (["bogus"], ["dispose", "o/r"], ["dispose", "o/r", "7", "c2", "--verdict", "maybe"]):
            with self.subTest(argv=argv):
                code, _, err = run(argv, fake())
                self.assertEqual(code, 2)
                self.assertIn("usage:", err)

    def test_unexpected_errors_return_1(self):
        client = FakeClient({("GET", COMMENTS): ValueError("bad JSON from GitHub")})
        code, out, err = run(["show", "o/r", "7"], client)
        self.assertEqual(code, 1)
        self.assertIn("error: bad JSON from GitHub", err)


class ShowTest(unittest.TestCase):
    def test_show_prints_embedded_json(self):
        body = ledger_body(load("triage.ledger.json"))
        client = fake(posted(body))
        code, out, err = run(["show", "o/r", "7"], client)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), extract_doc(body))
        self.assertEqual(writes(client), [])

    def test_show_on_a_truncated_ledger_prints_the_gate(self):
        ledger = load("triage.ledger.json")
        del cluster(ledger, "c1")["disposition"]["commit"]  # confirmed, not yet fixed: it blocks
        client = fake(posted(truncated_body(ledger)))
        code, out, err = run(["show", "o/r", "7"], client)
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertIs(shown["truncated"], True)
        self.assertEqual((shown["gate"]["state"], shown["gate"]["blocking"]), ("blocked", ["c1"]))
        self.assertEqual(shown["deferred"], ["c3"])

    def test_show_without_a_ledger_comment(self):
        code, out, err = run(["show", "o/r", "7"], fake())
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no Quorum ledger", err)


if __name__ == "__main__":
    unittest.main()
