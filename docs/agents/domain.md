# Domain Docs

Use a single context for this workspace. Root domain docs describe the workspace; submodules follow their own documentation.

## Before exploring, read these

- `CONTEXT.md` at the repo root, if it exists.
- ADRs in `docs/adr/` that touch the area you're about to work in, if any exist.

Proceed silently when these files don't exist. The `/domain-modeling` skill creates them when terms or decisions are resolved; their absence does not require creating them upfront.

## Use the glossary's vocabulary

When naming a domain concept in an issue, proposal, hypothesis, or test, use the term defined in `CONTEXT.md`. If the term is missing, reconsider it or note the gap for `/domain-modeling`.

## Flag ADR conflicts

If your output contradicts an existing ADR, surface the conflict rather than silently overriding it.
