---
name: to-human-ticket
description: Agree repository-scoped work items, then validate and independently review concise human tickets.
disable-model-invocation: true
---

# To Human Ticket

Turn ticket text, local files or issue references into actionable work items, not implementations. Apply `/i-have-adhd` to the ticket bodies, not just chat.

## Process

1. Read every source ticket, relevant discussion and [Domain docs](../../../docs/agents/domain.md). Identify required behavior, interfaces, coverage, compatibility and scope boundaries separately from test-data choices, internal plumbing and drafting workflow. Finish when every source requirement is accounted for.
2. Inspect the owning implementation/tests and applicable specifications. Verify paths, symbols, responsibilities and source URLs. Use the reference lookup below for HLSL methods. If research needs delegation, request compact structured findings from read-only scouts; the parent performs shell commands and artifact writes, not the scout.
3. Use `/grilling` to agree unresolved boundaries and materially different scope choices. Keep independently useful deliverables and owning repositories separate. Treat explicit user choices as settled; ordinary editorial corrections do not reopen them. Confirm shared understanding before initial rewriting or material regrouping.
4. Draft one ticket per agreed repository-scoped deliverable. Preserve one-to-one titles, relationships, metadata and discussion except readiness normalization. Preserve consolidated source records; retire them or change relationships only when requested. Stage tracker changes until both gates below pass.
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

- Enumerate actual features/interfaces, not totals or "all variants." Use compact tables and concrete calls such as `tex.GetDimensions(0, w, h, levels)` rather than bare parameter lists. Keep exact types, directions, defaults and availability when they define the work; define descriptive notation immediately.
- State each requirement once. Use short paragraphs, small examples and links for deeper explanations. Preserve behavior, diagnostics, compatibility, platforms and real acceptance boundaries while removing repeated rationale, historical audits and negative scope catalogs. Do not hide a wall of text in a collapsed block.
- Name existing YAML/configuration keys needed to construct test conditions and link their documentation. Retain actual numeric interface/acceptance limits; omit incidental resource dimensions, sample values, buffer sizes, call order, allocation recipes and test profiles.
- Rely on CI/test conventions for routine execution, reproducibility and reporting. Omit generic run/evidence/status-accounting chores. Documentation tasks must identify the specific new/changed behavior to document; implementing an already-described interface does not automatically require a documentation edit.
- Each ticket changes only its owning repository. Offload tests add cases/expectations independently of current or future Clang support. Keep scheduling, compiler-progress narratives, local-tool/worktree instructions and agent runtime fallbacks out of tickets; assume suitable environments while retaining genuine device restrictions.

## Semantic review checklist

The independent reviewer must report material findings for these questions, not merely repeat the mechanical result:

- Does every checkbox request substantive work in the owning repository, with no routine CI/reporting chores, unjustified documentation task or cross-ticket/future-compiler promise?
- Is every required feature/interface/compatibility boundary retained, with no invented scope, algorithm or artificial implementation fence?
- Are language rules, backend/native behavior and runtime test expectations in their proper sections?
- Are test setup and method calls concrete enough to act on, references accurate/specific, and proposed paths clearly new?
- Can the reader immediately find the next action, without repeated prose, speculative caveats, unexplained shorthand or several jobs packed into one checkbox?

For multiple independent slices, review them in parallel; give each reviewer the same agreed contract. Keep judgements here rather than adding global steering rules or prose-pinning tests.

## Project references

Apply [Domain docs](../../../docs/agents/domain.md) for vocabulary. Use `/domain-modeling` only for distinctions primary sources cannot settle; put agreed project terms in the root glossary, not submodules.

For HLSL/compiler/DirectX/DXIL/Vulkan/SPIR-V work, apply [HLSL feature planning](../../../docs/agents/hlsl-feature-planning.md) to source selection and compiler implementation verification. Consult all three proposal collections and distinguish proposals, adopted behavior and observed results. Offload-test tickets still describe their own deliverables, not future compiler outcomes.

## Worked examples

These illustrate ticket shape; they are not additional implementation requests. Four-backtick outer fences let the validator check complete examples, including nested HLSL snippets.

### Focused offload test

````markdown repo=offload-test-suite
# [DirectX][Vulkan] Add Texture2D GetDimensions mip checks

Add Texture2D dimension checks for selected mip levels on DirectX and Vulkan.

**Status:** ready

## Acceptance Criteria

- [ ] Extend [test/Feature/Textures/GetDimensions.test] with uint and float output cases.
- [ ] Configure `Buffers[].OutputProps.MipLevels` and check mip zero, an intermediate mip and the last index, `MipLevels - 1`.

## DirectX

Read-only views expose the configured mip chain; see [docs/MipMappedTextures.md].

## Vulkan

Expected selected-mip sizes follow [OpImageQuerySizeLod].

## HLSL

Reference: [Texture2D::GetDimensions]. Outputs `w`, `h` and `levels` are all uint or all float.

```hlsl
tex.GetDimensions(w, h);
tex.GetDimensions(0, w, h, levels);
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

- [ ] Add SM6.8 comparison overloads in [clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp].
- [ ] Reuse native query operations through [llvm/lib/Target/DirectX/DXILOpLowering.cpp] and [llvm/lib/Target/SPIRV/SPIRVInstructionSelector.cpp].
- [ ] Extend [clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl] with overload and reached-entry availability cases.

## DirectX

[docs/DXIL.rst] defines CalculateLOD opcode 81: preserve selected handles and the clamped/unclamped selector.

## SPIR-V

[OpImageQueryLod] supplies clamped component 0 and unclamped component 1.

## HLSL

Reference: [comparison overloads]. Pixel minimum is SM6.8; `Location` is the concrete type below.

```hlsl
float CalculateLevelOfDetail(SamplerComparisonState sampler, Location location);
float CalculateLevelOfDetailUnclamped(SamplerComparisonState sampler, Location location);
```

| Receivers | Location |
| --- | --- |
| `Texture1D<T>`, `Texture1DArray<T>` | `float` |
| `Texture2D<T>`, `Texture2DArray<T>` | `float2` |
| `TextureCube<T>`, `TextureCubeArray<T>` | `float3` direction |

[clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/clang/lib/Sema/HLSLBuiltinTypeDeclBuilder.cpp
[llvm/lib/Target/DirectX/DXILOpLowering.cpp]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/llvm/lib/Target/DirectX/DXILOpLowering.cpp
[llvm/lib/Target/SPIRV/SPIRVInstructionSelector.cpp]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/llvm/lib/Target/SPIRV/SPIRVInstructionSelector.cpp
[clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl]: https://github.com/llvm/llvm-project/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/clang/test/SemaHLSL/Resources/Textures-CalculateLevelOfDetail.hlsl
[docs/DXIL.rst]: https://github.com/microsoft/DirectXShaderCompiler/blob/main/docs/DXIL.rst#calculatelod
[OpImageQueryLod]: https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html#OpImageQueryLod
[comparison overloads]: https://microsoft.github.io/hlsl-specs/proposals/0014-expanded-comparison-sampling/#hlsl-additions
````
