# Dependency notes

## npm `overrides` (security pins)

`@cursor/sdk@1.0.18` pulls transitive dependencies with `npm audit` findings and
has no upstream fix yet, so `package.json` pins patched versions via `overrides`:

| Override | Pinned | Consumer (chain) | Scope / why safe | Remove when |
|----------|--------|------------------|------------------|-------------|
| `tar` | `^7.5.10` | `sqlite3 → node-gyp@8 / cacache` | **install-time only** — not in the runtime path; used to build sqlite3's native addon | `@cursor/sdk` updates `sqlite3`/`node-gyp` past `tar` 6 |
| `undici` | `^6.24.0` | `@connectrpc/connect-node@1.7.0` | runtime-inert on Node ≥18 (see below) | connect-node / `@cursor/sdk` ship `undici` ≥6 |
| `@tootallnate/once` | `^2.0.1` | optional install tooling (`http-proxy-agent`) | optional, install-time | transitive bump |

**Verified (2026-06):** a clean `npm ci` on **Node 26** — which has no `sqlite3`
prebuilt, so it forces the native source build that actually exercises `tar` —
succeeds with `tar@7` building the addon; `npm audit --audit-level=moderate`
(against npmjs) → **0 vulnerabilities**.

### Why `undici` 5→6 is runtime-safe
`@connectrpc/connect-node@1.7.0`'s only use of `undici` is a `Headers` polyfill
gated behind `node < 18`:

```js
const undici_1 = require("undici");
if (major < 18) {
  if (typeof globalThis.Headers === "undefined") globalThis.Headers = undici_1.Headers;
}
```

This repo requires Node ≥22, so that branch never runs (the global `fetch` /
`Headers` are used). `undici@6` imports cleanly on Node ≥18.17, and nothing else
consumes it — so the major bump is inert at runtime here.

### Do not
- Delete or loosen these overrides (re-introduces the audit findings).
- Regenerate `package-lock.json` against `registry.npmmirror.com`.

Removal is tracked in #5.

## Registry

Installs and audits use `registry.npmjs.org`, pinned in `.npmrc`.
`registry.npmmirror.com`'s advisory endpoint returns `NOT_IMPLEMENTED`, so
`npm audit` cannot run against it. The lockfile should resolve from this single
registry; if entries drift, run `npm install` with the npmjs registry to converge.
