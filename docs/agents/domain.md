# Domain Docs

Use a single context for this workspace: `GLOSSARY.md` is its glossary and `docs/adr/` holds its decisions, including decisions about external submodules. Store workspace-local project documentation in hlsl-dev rather than external submodules; consult their upstream documentation in place.

## Before exploring, read these

- `GLOSSARY.md` at the repo root, if it exists.
- ADRs in `docs/adr/` that touch the area you're about to work in, if any exist.

Proceed silently when these files don't exist. The `/domain-modeling` skill creates them when terms or decisions are resolved; their absence does not require creating them upfront.

## Use the glossary's vocabulary

Use the terms defined in [GLOSSARY.md](../../GLOSSARY.md) throughout project documentation, specifications, proposals, hypotheses and tests. Consult upstream sources for the relevant component, but keep workspace-local definitions in this root glossary. A missing term is a reason to investigate its meaning, not invent an abstraction.

## Precise terminology

Name the actual subject and observable requirement rather than a catch-all label. Choose words from the source's meaning, not a mechanical substitution:

| Ambiguous label | State what is actually meant |
| --- | --- |
| Contract | Language requirements, method signature, required return values, diagnostics, binary interface or compatibility requirements. |
| Fixture | Offload test, compiler regression test, test shader, pipeline description, test resource or test input, according to its role. |
| Target | Shader stage, target profile, compiler target, graphics API or test backend; identify which one. |
| Code generation or lowering | Clang HLSL codegen (HLSL AST to LLVM IR), an LLVM transformation, or LLVM target code generation (LLVM IR to DXIL or SPIR-V); name the language, compiler phase, input and resulting representation. |
| Validation | Compiler diagnostics, DXIL or SPIR-V validation, a FileCheck check or an offload result check; say what is checked. |

Keep exact API names, code identifiers, filenames, required diagnostics and source quotations intact. A GoogleTest test fixture is a genuine framework concept, not a synonym for every shader or runtime test. Explain the concrete role of any formally defined term rather than banning a spelling wherever it appears.

For example, preserve LLVM's [`contract` fast-math flag](../../llvm-project/llvm/docs/LangRef.md) for floating-point contraction. DXC's [`LibTestFixture.h`](../../DirectXShaderCompiler/tools/clang/unittests/SPIRV/LibTestFixture.h) really declares the GoogleTest fixture `clang::spirv::LibTest`; the filename does not imply a class named `LibTestFixture`.

Distinguish sample count from texture-layer count, compiler from backend, backend from runtime provider, and IR validation from shader execution. Use metadata and lowering with the specific information or transformation named.

## Add or revise terminology

Resolve facts from upstream documentation, specifications and code before asking the user for a decision. Use `/domain-modeling` for genuinely unsettled distinctions; record each newly agreed project term in `GLOSSARY.md` as it is resolved. Keep definitions short and implementation-free, and keep generic software vocabulary and writing rules out of the glossary. Store terminology rationale and source references here rather than turning the glossary into an implementation guide.

Apply this policy to `llvm-project/`, `offload-test-suite/` and `DirectXShaderCompiler/` without creating or updating personal glossaries, agent files or other workspace-local documentation inside them. Consult upstream files in place; preserve their established technical names and check version-sensitive meanings against the applicable source.

## Upstream terminology references

Use these read-only sources to check the glossary's compiler, execution and test distinctions:

| Area | Sources and relevant sections |
| --- | --- |
| Clang HLSL and source APIs | [HLSLSupport.md](../../llvm-project/clang/docs/HLSL/HLSLSupport.md): Project Goals, Non-Goals, Architectural Direction and hlsl.h. [ResourceTypes.md](../../llvm-project/clang/docs/HLSL/ResourceTypes.md): Introduction and resource member functions. [HLSL 202x and 202y](../../tc57/proposals/0001-hlsl-202x-202y.md): language versions and distinct Clang/DXC implementations. |
| Compiler representations and transformations | [IntroductionToTheClangAST.md](../../llvm-project/clang/docs/IntroductionToTheClangAST.md): Introduction and AST Nodes. [hacking.html](../../llvm-project/clang/www/hacking.html): LLVM IR Generation. [DXILArchitecture.md](../../llvm-project/llvm/docs/DirectX/DXILArchitecture.md): The DirectX Backend and DirectX Backend Flow. |
| Compiler tests and their inputs | [TestingGuide.md](../../llvm-project/llvm/docs/TestingGuide.md): Regression tests, RUN directives and Extra files. [lit.md](../../llvm-project/llvm/docs/CommandGuide/lit.md) and [FileCheck.md](../../llvm-project/llvm/docs/CommandGuide/FileCheck.md): Description. |
| Offload tests, pipelines and results | [README.md](../../offload-test-suite/README.md): Running Tests and YAML Pipeline Format. [simple.test](../../offload-test-suite/test/Basic/simple.test): separate shader, pipeline, compilation, execution and output checks. [BufferExact.test](../../offload-test-suite/test/Tools/Offloader/BufferExact.test): actual/expected buffer comparisons. Current schema declarations are in [Pipeline.h](../../offload-test-suite/include/Support/Pipeline.h) and [Pipeline.cpp](../../offload-test-suite/lib/Support/Pipeline.cpp). |
| Execution configuration and runtime roles | [offload-distribution.md](../../offload-test-suite/docs/offload-distribution.md): Suites and its Backend/Compiler matrix. [Direct3D.md](../../offload-test-suite/docs/Direct3D.md): Direct3D runtime selection and WARP as a software adapter. [MoltenVK.md](../../offload-test-suite/docs/MoltenVK.md): Vulkan portability layer. |
| DXC, DXIL and shader interfaces | [README.md](../../DirectXShaderCompiler/README.md): Features and Goals. [DXIL.rst](../../DirectXShaderCompiler/docs/DXIL.rst): Introduction, Versioning, Shader Model, Entry points, Shader Flags, HLSL signatures and semantics, Resources, Validation and DXIL Container Format. [HLSLOptions.td](../../DirectXShaderCompiler/include/dxc/Support/HLSLOptions.td): target profiles. [DxilRootSignature.h](../../DirectXShaderCompiler/include/dxc/DxilRootSignature/DxilRootSignature.h): resource-binding layout. |
| SPIR-V output and Vulkan requirements | [SPIR-V.rst](../../DirectXShaderCompiler/docs/SPIR-V.rst): Introduction, target environments, legalization/validation and HLSL register/Vulkan binding. [HLSL feature planning](hlsl-feature-planning.md): applicable language proposals, SPIR-V specifications and Vulkan requirements. |

These sources establish terminology, not proof that every described feature is implemented. Keep version-sensitive requirements tied to the applicable version; do not copy historical design documents' feature inventories as current support claims.

Gather terminology follows [WG texture methods, lines 314–329](../../wg-hlsl/proposals/0037-texture-and-sampler-types.md#L314-L329) and [SM 6.7 Raw Gather Methods](https://microsoft.github.io/DirectX-Specs/d3d/HLSL_SM_6_7_Advanced_Texture_Ops.html#raw-gather-methods). Ordinary gather selects a component; raw gather preserves a whole texel's bits through an eligible unsigned view; comparison gather returns comparison outcomes. All three are distinct from residency feedback, which describes mapping rather than data or comparison success.

The Texture view distinction is grounded in [Vulkan image-view requirements](https://docs.vulkan.org/refpages/latest/refpages/source/VkImageViewCreateInfo.html) and [DirectX resource integer aliasing](https://microsoft.github.io/DirectX-Specs/d3d/HLSL_SM_6_7_Advanced_Texture_Ops.html#resource-integer-aliasing). Keep public HLSL signatures, target instruction/image/result representation and bound-view/sampler/format eligibility separate; neither the shader's element spelling nor a target overload suffix proves a legal runtime view.

Texel-offset terminology follows [SM 6.7 Programmable Offsets](https://microsoft.github.io/DirectX-Specs/d3d/HLSL_SM_6_7_Advanced_Texture_Ops.html#programmable-offsets) and [SPIR-V Image Operands](https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html#Image_Operands). Offsets are spatial integer texel displacements, not normalized-coordinate deltas, array-layer selectors or mip/sample selectors. Constant versus dynamic describes whether the value is fixed at compile time after simplification, not merely whether the source uses a literal or `const`. Programmable offset is established DirectX terminology, not a separate resource method or sampler type.

## Flag ADR conflicts

If your output contradicts an existing ADR, surface the conflict rather than silently overriding it.
