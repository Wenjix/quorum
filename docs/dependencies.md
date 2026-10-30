# Dependency notes

## npm `overrides` (security pins)

`@cursor/sdk@1.2.0` has no vulnerable transitive dependency of its own: the
1.0.x line's `@connectrpc/connect-node` (and with it `undici`), `sqlite3`,
`tar` and `@tootallnate/once` chains are gone. One pin remains, for a peer
dependency the SDK declares rather than installs:

| Override | Pinned | Consumer (chain) | Scope / why safe | Remove when |
|----------|--------|------------------|------------------|-------------|
| `zod` | `^4.6.7` | peer of `@cursor/sdk@1.2.0`, declared `^3.25.0 \|\| ^4.0.0` | the SDK declares support for zod 4; npm would otherwise auto-install the 3.x line, which carries GHSA-mrhx-c7xc-m3w5 (fixed in 4.6.6) | the SDK raises its peer floor to `>=4.6.6`, or drops zod |

Quorum itself never imports `zod`; the pin only chooses which release
satisfies the SDK's peer range.

**Verified (2026-10-29):** a clean install resolves `@cursor/sdk@1.2.0` and
`zod@4.6.7`; `npm ls` contains no `@connectrpc/connect-node`, `undici`,
`sqlite3`, `node-gyp`, `tar` or `@tootallnate/once`; typecheck and the full
test suite pass; and `npm audit --audit-level=moderate` (against npmjs)
reports **0 vulnerabilities**.

### Do not
- Delete or loosen the `zod` override (re-introduces the advisory).
- Regenerate `package-lock.json` against `registry.npmmirror.com`.

Removal is tracked in #5.

## Registry

Installs and audits use `registry.npmjs.org`, pinned in `.npmrc`.
`registry.npmmirror.com`'s advisory endpoint returns `NOT_IMPLEMENTED`, so
`npm audit` cannot run against it. The lockfile should resolve from this single
registry; if entries drift, run `npm install` with the npmjs registry to converge.
