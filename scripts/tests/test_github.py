import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import io
import json
import os
import subprocess
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

from fakes import FakeClient
from quorum_github import (
    GhClient, TokenClient, add_eyes, compare_files, default_client,
    find_marker_comment, get_pr_head, minimize, reply_in_thread, upsert_comment,
)

MARKER = "<!-- quorum:triage -->"


class FakeResponse:
    """Context-manager stand-in for what urllib.request.urlopen returns."""

    def __init__(self, payload, status=200):
        self.status = status
        self._raw = b"" if payload is None else json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeClientTest(unittest.TestCase):
    def test_longest_prefix_wins_and_unmatched_is_none(self):
        c = FakeClient({
            ("GET", "repos/o/r"): "short",
            ("GET", "repos/o/r/pulls"): "long",
        })
        self.assertEqual(c.api("GET", "repos/o/r/pulls/7"), "long")
        self.assertEqual(c.api("GET", "repos/o/r/issues/7"), "short")
        self.assertIsNone(c.api("POST", "repos/o/r/pulls/7"))
        self.assertIsNone(c.api("GET", "repos/x/y"))

    def test_records_calls_and_raises_exception_values(self):
        c = FakeClient({("GET", "boom"): RuntimeError("down")})
        with self.assertRaises(RuntimeError):
            c.api("GET", "boom", params={"a": 1})
        c.graphql("query", {"id": "n"})
        self.assertEqual(c.calls, [
            ("GET", "boom", None, {"a": 1}),
            ("GRAPHQL", "query", {"id": "n"}, None),
        ])


class TransportFunctionsTest(unittest.TestCase):
    def test_get_pr_head(self):
        c = FakeClient({("GET", "repos/o/r/pulls/7"): {"head": {"sha": "abc123"}}})
        self.assertEqual(get_pr_head(c, "o/r", 7), "abc123")
        self.assertEqual(c.calls[0][:2], ("GET", "repos/o/r/pulls/7"))

    def test_find_marker_comment_returns_newest_match(self):
        c = FakeClient({("GET", "repos/o/r/issues/7/comments"): [
            {"id": 4, "body": f"{MARKER}\nolder"},
            {"id": 9, "body": "unrelated"},
            {"id": 6, "body": f"{MARKER}\nnewer"},
            {"id": 2, "body": None},
        ]})
        found = find_marker_comment(c, "o/r", 7, MARKER)
        self.assertEqual(found, {"id": 6, "body": f"{MARKER}\nnewer"})
        # Pagination is requested from the client, not hand-rolled here.
        self.assertEqual(c.calls[0][3], {"paginate": True})

    def test_find_marker_comment_none_when_absent(self):
        c = FakeClient({("GET", "repos/o/r/issues/7/comments"): [{"id": 1, "body": "hi"}]})
        self.assertIsNone(find_marker_comment(c, "o/r", 7, MARKER))
        self.assertIsNone(find_marker_comment(FakeClient(), "o/r", 7, MARKER))

    def test_upsert_patches_when_existing(self):
        c = FakeClient()
        self.assertEqual(upsert_comment(c, "o/r", 7, "body", existing_id=5), 5)
        self.assertEqual(c.calls, [
            ("PATCH", "repos/o/r/issues/comments/5", {"body": "body"}, None),
        ])

    def test_upsert_posts_when_absent(self):
        c = FakeClient({("POST", "repos/o/r/issues/7/comments"): {"id": 42}})
        self.assertEqual(upsert_comment(c, "o/r", 7, "body"), 42)
        self.assertEqual(c.calls, [
            ("POST", "repos/o/r/issues/7/comments", {"body": "body"}, None),
        ])

    def test_add_eyes_posts_reaction_and_is_best_effort(self):
        c = FakeClient()
        self.assertTrue(add_eyes(c, "o/r", 99))
        self.assertEqual(c.calls, [
            ("POST", "repos/o/r/pulls/comments/99/reactions", {"content": "eyes"}, None),
        ])
        failing = FakeClient({("POST", "repos/o/r/pulls/comments"): RuntimeError("403")})
        self.assertFalse(add_eyes(failing, "o/r", 99))

    def test_minimize_sends_duplicate_mutation_and_is_best_effort(self):
        c = FakeClient()
        self.assertTrue(minimize(c, "NODE_1"))
        method, query, variables, _ = c.calls[0]
        self.assertEqual(method, "GRAPHQL")
        self.assertIn("minimizeComment", query)
        self.assertIn("classifier: DUPLICATE", query)
        self.assertEqual(variables, {"id": "NODE_1"})
        failing = FakeClient({("GRAPHQL", ""): RuntimeError("forbidden")})
        self.assertFalse(minimize(failing, "NODE_1"))

    def test_compare_files_lists_filenames(self):
        c = FakeClient({("GET", "repos/o/r/compare/aaa...bbb"): {
            "files": [{"filename": "src/a.ts"}, {"filename": "src/b.ts"}],
        }})
        self.assertEqual(compare_files(c, "o/r", "aaa", "bbb"), ["src/a.ts", "src/b.ts"])

    def test_compare_files_none_on_error(self):
        c = FakeClient({("GET", "repos/o/r/compare"): RuntimeError("404")})
        self.assertIsNone(compare_files(c, "o/r", "aaa", "bbb"))

    def test_reply_in_thread_path(self):
        c = FakeClient()
        self.assertTrue(reply_in_thread(c, "o/r", 7, 99, "thanks"))
        self.assertEqual(c.calls, [
            ("POST", "repos/o/r/pulls/7/comments/99/replies", {"body": "thanks"}, None),
        ])

    def test_reply_in_thread_false_on_error(self):
        c = FakeClient({("POST", "repos/o/r/pulls/7/comments/99/replies"): RuntimeError("422")})
        self.assertFalse(reply_in_thread(c, "o/r", 7, 99, "thanks"))


class DefaultClientTest(unittest.TestCase):
    def test_default_client_prefers_gh(self):
        env = {"GITHUB_TOKEN": "tok"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("quorum_github.shutil.which", return_value="/usr/bin/gh"):
            self.assertIsInstance(default_client(), GhClient)

    def test_default_client_falls_back_to_token(self):
        for var in ("GITHUB_TOKEN", "GH_TOKEN"):
            with mock.patch.dict(os.environ, {var: "tok"}, clear=True), \
                    mock.patch("quorum_github.shutil.which", return_value=None):
                client = default_client()
                self.assertIsInstance(client, TokenClient)
                self.assertEqual(client.token, "tok")

    def test_default_client_raises_without_gh_or_token(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("quorum_github.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "No gh CLI and no GITHUB_TOKEN"):
                default_client()


class GhClientTest(unittest.TestCase):
    def run_api(self, stdout, *args, **kwargs):
        with mock.patch("quorum_github.subprocess.run",
                        return_value=SimpleNamespace(stdout=stdout)) as run:
            result = GhClient().api(*args, **kwargs)
        return result, run

    def test_post_sends_json_body_on_stdin(self):
        result, run = self.run_api('{"id": 42}', "POST", "repos/o/r/issues/7/comments",
                                   {"body": "hi"})
        self.assertEqual(result, {"id": 42})
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["gh", "api", "-X", "POST", "repos/o/r/issues/7/comments",
                                "--input", "-"])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"body": "hi"})

    def test_get_without_body_and_empty_stdout_is_none(self):
        result, run = self.run_api("", "GET", "repos/o/r/pulls/7")
        self.assertIsNone(result)
        self.assertEqual(run.call_args.args[0],
                         ["gh", "api", "-X", "GET", "repos/o/r/pulls/7"])
        self.assertIsNone(run.call_args.kwargs["input"])

    def test_query_params_other_than_paginate_go_in_the_path(self):
        _, run = self.run_api("[]", "GET", "repos/o/r/pulls/7/comments",
                              params={"per_page": 50, "paginate": True})
        argv = run.call_args.args[0]
        self.assertIn("repos/o/r/pulls/7/comments?per_page=50", argv)
        self.assertNotIn("paginate=True", " ".join(argv))

    def test_paginate_parses_a_stream_of_concatenated_json_values(self):
        # `--jq '.[]'` may print values compact, pretty-printed or back to back.
        stdout = '{"id": 1}\n{\n  "id": 2,\n  "body": "x"\n}{"id": 3}\n'
        result, run = self.run_api(stdout, "GET", "repos/o/r/issues/7/comments",
                                   params={"paginate": True})
        self.assertEqual(result, [{"id": 1}, {"id": 2, "body": "x"}, {"id": 3}])
        argv = run.call_args.args[0]
        self.assertIn("--paginate", argv)
        self.assertEqual(argv[argv.index("--jq") + 1], ".[]")

    def test_paginate_with_no_output_is_an_empty_list(self):
        result, _ = self.run_api("", "GET", "repos/o/r/issues/7/comments",
                                 params={"paginate": True})
        self.assertEqual(result, [])

    def test_graphql_passes_query_and_variables_as_fields(self):
        with mock.patch("quorum_github.subprocess.run", return_value=SimpleNamespace(
                stdout='{"data": {"ok": true}}')) as run:
            result = GhClient().graphql("query Q", {"id": "NODE_1"})
        self.assertEqual(result, {"data": {"ok": True}})
        self.assertEqual(run.call_args.args[0],
                         ["gh", "api", "graphql", "-f", "query=query Q", "-f", "id=NODE_1"])

    def test_failure_raises_runtime_error_with_stderr(self):
        err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404: Not Found")
        with mock.patch("quorum_github.subprocess.run", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404: Not Found"):
                GhClient().api("GET", "repos/o/r/pulls/7")


class TokenClientTest(unittest.TestCase):
    def test_request_shape_and_headers(self):
        with mock.patch("quorum_github.urllib.request.urlopen",
                        return_value=FakeResponse({"id": 42})) as urlopen:
            result = TokenClient("tok").api("POST", "repos/o/r/issues/7/comments", {"body": "hi"})
        self.assertEqual(result, {"id": 42})
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/o/r/issues/7/comments")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"body": "hi"})
        headers = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(headers["authorization"], "Bearer tok")
        self.assertEqual(headers["accept"], "application/vnd.github+json")
        self.assertEqual(headers["x-github-api-version"], "2022-11-28")

    def test_empty_response_is_none(self):
        with mock.patch("quorum_github.urllib.request.urlopen",
                        return_value=FakeResponse(None, status=204)):
            self.assertIsNone(TokenClient("tok").api("POST", "repos/o/r/x"))

    def test_paginate_follows_pages_until_a_short_page(self):
        pages = [FakeResponse([{"id": i} for i in range(100)]), FakeResponse([{"id": 100}])]
        with mock.patch("quorum_github.urllib.request.urlopen", side_effect=pages) as urlopen:
            result = TokenClient("tok").api("GET", "repos/o/r/issues/7/comments",
                                            params={"paginate": True})
        self.assertEqual([c["id"] for c in result], list(range(101)))
        urls = [call.args[0].full_url for call in urlopen.call_args_list]
        self.assertEqual(urls, [
            "https://api.github.com/repos/o/r/issues/7/comments?per_page=100&page=1",
            "https://api.github.com/repos/o/r/issues/7/comments?per_page=100&page=2",
        ])

    def test_http_error_raises_runtime_error_with_status_and_body(self):
        err = urllib.error.HTTPError("https://api.github.com/x", 403, "Forbidden", {},
                                     io.BytesIO(b'{"message": "rate limited"}'))
        with mock.patch("quorum_github.urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, r"403.*rate limited"):
                TokenClient("tok").api("GET", "repos/o/r/pulls/7")

    def test_graphql_posts_query_and_variables(self):
        with mock.patch("quorum_github.urllib.request.urlopen",
                        return_value=FakeResponse({"data": {"ok": True}})) as urlopen:
            result = TokenClient("tok").graphql("query Q", {"id": "NODE_1"})
        self.assertEqual(result, {"data": {"ok": True}})
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.github.com/graphql")
        self.assertEqual(json.loads(req.data), {"query": "query Q", "variables": {"id": "NODE_1"}})

    def test_graphql_errors_raise(self):
        with mock.patch("quorum_github.urllib.request.urlopen",
                        return_value=FakeResponse({"errors": [{"message": "no such node"}]})):
            with self.assertRaisesRegex(RuntimeError, "no such node"):
                TokenClient("tok").graphql("query Q", {})


if __name__ == "__main__":
    unittest.main()
