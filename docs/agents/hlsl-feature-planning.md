# HLSL feature planning

Use this reference when grilling HLSL decisions, charting a wayfinder map, or
writing specs and tickets for language features and compiler implementation.

Before choosing terms for plans, specs or tickets, read [Domain docs](domain.md)
for the canonical workspace vocabulary, upstream references and wording checks.

## Language and target requirements

Establish the language behavior before choosing its implementation. Identify the
HLSL language version, shader model and stages, runtime APIs, and generated
formats in scope. Keep language semantics, compiler lowering, and runtime/device
requirements distinct; a SPIR-V target does not by itself imply Vulkan.

Include **DirectX/D3D12 with DXIL** and **Vulkan with SPIR-V** in every feature's
implementation and test coverage. Both target paths must be done before the
feature is complete. Target-specific work may proceed sequentially or
independently, in separate tickets, commits or PRs; the two paths need not be
implemented or land together.

Keep both paths in feature plans regardless of whether the local development
environment can run DirectX/D3D12, Vulkan, both or neither. Local runtime
availability determines where execution evidence comes from, not which target
is developed; keep both paths in scope rather than deferring one until it can
run locally.

Consult relevant text in **all three** proposal collections, using the local
checkouts for discovery and specific upstream documents for citations:

- [`wg-hlsl/proposals/`](https://github.com/llvm/wg-hlsl/tree/main/proposals):
  Clang/LLVM HLSL implementation proposals.
- [`hlsl-specs/proposals/`](https://github.com/microsoft/hlsl-specs/tree/main/proposals):
  HLSL feature and implementation proposals, including earlier language work.
- [`tc57/proposals/`](https://github.com/hlsl-tc57/tc57/tree/main/proposals):
  current language proposals; also consult the applicable language specification.

TC57 supersedes `hlsl-specs` on conflict. Check each proposal's status and the
applicable language version: distinguish proposed behavior from adopted
requirements, and implementation plans from implemented behavior. Record
unresolved conflicts or missing coverage as research questions. If no relevant
proposal exists, record that gap and the collections searched rather than
inventing a reference.

Select additional references by the work in scope:

- **Feature overview:** use [Microsoft Learn's HLSL reference](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/dx-graphics-hlsl)
  for high-level descriptions; follow it to the specific construct page. Treat
  it as orientation, with precise semantics checked against the applicable
  specification and proposals.
- **DirectX:** consult the relevant feature specifications in
  [DirectX-Specs](https://github.com/microsoft/DirectX-Specs/) for D3D12, and
  D3D11 in the [D3D11 Functional spec](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm)
- **DXIL IR:** consult [DXIL.rst](https://github.com/microsoft/DirectXShaderCompiler/blob/main/docs/DXIL.rst)
  for DXIL operations, types, metadata and validity rules, including lowering
  and IR transformations. Use `DirectXShaderCompiler/docs/DXIL.rst` for local
  lookup and cite the relevant upstream sections for the applicable DXIL version.
  For every DXIL work item, complete the shader and feature flag checks below.
- **Vulkan:** consult the [Vulkan specification](https://registry.khronos.org/vulkan/specs/latest/html/vkspec.html)
  for API behavior, resource rules, device features, limits and extensions.
- **SPIR-V:** consult the [SPIR-V specification](https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html)
  for instructions, types, execution and memory models, capabilities and
  extensions. Check the target environment and applicable version; use Vulkan
  requirements as well when the execution environment is Vulkan.

Cite the specific proposal, section, instruction or feature page beside the
recommendation, decision or acceptance criterion it supports. State the required
behavior in the text so the link supplements a self-contained requirement.
Verify links and anchors; record the relevant revision/version when behavior
is version-sensitive. Repository roots and specification landing pages are
lookup entry points, not sufficient evidence for a precise semantic claim.

## DXIL shader and feature flags

Whenever planning DXIL work, include required flag support in the implementation
scope, even when the operations or resource types already have lowering support.

1. **Identify required flags.** Check the
   [Shader Flags table in DXIL.rst](https://github.com/microsoft/DirectXShaderCompiler/blob/main/docs/DXIL.rst#shader-flags)
   and the applicable feature specifications for every affected operation, type,
   resource, semantic, attribute and compiler option. Record each applicable
   flag's name/bit, setting criteria, and shader-model, stage and DXIL-version
   restrictions. Consult feature specifications for additions not yet in the
   table; explicitly record when no flag change is required and why.
2. **Plan end-to-end support.** Inspect existing flag analysis and emission;
   include missing detection, DXIL shader-property metadata, container feature
   information (`SFI0`) and validation support where required. Distinguish shader
   flag bits from container feature bits: their bit positions can differ (see
   [SampleCmpGradientOrBias's SFI0 definition](https://github.com/microsoft/hlsl-specs/blob/main/proposals/0014-expanded-comparison-sampling.md#runtime-information)). Put
   required changes in the implementation tickets and acceptance criteria,
   rather than treating flag emission as optional follow-up work.
3. **Specify flag evidence.** Plan focused compiler tests for required bits being
   set and remaining clear when their criteria are absent, plus applicable
   version/stage boundaries. Check emitted metadata and container feature
   information against the applicable specifications. Correct
   operations or runtime results alone do not establish correct flag emission.

## Offload-test-first TDD

Offload tests exercise an end-to-end path: compile an HLSL shader with a compiler,
run the compiled shader on a GPU, and check the observable results against
expected results derived from the cited specifications. Clang and DXC are
independent implementations under test; neither is an oracle or baseline for the
other. Their behavior should overlap where they implement the same HLSL
requirements, but language versions,
feature support and documented compiler differences can affect that overlap. See
[Clang's expected differences](https://clang.llvm.org/docs/HLSL/ExpectedDifferences.html)
and the [HLSL 202x/202y proposal](https://github.com/hlsl-tc57/tc57/blob/main/proposals/0001-hlsl-202x-202y.md#hlsl-202x)
for differences to check against the work's language version and proposal status.

The offload test suite's
[pull-request CI](../../offload-test-suite/.github/workflows/pr-matrix.yaml)
runs both DirectX/D3D12 and Vulkan, providing execution feedback for runtimes
unavailable locally. Review CI
results even when both runtimes work locally: differences in hardware, firmware,
drivers and runtime versions can expose failures absent from local runs.
Run available configurations locally for a fast feedback loop, and use CI to
complete the required execution coverage rather than reducing target scope.

When a suitable local DirectX/D3D12 or Vulkan runtime/driver is unavailable,
agents can compile shaders for that target and read and interpret the emitted
DXIL and/or SPIR-V IR to check the lowering against the cited target
specifications. Record what was inspected and any discrepancies as interim
compiler evidence while CI execution is pending. IR inspection supplements the
required offload runs; it does not establish runtime correctness or count as a
runtime pass. Keep unexecuted runtime results pending.

For Clang HLSL feature work in `llvm-project/` with runtime-observable behavior,
apply this sequence per implementation slice, using that slice's target scope:

1. **Write the offload tests first.** Derive expected results and legal boundaries
   from the sources above. Before implementing the Clang feature, find related
   tests in `offload-test-suite/` and extend them or add focused runtime tests
   using its existing shader, pipeline and result conventions. Cover the
   feature's observable behavior and relevant edge cases. Existing tests can
   satisfy this step when their coverage matches the planned work.
2. **Observe the missing behavior.** Run the tests with the current Clang on the
   slice's targets, locally where available and through CI where unavailable, to
   establish the failing compilation or runtime behavior the implementation will
   fix. Distinguish feature failures from test-harness, dependency and device
   failures. Run the tests with DXC on supported configurations as another
   implementation under test; DXC availability, support or a passing result is
   not a prerequisite for Clang implementation.
3. **Implement and turn the tests green.** Implement the feature in Clang/LLVM,
   with focused compiler regression tests where needed for diagnostics or
   lowering. Run the offload tests with Clang on the slice's targets. Reuse the
   same shaders, pipelines and expectations across compilers where the tested
   language and target rules are the same; document and justify compiler-specific
   cases where they differ. Compiler tests supplement runtime coverage; they do
   not replace it.
4. **Close on execution evidence.** Evaluate each compiler's results independently
   against those requirements and expected results, not against the other
   compiler's output. Record test identifiers, compiler revisions, commands,
   runtime APIs and devices/drivers, expected and observed results,
   local or linked CI execution evidence, and unsupported or unexecuted
   configurations. Keep unavailable results pending. A build, a cross-build, a
   skip or an expected failure is not a runtime pass.

Investigate compiler disagreements against the applicable requirements and
document whether they reflect a defect, a support gap or an intentional
difference. Preserve expectations derived from specifications when DXC disagrees;
changing Clang or a test expectation solely to match DXC is not a correctness
criterion.

For diagnostic-only or compile-time behavior with no runtime observation, state
why runtime coverage is inapplicable and specify focused compiler tests instead.

## Planning outputs

- **Grilling:** look up facts first, then ask only unresolved decisions. Ground
  recommendations in citations. Work through applicable semantics, target
  restrictions, expected runtime results and compiler differences before calling the
  design settled; carry the decisions and sources into the handoff.
- **Wayfinder:** keep decisions in their tickets and gist/link them from the map.
  Record the in-scope targets and a link to this reference in the map's Notes;
  keep shared guidance here rather than restating it in Notes. Resolve the link
  from the map file to the workspace's `docs/agents/hlsl-feature-planning.md`,
  rather than copying the skill's relative URL. Ticket source conflicts and
  uncertainty about required behavior or test execution as decision prerequisites.
  Planning specifies the required tests and evidence; execution belongs in the
  map only when its Notes explicitly include it.
- **Specs:** include language and target requirements and their
  citations, unresolved gaps, and testing decisions that name the offload-test-first
  TDD sequence, coverage, compiler differences and required execution evidence.
- **Implementation tickets:** retain the applicable citations alongside the
  behavior they support. Make offload-test coverage a prerequisite to the relevant
  Clang implementation slice: reference existing coverage or create a preceding
  offload-test ticket and a blocking edge. A ticket's acceptance requires Clang
  execution of those tests on the targets covered by that ticket, locally or in
  CI, against the specified results. Link separately delivered target work so
  feature-wide completion remains tracked. Record DXC execution independently
  where supported, with compiler-specific differences explained and unavailable
  results pending. Keep planned checks distinct from observed passes when
  creating or rewriting a ticket.
