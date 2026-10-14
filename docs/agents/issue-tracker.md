# Issue tracker: Local Markdown

Issues and specs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`, never a single combined tickets file
- Triage state is recorded as a `Status:` line near the top of each issue file (see `triage-labels.md` for the role strings)
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## Subproject ownership

Before `/to-tickets` drafts or publishes tickets, assign each ticket exactly one
owning subproject. Record `Subproject: <name>` near the top of each local ticket
and show it in the proposed breakdown. Use the canonical checkout name below,
not a feature worktree name. Ownership categorizes the work; tickets stay in the
workspace's shared `.scratch/<feature-slug>/issues/` directory.

| Subproject | Work it owns |
| --- | --- |
| `llvm-project` | Clang HLSL implementation, including Sema and HLSL codegen; LLVM transformations and DirectX/DXIL or SPIR-V target code generation; associated compiler regression tests. |
| `offload-test-suite` | Offload tests, shaders, pipelines, result checks, offloader implementation and test infrastructure. |
| `offload-golden-images` | New or updated reference images used by offload tests. |
| `DirectXShaderCompiler` | DXC implementation changes and its associated compiler tests. |
| `wg-hlsl` | Human-written implementation-design proposals for HLSL-related features, including offload infrastructure designs. |
| `tc57` | Proposals for HLSL language-specification changes and specification edits. |
| `hlsl-dev` | Workspace CLI, environment, orchestration and workspace-local documentation. |

For another subproject, inspect its role and use its canonical checkout name.
Consult `hlsl-specs` as required by the feature-planning policy; new language
specification proposals belong to `tc57` unless the requested deliverable
explicitly changes `hlsl-specs` itself.

Each ticket assigns changes only in its owning subproject. Split work spanning
repositories into separately verifiable, linked tickets; each remains a complete
behavioral slice within its repository, including its relevant tests. An LLVM
compiler ticket may run existing offload tests as acceptance evidence without
owning their creation or modification. Keep Clang/LLVM and DXC implementation
work separate even when both generate DXIL or SPIR-V.

Use blocking edges only for real prerequisites. Apply
[HLSL feature planning](hlsl-feature-planning.md) for offload-test-first coverage
and target execution: new offload coverage precedes the relevant Clang
implementation ticket, while DXC support or a DXC pass does not gate it. If an
offload test needs new reference images, make those images a separate prerequisite
to the test's result check. Test and image tickets must be verifiable independently
of the missing Clang behavior; a recorded expected feature failure is evidence,
not a runtime pass. Proposal tickets block implementation only when their design
or language decision genuinely remains unresolved.

Use `ready-for-human` for human-authored proposal tickets and `ready-for-agent`
for fully specified agent-executable work, following [Triage labels](triage-labels.md).
For a real tracker, retain the same explicit subproject field or use its native
project/repository field; local categorization does not authorize upstream
publication.

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a file with one **child** file per ticket.

- **Map**: `.scratch/<effort>/map.md` (the Notes / Decisions-so-far / Fog body).
- **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`grilling`/`task`); a `Status:` line records `claimed`/`resolved`.
- **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
- **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
- **Claim**: set `Status: claimed` and save before any work.
- **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
