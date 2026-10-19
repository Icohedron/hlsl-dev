---
name: to-human-ticket
description: Agree repository-scoped work items, then validate and independently review concise human tickets.
disable-model-invocation: true
---

# To Human Ticket

Turn ticket text, local files or issue references into actionable work items, not implementations. Read `skill://i-have-adhd` before drafting; apply its actionability rules to ticket bodies, not just chat.

## Process

1. Identify the requested tickets and sections. If a filename and quoted passage identify different tickets, resolve the target before editing either. Read every source ticket in that scope, relevant discussion and [Domain docs](../../../docs/agents/domain.md). Separate required behavior, interfaces, compatibility and coverage goals from illustrative test inputs, plumbing and drafting workflow. Account for every source requirement and later user clarification.
2. Inspect the owning implementation/tests and applicable specifications. Verify that each proposed edit location exercises the requested behavior, not merely that the path exists. Verify symbols, responsibilities and source URLs; use the reference lookup below for HLSL methods. For focused wording corrections, reuse verified unchanged context and inspect further only to resolve a semantic question. Delegated research uses read-only scouts returning compact findings; the parent runs commands and writes artifacts.
3. Use `/grilling` to agree unresolved boundaries and materially different scope choices. Keep independently useful deliverables and owning repositories separate. Treat explicit user choices as settled; ordinary editorial corrections do not reopen them. Confirm shared understanding before initial rewriting or material regrouping.
4. Draft one ticket per agreed repository-scoped deliverable. Preserve one-to-one titles, relationships, metadata and discussion except readiness normalization. Preserve consolidated source records; retire them or change relationships only when requested. For a batch, pass both gates on one representative draft before expanding the rewrite; a requested user-review checkpoint also requires approval before rewriting the remaining tickets. Stage tracker changes until both gates pass.
5. **Mechanical gate:** run the validator on every output and the worked examples. Resolve every diagnostic; empty selections, missing dependencies and failed commands are not passes. This checks structure and references, not semantics.
6. **Independent review gate:** request a `reviewer` agent to evaluate the drafts against the agreed requirements and the semantic checklist below. Supply source requirements, draft paths/text and validator results; request findings with locations and no edits/check-suite runs. Resolve material findings, rerun affected mechanical checks and obtain review with no unresolved material findings before delivery. Self-review or a clean validator result cannot replace this gate.
7. Update the agreed local/tracker destinations and report the source-to-output mapping separately. Record approved scope changes in that mapping. Validate final files after applying changes; the gates are agent workflow, not acceptance criteria to insert into tickets.

## Commands

Use the developer environment (`devenv shell`) or install this skill's tooling dependencies with `python3 -m pip install -r .agents/skills/to-human-ticket/requirements.txt`.

From the workspace root, pass explicit output files/directories, including ignored `.scratch/` drafts:

```sh
python3 .agents/skills/to-human-ticket/scripts/validate.py \
  --examples .agents/skills/to-human-ticket/SKILL.md PATH...
```

Directories are searched recursively for Markdown. Repository ownership comes from the nearest enclosing `llvm-project` or `offload-test-suite` directory; otherwise pass `--repo` for that batch. Exit 0 means mechanically clean, 1 means document findings, and 2 means an invocation/input error. Semantic review remains mandatory.

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

Use actual repository remotes and existing revisions for GitHub source links. Read-only scouts return facts and citations through their result; use `agent://` output directly rather than requesting files or commands their tools cannot provide.

## Ticket structure

- Use a standalone descriptive `#` title. Apply relevant compiler prefixes `[HLSL][DirectX][SPIRV][NFC]`, or offload API prefixes `[DirectX][Vulkan][Metal]`, with no spaces between prefixes. Reuse meaningful source titles.
- Follow with one short deliverable-only paragraph. A separate optional paragraph may name genuine capability prerequisites, never cross-ticket ordering or another implementation's future outcomes. Normalize `ready-for-agent` to `ready`; preserve other states/history.
- Put `## Acceptance Criteria` before reference material. Each unchecked checkbox is one short imperative sentence naming real new work and a useful file starting point. Use only as many as needed, at most five per group; small test additions may need one or two.
- Compiler reference sections are `## DirectX`, `## SPIR-V`, `## HLSL`; offload sections are `## DirectX`, `## Vulkan`, `## HLSL`, plus Metal where relevant. Other domains use appropriate technology names.
- Use reference-style links with definitions at the end. GitHub file labels are full owning-repository-root paths, not basenames. Use descriptive IDs when equal paths have different anchors. Existing files get verified URLs; proposed new paths are inline code marked new, never invented URLs. Keep local-file/local-ticket links out of ticket bodies.

### Section responsibilities

| Section | Include |
| --- | --- |
| HLSL | Source syntax, receivers, parameter/result types, evaluation rules, availability and diagnostics, with direct language/method references |
| Compiler DirectX / SPIR-V | Native instructions, operand/result mapping, emitted capabilities and metadata |
| Runtime DirectX / Vulkan | Resource/sampler configuration, device capabilities and API-specific query semantics |
| Offload test coverage | Input cases, expected results and numerical tolerances |

Each section links its own documentation. Find the most specific official receiver/method page individually; use a shared reference only when official sources offer no specific entry. Include proposals/shader-model sources for overloads or stages older references omit. A native-query compiler task selects native operations/results; it does not implement the device's LOD algorithm or runtime-verdict tolerances.

## Concise content

- **Behavior-first criteria:** use short checkboxes, each naming one bounded behavior change, a useful file starting point and an observable result. Prefer “query the chosen texture and write results to the requested variables” over a list of test mechanisms. Keep necessary edge cases in the relevant reference section. Number genuine multi-step instructions.
- **Explicit references:** name the method, texture type, parameter, configuration or output being changed. A criterion may point to a named table for details after identifying the behavior; “generate the signatures in HLSL” leaves the task undefined. Repeat names needed for clarity, not inventories of every edge case. Apply this check to the whole ticket.
- **Established new work:** tie implementation edits to a concrete missing capability or demonstrated failure. When code is already correct, assign the required regression coverage or acceptance outcome instead of a speculative “correct the analysis where it disagrees” task. Retain substantive compatibility requirements; unchanged setup is not additional work.
- **Concrete interfaces and terms:** enumerate actual calls such as `tex.GetDimensions(0, w, h, levels)`, types, argument directions, defaults, availability and result rules. Use plain behavior in criteria. Define internal notation only when the reader must use it; otherwise omit the notation and its explanation. Keep required limits and meanings intact.
- **Documentation:** name the configuration key or API needed for the requirement and link the relevant documentation for syntax and background. Explain a new or changed interface when that interface is the deliverable; ordinary tickets are not schema tutorials, binding walkthroughs or allocation recipes. A documentation checkbox must identify specific behavior to document.

Default to brief point-form explanations and compact tables. Use paragraphs only when bullets would obscure a relationship. Remove repeated rationale, optional recipes and unused background rather than moving them into another long section. State each requirement once; repeat names only for clarity. Keep each ticket repository-scoped and retain substantive numerical acceptance, with expectations independent of future compiler outcomes. Rely on existing CI/test conventions for routine execution and reporting.

### Representative offload coverage

- Separate required **coverage goals** and expected results from optional **suggested cases**. API/signature tables describe the interface; they do not automatically require runtime tests for every texture × overload × output type × mip × stage combination. Cover the agreed behaviors with discriminating representative cases; compiler regression coverage remains a separate requirement.
- Label example setups and test ideas as suggestions, not acceptance checkboxes or a compulsory sequence to repeat for every receiver. Keep real numeric limits and result tolerances mandatory, but leave incidental dimensions, mip counts, sample values and call ordering to the test author. Every selected runtime case still needs independently specified expected results.
- Honor explicit user coverage decisions. If a source demands an exhaustive runtime matrix and the user has not settled that boundary, surface the coverage tradeoff before changing scope. Record approved changes in the source-to-output mapping rather than silently weakening acceptance or reinstating superseded source requirements.

## Semantic review checklist

Review in two passes: first read the title, deliverable and checklist for small, understandable edits; then trace every source requirement through the body and references. A short criterion may delegate detailed cases to a named section, but must state what changes and what success looks like. Ambiguous or overloaded criteria are material findings even when coverage is complete and the validator passes.

- Does every checkbox request a bounded behavior change or concrete test outcome in the owning repository, with established implementation work rather than speculative repairs, preservation chores, reporting or cross-ticket promises?
- Are required behavior, interfaces, compatibility and numerical acceptance retained, with required coverage clearly distinguished from optional examples and no unapproved exhaustive runtime matrix?
- Are language rules, backend/native behavior and runtime test expectations in their proper sections?
- Do verified edit locations exercise the requested feature, with accurate method calls, configuration names, expected results and specific references, and proposed paths clearly marked new?
- Does the presentation follow `Concise content`, with understandable criteria and only the definitions, examples and background needed to perform the work?

For multiple independent slices, review them in parallel; give each reviewer the same agreed contract. Keep judgements here rather than adding global steering rules or prose-pinning tests.

## Project references

Apply [Domain docs](../../../docs/agents/domain.md) for vocabulary. Use `/domain-modeling` only for distinctions primary sources cannot settle; put agreed project terms in the root glossary, not submodules.

For HLSL/compiler/DirectX/DXIL/Vulkan/SPIR-V work, apply [HLSL feature planning](../../../docs/agents/hlsl-feature-planning.md) to source selection and compiler implementation verification. Consult all three proposal collections and distinguish proposals, adopted behavior and observed results. Offload-test tickets still describe their own deliverables, not future compiler outcomes.

## Worked examples

These illustrate ticket shape; they are not additional implementation requests. Four-backtick outer fences let the validator check complete examples, including nested HLSL snippets.

### Focused offload test

````markdown repo=offload-test-suite
# [DirectX][Vulkan] Check Texture2D mip counts at nonzero levels

Check the mip-count outputs of existing nonzero-mip Texture2D GetDimensions calls on DirectX and Vulkan.

**Status:** ready

## Acceptance Criteria

- [ ] Store the uint and float mip-count outputs of nonzero-mip `GetDimensions` calls in [test/Feature/Textures/GetDimensions.test].
- [ ] Add `BufferExact` expectations for the newly stored mip counts in [test/Feature/Textures/GetDimensions.test].

## Offload test coverage

### Coverage goals

- Cover intermediate and final mip queries across representative uint/float cases, without repeating every mip with both output types.

### Expected results

- For explicit mip `m`, expect width `max(1, Width >> m)`, height `max(1, Height >> m)` and total levels `MipLevels`; these names refer to configured `OutputProps` values.
- The total mip count stays unchanged as `m` varies; retain the existing spatial-size checks.
- Omitted-mip calls report mip-zero dimensions without a levels output. Store uint results as numeric floats, not reinterpreted bits.

### Suggested cases

- Optional: query mips `1` and `3` with `Buffers[].OutputProps.MipLevels: 4` ([docs/MipMappedTextures.md]); other legal mip chains can distinguish the same behavior.

## DirectX

- Read-only views expose the configured mip chain ([docs/MipMappedTextures.md]).

## Vulkan

- Expected selected-mip sizes follow [OpImageQuerySizeLod].

## HLSL

Reference: [Texture2D::GetDimensions].

- `tex` is `Texture2D<float4>`; input `mip` is `uint`.
- Each call returns `void`; outputs `w`, `h` and `levels` are either all uint or all float for a call.

```hlsl
tex.GetDimensions(w, h);
tex.GetDimensions(mip, w, h, levels);
```

[test/Feature/Textures/GetDimensions.test]: https://github.com/llvm/offload-test-suite/blob/c90d8bd5d976593af12a4d96316c2fe5af402d9e/test/Feature/Textures/GetDimensions.test
[docs/MipMappedTextures.md]: https://github.com/llvm/offload-test-suite/blob/c90d8bd5d976593af12a4d96316c2fe5af402d9e/docs/MipMappedTextures.md#defining-mip-levels
[OpImageQuerySizeLod]: https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html#OpImageQuerySizeLod
[Texture2D::GetDimensions]: https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions
````

### Compiler interface extension

````markdown repo=llvm-project
# [HLSL][DirectX][SPIRV] Add comparison-sampler pixel LOD overloads

Add comparison-sampler pixel LOD overloads to Clang HLSL.

**Status:** ready

## Acceptance Criteria

- [ ] Add `SamplerComparisonState` overloads for `CalculateLevelOfDetail` and `CalculateLevelOfDetailUnclamped` in [clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp].
- [ ] Route comparison-sampler LOD results through existing native queries in [llvm/lib/Target/DirectX/DXILOpLowering.cpp] and [llvm/lib/Target/SPIRV/SPIRVInstructionSelector.cpp].
- [ ] Add tests accepting SM6.8 comparison calls and rejecting SM6.7 calls in [clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl].

## DirectX

- Emit CalculateLOD opcode 81 with the selected texture and comparison-sampler handles ([docs/DXIL.rst]).
- Set the clamp selector true for `CalculateLevelOfDetail` and false for `CalculateLevelOfDetailUnclamped`.

## SPIR-V

- [OpImageQueryLod] supplies clamped component 0 and unclamped component 1.

## HLSL

Reference: [comparison overloads].

- Both methods return scalar `float`.
- Inputs: `comparisonSampler` is `SamplerComparisonState`; `location` has the spatial type below.
- Require SM6.8 pixel availability, including reached helper calls; reject unsupported shader stages.

```hlsl
float clamped = tex.CalculateLevelOfDetail(comparisonSampler, location);
float unclamped = tex.CalculateLevelOfDetailUnclamped(comparisonSampler, location);
```

- `tex` has one of the receiver types below with an otherwise-valid element type `T`.
- Array locations contain no layer coordinate.

| Receivers | Type of `location` |
| --- | --- |
| `Texture1D<T>`, `Texture1DArray<T>` | `float` |
| `Texture2D<T>`, `Texture2DArray<T>` | `float2` |
| `TextureCube<T>`, `TextureCubeArray<T>` | `float3` direction |

[clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp
[llvm/lib/Target/DirectX/DXILOpLowering.cpp]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/llvm/lib/Target/DirectX/DXILOpLowering.cpp
[llvm/lib/Target/SPIRV/SPIRVInstructionSelector.cpp]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/llvm/lib/Target/SPIRV/SPIRVInstructionSelector.cpp
[clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl
[docs/DXIL.rst]: https://github.com/microsoft/DirectXShaderCompiler/blob/791e06620e6eccad215440ff2af6f37620589115/docs/DXIL.rst#calculatelod
[OpImageQueryLod]: https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html#OpImageQueryLod
[comparison overloads]: https://microsoft.github.io/hlsl-specs/proposals/0014-expanded-comparison-sampling/#hlsl-additions
````
