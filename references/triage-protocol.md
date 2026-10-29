**Protocol** (for an agent without the Quorum skill)

1. `agreed` clusters: do not re-prove. Establish the missing invariant, sweep for sibling sites, write a fix brief naming the chokepoint and the test. Report with `escalate`.
2. `solo` clusters: prove or kill. Cheapest reproduction first. Return `valid | partly | invalid` with evidence. Report with `dispose`. `priors` in the JSON is each reviewer's past precision here: a prior, never a verdict.
3. If `mechanism_agreement` is `split`, establish the true mechanism before fixing anything.
4. Skip clusters that already carry a disposition unless it is marked `stale: true`.
5. Report with `python3 scripts/quorum_ledger.py dispose OWNER/REPO PR CLUSTER_ID --verdict valid|partly|invalid|deferred --note "..." [--evidence "..."] [--commit SHA] --reply` or `escalate OWNER/REPO PR CLUSTER_ID --from escalation.json`; Quorum re-renders this comment. Run it from a checkout of https://github.com/Wenjix/quorum, or unpack the scripts with `mkdir -p quorum && gh api repos/Wenjix/quorum/tarball/{ledger_ref} > quorum.tgz && tar -xzf quorum.tgz -C quorum --strip-components=1` and run `python3 quorum/scripts/quorum_ledger.py ...` (the `scripts/` and `references/` directories are needed together).
