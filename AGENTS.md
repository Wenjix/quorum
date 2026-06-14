# Agent & contributor notes

Operational guidance for humans and coding agents working in this repo.

## Dependencies / npm

- **Registry:** npmjs, pinned in `.npmrc`. Do not switch to
  `registry.npmmirror.com` or commit a lockfile that resolves from it — its
  advisory endpoint returns `NOT_IMPLEMENTED`, so `npm audit` silently can't run.
- **The `overrides` in `package.json` are intentional security pins** for
  `@cursor/sdk`'s transitive deps (`tar`, `undici`, `@tootallnate/once`) — there
  is no upstream `@cursor/sdk` fix yet. Do not remove or loosen them, and do not
  regenerate the lockfile against another registry. Rationale, verification, and
  removal conditions live in `docs/dependencies.md` (removal tracked in #5).
- **Auditing:** `npm audit --audit-level=moderate` (npmjs registry required).
