"""Test doubles shared by the ledger and priors tests."""


class FakeClient:
    """In-memory stand-in for a `quorum_github.Client`.

    `responses` maps `(METHOD, path_prefix)` to the value to return; a value
    that is an `Exception` instance is raised instead. The longest prefix the
    request path starts with wins, and an unmatched call returns None. GraphQL
    calls are keyed `("GRAPHQL", "")`.

    `calls` records every request as `(method, path, body, params)`; a GraphQL
    call is recorded as `("GRAPHQL", query, variables, None)`.
    """

    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def _answer(self, method, path):
        matches = [
            prefix for (m, prefix) in self.responses
            if m == method and path.startswith(prefix)
        ]
        if not matches:
            return None
        value = self.responses[(method, max(matches, key=len))]
        if isinstance(value, Exception):
            raise value
        return value

    def api(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        return self._answer(method, path)

    def graphql(self, query, variables):
        self.calls.append(("GRAPHQL", query, variables, None))
        return self._answer("GRAPHQL", "")
