# Dependency notes

## npm `overrides` (security pins)

`@cursor/sdk@1.0.23` still pulls a transitive dependency with `npm audit`
findings and has no compatible upstream fix yet, so `package.json` pins a
patched version via `overrides`:

| Override | Pinned | Consumer (chain) | Scope / why safe | Remove when |
|----------|--------|------------------|------------------|-------------|
| `undici` | `^6.27.0` | `@connectrpc/connect-node@1.7.0` | runtime-inert on Node ≥18 (see below) | `@cursor/sdk` moves to ConnectRPC 2 or otherwise stops resolving vulnerable Undici 5 |

`@cursor/sdk@1.0.19` removed its `sqlite3` dependency. At 1.0.23, neither
`sqlite3` nor its install-time `tar` / `@tootallnate/once` chains are present,
so their former overrides have been removed.

**Verified (2026-07):** a clean install resolves `@cursor/sdk@1.0.23`,
`@connectrpc/connect-node@1.7.0`, and `undici@6.27.0`; `npm ls` contains no
`sqlite3`, `node-gyp`, `tar`, or `@tootallnate/once`; and
`npm audit --audit-level=moderate` (against npmjs) reports **0 vulnerabilities**.

### Why `undici` 5→6 is runtime-safe
`@connectrpc/connect-node@1.7.0`'s only use of `undici` is a `Headers` polyfill
gated behind `node < 18`:

```js
const undici_1 = require("undici");
if (major < 18) {
  if (typeof globalThis.Headers === "undefined") globalThis.Headers = undici_1.Headers;
}
```

This repo requires Node ≥22.13, so that branch never runs (the global `fetch` /
`Headers` are used). `undici@6` imports cleanly on Node ≥18.17, and nothing else
consumes it — so the major bump is inert at runtime here.

### Do not
- Delete or loosen the Undici override (re-introduces the audit findings).
- Regenerate `package-lock.json` against `registry.npmmirror.com`.

Removal is tracked in #5.

## Registry

Installs and audits use `registry.npmjs.org`, pinned in `.npmrc`.
`registry.npmmirror.com`'s advisory endpoint returns `NOT_IMPLEMENTED`, so
`npm audit` cannot run against it. The lockfile should resolve from this single
registry; if entries drift, run `npm install` with the npmjs registry to converge.
