---
name: to-human-ticket
description: Agree repository-scoped work items, then validate and independently review concise human tickets.
disable-model-invocation: true
---

# To Human Ticket

Turn ticket text, local files or issue references into actionable work items, not implementations. Read `skill://i-have-adhd` before drafting; apply its actionability rules to ticket bodies, not just chat.

## Process

1. Identify the requested tickets and sections. If a filename and quoted passage identify different tickets, resolve the target before editing either. Read every source ticket in that scope, relevant discussion and [Domain docs](../../../docs/agents/domain.md). Separate required behavior, interfaces, compatibility and coverage goals from illustrative test inputs, plumbing and drafting workflow. Preserve every required behavior and later user clarification without preserving source checklist itemization. Carry verified implementation specifics into Suggested Implementation, not into the brief, criteria or Notes.
2. Inspect the owning implementation/tests and applicable specifications. Verify that each proposed edit location exercises the requested behavior, not merely that the path exists. Verify symbols, responsibilities and source URLs; use the reference lookup below for HLSL methods. For focused wording corrections, reuse verified unchanged context and inspect further only to resolve a semantic question. Delegated research uses read-only scouts returning compact findings; the parent runs commands and writes artifacts.
3. Use `/grilling` to agree unresolved boundaries and materially different scope choices. Keep independently useful deliverables and owning repositories separate. Treat explicit user choices as settled; ordinary editorial corrections do not reopen them. Confirm shared understanding before initial rewriting or material regrouping.
4. Draft one ticket per agreed repository-scoped deliverable. Preserve one-to-one relationships and metadata except readiness normalization; retitle only to meet `Title and brief`. Preserve consolidated source records; retire them or change relationships only when requested. For a batch, pass both gates on one representative draft before expanding the rewrite; a requested user-review checkpoint also requires approval before rewriting the remaining tickets. Stage tracker changes until both gates pass.
5. **Mechanical gate:** run the validator on every output and the worked examples. Resolve every diagnostic; empty selections, missing dependencies and failed commands are not passes. This checks structure and references, not semantics.
6. **Independent review gate:** obtain a `reviewer` agent's checklist-only clarity review before supplying the full draft, following `Checklist-only clarity gate`. Then supply the source requirements, full draft and validator results for semantic review. Request findings with locations and no edits/check-suite runs. Resolve material findings, rerun affected mechanical checks and obtain clearance for both passes before delivery. Self-review or a clean validator result cannot replace either pass.
7. Update the agreed local/tracker destinations and report the source-to-output mapping separately. Record approved scope changes in that mapping. Validate final files after applying changes; the gates are agent workflow, not acceptance criteria to insert into tickets.

## Commands

Use the developer environment (`devenv shell`) or install this skill's tooling dependencies with `python3 -m pip install -r .agents/skills/to-human-ticket/requirements.txt`.

From the workspace root, pass explicit output files/directories, including ignored `.scratch/` drafts:

```sh
python3 .agents/skills/to-human-ticket/scripts/validate.py \
  --examples .agents/skills/to-human-ticket/SKILL.md PATH...
```

Directories are searched recursively for Markdown. Repository ownership comes from the nearest enclosing `llvm-project`, `offload-test-suite` or `wg-hlsl` directory; otherwise pass `--repo` for that batch. Exit 0 means mechanically clean, 1 means document findings, and 2 means an invocation/input error. Semantic review remains mandatory.

When changing this skill or its tooling, also run its existing regression suite:

```sh
python3 -m pytest -q .agents/skills/to-human-ticket/tests
```

Prefer full commit-SHA source URLs. If a named reference contains slashes, percent-encode them (for example `release%2F20.x`); ambiguous named-reference label mismatches produce `GITHUB_REVISION` rather than a guessed file-path correction.

## Reference discovery

Resolve each HLSL receiver/method through the official navigation index before broad web searches:

```sh
python3 .agents/skills/to-human-ticket/scripts/hlsl_reference.py Texture2D GetDimensions
```

The helper prints exact receiver-scoped URLs, not the full index. `--toc PATH` uses a cached official JSON index offline. Exit 1 means no exact method entry: inspect the official object/reference pages before selecting a verified shared fallback. Exit 2 means a lookup/input failure, not absence of documentation. Read returned pages to verify content; an indexed URL alone does not establish semantics.

Use actual repository remotes and existing revisions for GitHub source links. Pin each link to a commit on the upstream repository's default branch, never to a local or fork-only commit: a submodule checkout can sit on a fork branch. Check with `git merge-base --is-ancestor <commit> <upstream default branch>`. Read-only scouts return facts and citations through their result; use `agent://` output directly rather than requesting files or commands their tools cannot provide.

## Ticket structure

A ticket has these parts, in this order. `## Notes` and its `### Suggested Implementation` subsection are optional; the three `##` headings are the only level-two sections.

```markdown
**Filing metadata.** Delete this block, through the line below it, before you file the issue on GitHub.

- **Repository:** llvm/llvm-project
- **Parent:** llvm/wg-hlsl#490 `[deliverable] Texture Query Methods`
- **Blocked by:** none
- **Status:** ready
- **Estimate:** 2 days

---

# [HLSL][DirectX][SPIRV] Title

Brief.

## Acceptance Criteria

## Notes

### Suggested Implementation

<details>
<summary>Show details</summary>

#### HLSL Interface

</details>

## References

<details>
<summary>HLSL</summary>

- [Link label] — Brief description of the document.

</details>

_Disclaimer: This issue was written with the help of AI and may contain some inaccuracies._

[Link label]: https://example.com/page#section
```

Every `<details>` block is a GitHub [collapsed section](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/organizing-information-with-collapsed-sections). Keep the blank line after `</summary>` and before `</details>`: GitHub renders the Markdown between them only with these blank lines.

The body states the current work only. Revision history, superseded requirements and discussion stay out of it. State each requirement once in the brief, criteria and Notes; repeat names only for clarity. Keep each ticket repository-scoped and retain substantive numerical acceptance, with expectations independent of future compiler outcomes. Delete empty optional sections and their headings.

### Filing metadata

GitHub records the repository, parent, blockers, status and estimate as issue or project fields, not body text. The metadata block comes first so that the person who files the issue can set those fields and then delete the block in one cut. It holds these bullets, in this order:

- **Existing issue:** optional; only when the ticket replaces a published issue. Write `owner/repo#N (replace its title and body)`, or `owner/repo#N (replace its body)` when the published title already meets `Title and brief`.
- **Repository:** the owning repository as `owner/name`.
- **Parent:** the issue this ticket becomes a sub-issue of, as `owner/repo#N` followed by its title.
- **Blocked by:** each blocking issue by repository and exact title. Add `owner/repo#N` when the blocker already exists; otherwise write "file this issue first". Write `none` when nothing blocks the ticket. With several blockers, put one nested bullet under `Blocked by` for each. List genuine prerequisites only, never preferred ordering.
- **Status:** normalize `ready-for-agent` to `ready`; preserve other states.
- **Estimate:** the hands-on time for one engineer who knows the repository but not this feature. Count reading the references, implementing, writing and running the tests, and addressing review comments. Leave out time spent waiting for review, CI or blockers. Write `N hours` for less than a day; otherwise write `N days` or a range such as `2–3 days`, where a day is about six hours of focused work. Base the estimate on the criteria and the verified edit locations. For a proposal ticket, count drafting and revisions, not working-group review time.

Write every issue title in backticks, such as `` `[DirectX][Vulkan] Test …` ``: an unquoted title prefix would render as a link. When the title itself contains backticks, use double backticks with inner spaces: ``` `` Add tests for `CalculateLevelOfDetail` `` ```.

### Title and brief

- Use a standalone descriptive `#` title. Apply compiler prefixes `[HLSL][DirectX][SPIRV][NFC]`, or offload API prefixes `[DirectX][Vulkan][Metal]`, only for work assigned by this ticket. Put no spaces between prefixes. Proposal tickets follow `Proposal tickets`.
- A reader must understand the title without opening the ticket. Name the methods, operations or feature with HLSL names or with the terms that the DirectX and Vulkan documentation use. Replace project shorthand, such as "ordinary pixel LOD", with those terms; add a term to the root glossary when the ticket needs one. Reuse a source title only when it meets these rules.
- Leave out of the title and brief what the prefixes already say, such as "in Clang", "in Clang/LLVM", "in the offload test suite" or "Run the tests on DirectX and Vulkan".
- The brief is one short paragraph that states the deliverable in short sentences, one action per sentence. A separate optional paragraph may name a genuine capability prerequisite, never cross-ticket ordering or another implementation's future outcomes.
- Use reference-style links with definitions at the end. GitHub file labels are full repository-root paths, not basenames. A file in another repository than the ticket's starts with that repository and a colon: `microsoft/DirectXShaderCompiler:docs/DXIL.rst`. When a link points into a document, the label shows where: `path/to/file.cpp:L10-15` for a GitHub line range, `path/to/doc.md#section` for a GitHub section, and the section or symbol name for other pages, such as `[OpImageQueryLod]`. Existing files get verified URLs; proposed new paths are inline code marked new, never invented URLs. Keep local-file/local-ticket links out of ticket bodies.

### Acceptance Criteria

The criteria stand on their own: a reader who skips Notes and References still knows every goal and its subject. Each checkbox is one high-level goal: the outcome a reviewer can check, the subject it acts on (operation, interface, test or document) and the files the work touches. Leave the implementation to the reader: functions, helpers, lowering steps, test inputs, case lists and call sequences stay out. Group checkboxes by coherent work; there is no numeric limit per group or ticket.

- **One goal per subject:** group case variants under one goal: “Add the `SamplerComparisonState` overloads,” not a checkbox for each type, arity or rejection case.
- **Clear subjects:** name the operation, method or interface; referents must be clear from the title, brief, headings or adjacent criteria, never from a Notes table. Name new builtins, intrinsics and methods by their names, not by a relation such as “the queries of the new overloads”. Counts alone do not establish scope.
- **Justified files:** name a file only when the goal changes it. When the ticket has Suggested Implementation, say there what changes in every file that a criterion names, documentation included; the validator reports `UNJUSTIFIED_FILE` otherwise. A test file is justified by its `#####` subsection under `#### Test Cases`. For a documentation file, say what the document already covers and what is missing or wrong.
- **Established new work:** inspect current code and tests before assigning implementation work. Assign changes only for verified gaps or demonstrated failures; omit steps already implemented, even when they belong to the feature's normal path. When existing code supports new behavior, assign the needed new surface and tests instead. Omit routine preservation chores: keeping existing tests passing, retaining unrelated checks or ensuring unchanged propagation is baseline correctness, not ticket work.
- **Goal-level detail:** a criterion carries a detail only when the goal is ambiguous without it, such as a required boundary or numeric limit. Omit inherent correctness, such as a method acting on its receiver.
- **Compiler tests:** write one criterion per test file to add or extend. The title prefixes imply the expected kinds of test: `[HLSL]` implies Sema and HLSL codegen tests, `[DirectX]` DXIL lowering tests and `[SPIRV]` SPIR-V lowering tests. For those, name only the kind and the file: “Add HLSL codegen tests to `clang/test/CodeGenHLSL/resources/Textures-GetDimensions.hlsl`.” When a test file has a narrower purpose than its kind, state the exact requirement it checks instead of restating the kind: “Add Sema tests to `clang/test/SemaHLSL/Resources/Textures-derivative-availability.hlsl` that allow the LOD queries only in pixel shaders and, from SM6.6, in compute shaders.” Leave out negative shader flag tests, per [Test scope and layout](../../../docs/agents/hlsl-feature-planning.md#test-scope-and-layout).
- **Offload tests:** the offload repository makes offload tests the expected kind, so name the methods or behavior under test and the file: “Add checks of the mip-count outputs of nonzero-mip `GetDimensions` calls to [test/Feature/Textures/GetDimensions.test].” Assign only checks that meet [Offload check scope](../../../docs/agents/hlsl-feature-planning.md#offload-check-scope). When a source requirement fails that scope, leave it out and record it in the source-to-output mapping.
- **Test files:** choose files per [Test scope and layout](../../../docs/agents/hlsl-feature-planning.md#test-scope-and-layout). What the tests exercise goes in `#### Test Cases`; the implementer decides which cases are representative.
- **Documentation:** pick targets per [Documentation targets](../../../docs/agents/hlsl-feature-planning.md#documentation-targets). A documentation criterion names the behavior to document and an existing document whose subject covers it; read that document before assigning it.

**Assumed practice:** standard offload-test-first development, running existing compiler/offload tests, routine coverage/ownership summaries, independent compiler assessment and honest reporting are defaults, not checkbox work. Compiler-feature criteria name feature behaviors and new regression checks; list runtime-test authoring, setup or evidence work only when that work is specifically assigned.

### Proposal tickets

A `llvm/wg-hlsl` ticket asks a person to write or amend a proposal. It assigns no code and no tests, so the test rules above do not apply. Keep its `ready-for-human` status.

- **Title:** “Write a proposal for …” for a new proposal; “[NNNN] Describe …” for an amendment to proposal NNNN. A later part of a proposal that has no number yet uses no prefix: “Write the … part of the … proposal”. Use no other prefix.
- **Separately approvable parts:** when other work needs one part of a proposal before the rest, write one ticket per part. All of them edit the same proposal document. The first ticket creates the document; each later ticket is blocked by it and adds its own section. Downstream tickets are blocked by the part they need.
- **First criterion:** name the file. A new proposal is `proposals/NNNN-<name>.md` (new), drafted from the wg-hlsl template with its Introduction, Motivation and Proposed solution sections. An amendment or a later part names the proposal file and the section it adds or changes.
- **Design criteria:** write one criterion per question the proposal must answer: “Define how …” or “Decide whether …”. Keep the answers, implementation mechanisms and any unconfirmed constraints, such as “without the compiler under test”, out of the criteria. Put recommended answers in `#### Recommendations` and the trade-offs in `#### Alternatives`.
- **Examples criterion:** require the paired HLSL, YAML and expected-result examples the proposal must show; list them in `#### Examples`.
- **Last criterion:** merge the proposal after working-group review.
- **Technology subsections:** in Suggested Implementation, `#### DirectX`, `#### Vulkan` and the other technology subsections hold the API facts the design depends on.
- **References:** link the wg-hlsl template only from the first criterion. The ticket still has `## References` for its other sources.

### Notes

Add `## Notes` for essential gotchas, known bugs and edge cases that the linked references do not cover, such as a repository-specific bypass, a current compiler defect or a non-obvious configuration constraint. Write brief point-form bullets of one or two short sentences. Use a compact table only when one gotcha has several cases. Restated references, background, coverage inventories, test recipes, approved-scope remarks and lit gates that the linked tests already declare (`XFAIL`, `UNSUPPORTED`, `REQUIRES`) stay out of these bullets.

### Suggested Implementation

Add `### Suggested Implementation` as the last part of `## Notes` when the source agent ticket carries verified specifics that the criteria leave to the implementer. `## Notes` may hold only this subsection. Write the brief, criteria and Notes bullets as if the subsection does not exist: they never refer to it, and deleting it removes no goal. The criteria alone decide completion. The subsection holds exactly one collapsed block with the summary `Show details`.

- **Derived and verified:** take every item from the source agent ticket and check it against current code and the References links. Add no scope. Drop source items that are wrong, already implemented or ticket history.
- **Specific requirements:** enumerate concrete operands, opcodes, operations, capabilities, flag bits, diagnostics, type and stage boundaries and edge cases as bullets or numbered steps. Use no checkboxes: GitHub counts them as task progress.
- **Aggregating tables:** collect facts scattered across references into compact tables, such as coordinates per receiver, an operation's operand layout or flag bits with their set and clear conditions. Link each table's source with a reference-style link. Refer to a table by its subsection, such as "the flag table under DirectX".
- **Layout:** use `#### HLSL Interface` for the source-level interface. An offload ticket then adds `#### Expected results` for how to derive expected values. Next comes one `####` subsection per technology with assigned work (`DirectX`, `SPIR-V`, `Vulkan`, `Metal`, `Offload test suite`), then `#### Test Cases`. A proposal ticket replaces `#### Test Cases` with `#### Recommendations`, a table of each design question and its recommended answer; `#### Alternatives`, a table of the options for each question with their pros and cons, for the template's "Alternatives considered" section; and `#### Examples`, the examples the proposal should show. A recommendation is not a decision.
- **Test cases:** under `#### Test Cases`, add one `#####` subsection per test file named in the criteria, titled with that file's path. Enumerate every case and dimension worth considering for that file, such as receivers, element types, shader models, stages, targets and error conditions. The implementer chooses the representative subset.
- **Portable:** the link and wording rules of the whole ticket apply. Name other tickets by exact title. Keep local paths, local ticket numbers and planning history out.

### References

`## References` holds every link the ticket needs to implement or verify its goals. Each subsection is one collapsed block whose summary is the subsection name:

| Subsection | Link to |
| --- | --- |
| `HLSL` | Language/method references, proposals and shader-model requirements |
| `DirectX`, `SPIR-V` | Compiler work: native instructions, operand/result mappings, capabilities and metadata |
| `DirectX`, `Vulkan`, `Metal` | Runtime work: resource/sampler configuration, device capabilities and query semantics |
| `Offload test suite` | Test configuration, verdicts and numerical comparison documentation shared by both API runs |

Include a technology subsection only for technology-specific implementation, testing or references assigned by the ticket. Related specifications, shared HLSL semantics and applicability to a backend do not establish backend work.

Each reference is one bullet: the link, an em dash and the briefest description that identifies the document, such as "DXIL `CalculateLOD` operation, opcode 81" or "HLSL method reference". Point the link at the relevant part, a line range, section, symbol, feature or configuration key, and show it in the label. Find the most specific official receiver/method page; use a shared reference only when official sources offer no specific entry. A method that Microsoft documents once for all receivers, such as `CalculateLevelOfDetail`, uses that shared page even when the reference lookup finds no receiver-scoped entry. Include proposals/shader-model sources for overloads or stages older references omit. Leave explanations, signatures, semantics and examples at the links or in Suggested Implementation.

### Disclaimer

End the visible body with this exact line, after References and before the link definitions:

```markdown
_Disclaimer: This issue was written with the help of AI and may contain some inaccuracies._
```

### STE-inspired wording

Apply these [STE-inspired rules](https://www.asd-ste100.org/about_STE.html) to every part of the ticket, including tables and worked examples. These are local writing requirements, not a claim of full ASD-STE100 compliance.

- Use short, active sentences. Name the actor, action and object when the context does not make them clear.
- Give one instruction per sentence. Split unrelated actions into separate criteria or numbered steps.
- Use concrete verbs and explicit objects. For example: “Add the `SamplerComparisonState` overloads to `clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp`.”
- Use one term for each concept. Keep API names and code identifiers exact. Define unfamiliar technical terms where they are first used.
- State a condition or expected result when it distinguishes the requested work; otherwise point to the defining rule or reference. Use plain sentences instead of slash-joined case lists, dense noun phrases and unclear pronouns.
- In the brief and Notes, write a known defect as a statement of fact, then the instruction that fixes it: “The SPIR-V lowering fails when the shader selects the sampler at run time. Make this lowering work for those samplers.” A criterion states only the goal: “Make both methods lower to valid SPIR-V for `TextureCubeArray` in …”. Put each condition in its own sentence or in a leading clause, not in a trailing qualifier such as “also when”.

### Representative coverage

- Test scope follows [Test scope and layout](../../../docs/agents/hlsl-feature-planning.md#test-scope-and-layout), even when a source issue lists an exhaustive matrix. Criteria name the test file and its kind or purpose; `#### Test Cases` enumerates the candidate cases. Record any coverage a source required that the ticket now leaves to representative selection in the source-to-output mapping.
- Prefer links to existing test examples over suggested recipes. Suggested test inputs, such as dimensions, mip counts, sample values and call ordering, belong only in Suggested Implementation. Keep required numeric limits and tolerances explicit in the criteria.
- Honor explicit user coverage decisions; record approved changes in the source-to-output mapping rather than silently weakening acceptance or reinstating superseded source requirements.

## Semantic review checklist

Complete the checklist-only clarity gate first. Then review the entire active ticket: filing metadata, title, brief, criteria, Notes, Suggested Implementation, References, tables and captions. Identify unclear subjects, unexplained shorthand, anonymous counted sets and unclear cross-references wherever they occur. Trace every source requirement through the full body and references. Ambiguity or ticket history anywhere is a material finding even when coverage is complete and the validator passes.

### Checklist-only clarity gate

1. Read only the title, brief, acceptance headings and checkboxes. A reader must know which goal to reach, what subject it acts on and which files the work touches, from those elements alone. Judge the working set of the whole checklist, not only sentence length: it must remain a goal list.
2. Give the reviewer that excerpt and the `Acceptance Criteria` rules first, without full draft paths, body text or source records. Require a brief per-criterion verdict, quoting the goal, subject and touched files. Flag implementation recipes, references to Notes, bundled inventories, case-by-case fragmentation and unnecessary detail as findings; missing detail is not a reason to expand a checkbox into a specification.
3. Resolve the clarity findings before supplying the full draft and sources for whole-ticket semantic review. Require the reviewer to inspect every active section, including prose and table cells. Keep review notes outside ticket bodies.

### Whole-ticket semantic review

- Does every checkbox request verified missing work or concrete new test coverage in the owning repository, rather than already-implemented steps, obvious behavior, speculative repairs, preservation chores, reporting or cross-ticket promises?
- Are required behavior, interfaces, compatibility and numerical acceptance retained through goals and references, with tests sized and laid out per `Test scope and layout`?
- Does every documentation criterion target an existing document whose subject covers the behavior, per `Documentation targets`?
- Do the Notes bullets hold only essential gotchas, bugs and edge cases absent from the linked references?
- Do the brief, criteria and Notes bullets stand alone without Suggested Implementation, and does that subsection hold only verified, source-derived specifics with no new scope? Does it say what changes in every file that a criterion names, including what a documentation target is missing? Does `#### Test Cases` cover every test file the criteria name?
- Does every test criterion whose kind the title prefixes do not imply state its purpose?
- Does References contain every needed link, in subsections that match the work the ticket assigns?
- Can a reader understand the title without opening the ticket, and does the filing metadata name every parent and blocker clearly enough to set them on GitHub?
- Do verified edit locations exercise the requested feature, with accurate method calls, configuration names, expected results and specific references, and proposed paths clearly marked new?
- Does every part follow `Ticket structure` and `STE-inspired wording`?

For multiple independent slices, review them in parallel; give each reviewer the same agreed contract. Keep judgements here rather than adding global steering rules or prose-pinning tests.

## Project references

Apply [Domain docs](../../../docs/agents/domain.md) for vocabulary. Use `/domain-modeling` only for distinctions primary sources cannot settle; put agreed project terms in the root glossary, not submodules.

For HLSL/compiler/DirectX/DXIL/Vulkan/SPIR-V work, apply [HLSL feature planning](../../../docs/agents/hlsl-feature-planning.md) to source selection and compiler implementation verification. Consult all three proposal collections and distinguish proposals, adopted behavior and observed results. Offload-test tickets still describe their own deliverables, not future compiler outcomes.

## Worked examples

These illustrate ticket shape, Notes, Suggested Implementation and conditional References subsections; they are not implementation requests. Four-backtick outer fences let the validator check complete examples.

### Focused offload test

````markdown repo=offload-test-suite
**Filing metadata.** Delete this block, through the line below it, before you file the issue on GitHub.

- **Repository:** llvm/offload-test-suite
- **Parent:** llvm/wg-hlsl#490 `[deliverable] Texture Query Methods`
- **Blocked by:** none
- **Status:** ready
- **Estimate:** 2 hours

---

# [DirectX][Vulkan] Check Texture2D mip counts at nonzero levels

Check the mip-count outputs of the existing nonzero-mip `Texture2D` `GetDimensions` calls. Run the checks on DirectX and Vulkan.

## Acceptance Criteria

- [ ] Add checks of the mip-count outputs of nonzero-mip `GetDimensions` calls to [test/Feature/Textures/GetDimensions.test].

## Notes

- `GetDimensions` reports the mip count of the bound view. It equals the `MipLevels` setting only when the view covers every mip level.

## References

<details>
<summary>HLSL</summary>

- [Texture2D::GetDimensions] — HLSL method reference.

</details>

<details>
<summary>Vulkan</summary>

- [OpImageQuerySizeLod] — SPIR-V size query for a selected mip level.

</details>

<details>
<summary>Offload test suite</summary>

- [docs/MipMappedTextures.md#defining-mip-levels] — Mip-chain configuration of test resources.

</details>

_Disclaimer: This issue was written with the help of AI and may contain some inaccuracies._

[test/Feature/Textures/GetDimensions.test]: https://github.com/llvm/offload-test-suite/blob/6620440795289335e9d5f84fb38bb78359a98552/test/Feature/Textures/GetDimensions.test
[docs/MipMappedTextures.md#defining-mip-levels]: https://github.com/llvm/offload-test-suite/blob/6620440795289335e9d5f84fb38bb78359a98552/docs/MipMappedTextures.md#defining-mip-levels
[OpImageQuerySizeLod]: https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html#OpImageQuerySizeLod
[Texture2D::GetDimensions]: https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions
````

### Compiler interface extension without backend changes

````markdown repo=llvm-project
**Filing metadata.** Delete this block, through the line below it, before you file the issue on GitHub.

- **Repository:** llvm/llvm-project
- **Parent:** llvm/wg-hlsl#491 `[deliverable] Comparison Sampling`
- **Blocked by:** none
- **Status:** ready
- **Estimate:** 1–2 days

---

# [HLSL] Add SamplerComparisonState overloads of the LOD queries

Add `SamplerComparisonState` overloads of `CalculateLevelOfDetail` and `CalculateLevelOfDetailUnclamped` to Clang HLSL.

## Acceptance Criteria

- [ ] Add the `SamplerComparisonState` overloads to [clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp].
- [ ] Add Sema tests to [clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl].

## Notes

### Suggested Implementation

<details>
<summary>Show details</summary>

#### HLSL Interface

| Method | Parameters | Result |
| --- | --- | --- |
| `CalculateLevelOfDetail` | `SamplerComparisonState S`, `vector<float, CoordsCt> Location` | `float`, clamped to the valid mip range |
| `CalculateLevelOfDetailUnclamped` | `SamplerComparisonState S`, `vector<float, CoordsCt> Location` | `float`, not clamped |

- Add both overloads in `addCalculateLodMethods` in `clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp`, beside the `SamplerState` overloads, for every texture type that has those. `Texture2DMS` and `Texture2DMSArray` get neither.
- Allow both overloads only in pixel and compute shaders.
- Below SM6.8, report an error that names the required shader model.
- Lower both overloads like the `SamplerState` overloads. The proposal's [DXIL additions][comparison lowering] reuse `dx.op.calculateLOD` and `OpImageQueryLod`.

#### Test Cases

##### `clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl`

- Both overloads on each texture type that has them.
- SM6.8 and SM6.7 profiles.
- Pixel, compute and each unsupported shader stage.
- `Texture2DMS` and `Texture2DMSArray`, which have no overloads.

</details>

## References

<details>
<summary>HLSL</summary>

- [0014 expanded comparison sampling: HLSL additions][comparison overloads] — New comparison-sampler overloads.

</details>

_Disclaimer: This issue was written with the help of AI and may contain some inaccuracies._

[clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp
[clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl
[comparison overloads]: https://microsoft.github.io/hlsl-specs/proposals/0014-expanded-comparison-sampling/#hlsl-additions
[comparison lowering]: https://microsoft.github.io/hlsl-specs/proposals/0014-expanded-comparison-sampling/#dxil-additions
````
