# HLSL Workspace

This workspace develops HLSL compilers and exercises shader behavior across graphics platforms. These terms distinguish compiler responsibilities, shader representations, testing, texture resources and graphics runtime roles.

## Language

### Compilers and shader programs

**Clang HLSL**:
The HLSL implementation in upstream Clang/LLVM, distinct from the DirectX Shader Compiler implementation.
_Avoid_: DXC when referring to upstream Clang

**DirectX Shader Compiler (DXC)**:
The HLSL compiler implementation maintained in the DirectXShaderCompiler project, with DXIL and SPIR-V output support. Its LLVM/Clang-derived implementation is distinct from upstream Clang HLSL.

**HLSL shader**:
A program written in High-Level Shader Language for graphics or computation. Its source is distinct from the compiled representation supplied for execution.

**HLSL language version**:
The version of HLSL defining the source-language rules for a compilation. It is distinct from the shader model and generated IR version.

**Shader stage**:
The execution role of a shader, such as vertex, pixel or compute.
_Avoid_: Shader model

**Shader model**:
The execution model, capabilities and constraints targeted by a shader. It is distinct from the HLSL language version and DXIL version.

**Target profile**:
A compilation target name selecting a shader kind or library and a shader-model version. A library profile does not identify a pipeline stage.
_Avoid_: Graphics API

**Entry point**:
The shader function designated as an execution entry.

### Compiler representations and transformations

**Clang semantic analysis (Sema)**:
Clang's analysis of source types and language rules that builds a semantically analyzed abstract syntax tree.

**Abstract syntax tree (AST)**:
A structured representation of a source program's declarations, statements and expressions. It retains source-level structure rather than representing target instructions.

**Clang IR generation**:
Translation of a Clang AST into LLVM IR for any supported source language; Clang HLSL codegen is its HLSL-specific part.
_Avoid_: Unqualified code generation when the compiler phase matters

**Clang HLSL codegen**:
Translation of a semantically analyzed HLSL AST into LLVM IR by Clang. It is distinct from LLVM target code generation of DXIL or SPIR-V.
_Avoid_: Unqualified Clang IR generation when HLSL-specific behavior is meant

**LLVM IR**:
LLVM's intermediate representation of a program, with in-memory, textual and bitcode forms. It is distinct from both the source AST and a final machine-code binary.

**LLVM target code generation**:
Target-specific compilation of LLVM IR into the target's output representation. A shader target can produce DXIL or SPIR-V rather than GPU machine instructions.
_Avoid_: Execution backend

**Lowering**:
Translation of a source or IR construct into a lower-level or more target-specific representation. Its meaning depends on the input and resulting representations, not a single universal compiler phase.

**HLSL intrinsic**:
A predefined HLSL operation exposed through the language's function interface. Its implementation can involve library code, compiler builtins or IR operations.

**Clang builtin**:
An operation specially recognized by Clang rather than an ordinary library function. It is distinct from an LLVM IR intrinsic or an HLSL resource method.

**LLVM intrinsic**:
A specially recognized operation in LLVM IR with a defined name, type and meaning. It is distinct from the source-language function or method that may generate it.

**Resource method**:
A member function of an HLSL resource type that exposes an operation on that resource.

**DXIL operation**:
A shader operation represented by a DXIL operation call, distinct from the HLSL function or LLVM intrinsic lowered to that call.

**DXIL IR**:
The DirectX Intermediate Language representation of a Direct3D shader program, expressed using a constrained subset of LLVM IR.
_Avoid_: DXIL container when referring to the program representation

**DXIL container**:
A multipart binary package containing a DXIL program and associated shader information. It is distinct from the IR program carried inside it.

**SPIR-V**:
A binary intermediate language for shader stages and compute kernels. Its execution environment determines applicable requirements; the format alone does not imply Vulkan.
_Avoid_: Vulkan when referring only to the IR format

### Shader interfaces and validation

**DXIL metadata**:
Descriptive information associated with a DXIL program, including its shader model, entry points, signatures and resources.

**DXIL shader flags**:
Indicators of shader properties and capabilities carried in DXIL entry-point information. They are distinct from the feature information in a DXIL container.

**Container feature information (SFI0)**:
The feature-requirement information carried in a DXIL container. Its feature bits and DXIL shader-flag bits are distinct encodings.

**Per-function compatibility information**:
Usage properties and requirements associated with a shader function, including properties inherited from functions it calls. It is distinct from whole-shader device-feature requirements.

**DXIL runtime data (RDAT)**:
Structured information in a DXIL container describing functions, resources and subobjects, including per-function compatibility information.

**DXIL validation**:
Checking a DXIL program against the applicable representation rules and declared shader model. It is distinct from executing the shader and checking its observable results.
_Avoid_: Runtime test

**Resource binding**:
The association of a shader resource with the register or descriptor location used to access it.

**Root signature**:
A Direct3D resource-binding layout describing root parameters and static samplers. It is distinct from a shader input/output signature.

**Shader input/output signature**:
A description of the values consumed or produced through a shader's stage interface, including their semantics.

**Shader semantic**:
A name and optional index identifying a shader interface value or assigning it a system-defined meaning.

### Texture resources and sampling

**Multisampled texture**:
A texture containing multiple independently addressable samples at each texel location.
_Avoid_: Resolved texture, MSAA output

**Texture2DMS**:
An HLSL read-only, two-dimensional multisampled texture resource.

**Texture2DMSArray**:
An HLSL read-only array of two-dimensional multisampled texture layers.
_Avoid_: Descriptor array

**RWTexture2DMS**:
An HLSL read/write, two-dimensional multisampled texture resource whose samples are independently addressable.
_Avoid_: Multisampled render target, resolved texture

**RWTexture2DMSArray**:
An HLSL read/write array of two-dimensional multisampled texture layers.
_Avoid_: Descriptor array, array of render targets

**Sample count**:
The number of samples stored at each texel location of a resource, determined when the graphics runtime creates it. It is independent of the number of texture layers.
_Avoid_: Array size

**Sample position**:
The subpixel location associated with a sample index, expressed relative to a pixel's center or corner under a stated coordinate convention. It is distinct from the value stored in that sample.
_Avoid_: Sample value, texel coordinate

**Sample pattern**:
The ordered mapping from sample indices to subpixel sample positions. Equal sample counts do not imply equal patterns.

**Standard sample pattern**:
A sample pattern whose positions and index ordering are fixed by the applicable graphics specification.
_Avoid_: Default pattern

**Template sample count**:
The sample-count metadata in a multisampled texture's HLSL type declaration, distinct from creation or configuration of the actual resource. A runtime-determined declaration does not specify an actual zero-sample texture.
_Avoid_: Actual sample count

**Sample mask**:
A bitset identifying which samples are eligible to receive rasterized output. A one-bit mask selects one sample, not a texel or texture layer.

**Texture layer**:
One two-dimensional image within a texture array; every layer belongs to the same texture resource.
_Avoid_: Descriptor array element

**Descriptor array**:
An indexed collection of resource bindings, distinct from the layers within a texture resource.
_Avoid_: Texture array

**Resolve**:
An operation that produces a single-sample texture from a multisampled texture. Its result does not retain independently addressable samples.
_Avoid_: Per-sample readback

**CheckAccessFullyMapped**:
An HLSL intrinsic that interprets a resource-access status and reports whether every accessed portion of the resource was mapped.
_Avoid_: CheckFullyAccessedMapped

**Access status**:
An opaque value accompanying a resource-access result, interpreted by CheckAccessFullyMapped to determine whether the accessed data was fully mapped. Direct3D names the method parameter that returns it `Status`; SPIR-V calls the value a residency code.
_Avoid_: Status Boolean, error code, feedback parameter

**Sparse texture**:
A texture whose virtual regions can be backed independently by physical memory, allowing valid resources to contain mapped and unmapped regions.
_Avoid_: Unbound texture, invalid texture

**Tiled texture**:
A DirectX sparse texture whose backing is managed in runtime-defined tiles.
_Avoid_: Texture atlas

**Residency feedback**:
An indication of whether every required portion of a resource access was backed by mapped memory. It is independent of the accessed data or a depth-comparison result.
_Avoid_: Comparison success, shader-execution success

**Tile**:
A runtime-defined unit of sparse texture memory mapping. It is distinct from a texel and from a sampler-feedback mip region.
_Avoid_: Mip region, texture atlas cell

**Tile region**:
A collection of memory-residency tiles within one texture subresource.
_Avoid_: Texel rectangle

**Tile mapping**:
An association between virtual resource tiles and physical backing memory. Removing that association leaves the affected regions unmapped without invalidating the texture resource.
_Avoid_: Texture binding, descriptor binding

**Packed mip storage**:
Layout-defined storage for small mip levels that are not represented as ordinary independently addressable tile grids.

**Mip tail**:
A layout-defined region containing small mip levels whose backing is managed as opaque storage rather than individual image tiles.


**Texture query**:
An operation that reports texture dimensions, counts, or sampling level of detail without returning texel values. An LOD query may depend on sampler state and implicit derivatives; it is distinct from sampling and residency feedback.

**LOD query**:
A texture query that reports the level of detail a sampling operation would use at given coordinates, without sampling. HLSL exposes it as `CalculateLevelOfDetail` and `CalculateLevelOfDetailUnclamped`; Vulkan names the operation an LOD query.
_Avoid_: Pixel LOD, ordinary LOD, LOD calculation

**Mip level**:
One image resolution within a texture's mip chain. A mip level is distinct from a texture layer or multisample index.
_Avoid_: Texture layer, sample index

**Texel offset**:
An integer displacement of a texture access along its spatial axes, measured in texels at the effective mip level. It does not select a texture layer, mip level or multisample index.
_Avoid_: Normalized-coordinate delta

**Constant texel offset**:
A texel offset whose value is fixed at compile time. A constant result can come from simplifying an expression, not only from a literal in the source.

**Programmable offset**:
A texel offset whose value is determined during shader execution rather than fixed at compile time.
_Avoid_: Dynamic texel offset, variable offset

**Level of detail (LOD)**:
A mip-resolution selection expressed relative to the most detailed mip in the bound texture view. Fractional values can describe selection between mip levels.

**Unclamped LOD**:
The implicitly determined level of detail, including applicable sampler mip bias but before sampler and resource LOD clamps. It can lie outside the available mip range.
_Avoid_: Unbiased LOD

**Clamped LOD**:
The level of detail corresponding to sampling after applicable sampler, resource-view and mip-selection rules. It does not include a separate shader-provided per-sample clamp.
_Avoid_: Zero-clamped LOD

**Instruction LOD clamp**:
A shader-supplied limit, given with one sampling operation, on the most detailed mip level that operation may use. It applies to fully backed and sparse textures alike and is distinct from sampler and texture-view LOD limits.
_Avoid_: Sampler MinLOD, resource MinLOD

**SamplerComparisonState**:
An HLSL sampler type representing state that specifies how a supplied comparison reference is compared with sampled texture values.
_Avoid_: ComparisonSampler, new sampler type

**Comparison sampling**:
Texture sampling whose result comes from comparing a supplied reference with each sampled value and then filtering the comparison outcomes. It is distinct from comparing the reference with an already filtered texture value.
_Avoid_: Comparison of filtered depth

**Texture allocation**:
The complete texture storage, including its allocated dimensions, mip levels, array layers and samples. A texture view can expose only part of that storage without changing the allocation.

**Texture element type**:
The scalar or vector type declared for each shader-visible element of a texture resource. It is distinct from the texture's storage and view formats.

**Texture component type**:
The scalar type of a texture element, whether that element is scalar or vector. Its bit width is distinct from the total bit width of a texel containing multiple components.

**Partial-element store**:
A store that writes some, but not all, components of one element of a typed resource (a writable texture or typed buffer), leaving the element's other components unchanged. Components are counted in the declared element type, not in the channels of the underlying storage format.
_Avoid_: Partial-texel store (a typed-buffer element is not a texel), component store when referring to the whole set (a store to one component of a scalar element writes the whole element)

**SPIR-V image sampled type**:
The scalar type named by `OpTypeImage` for image values. It is distinct from the HLSL texture element type and the bound texture view format.

**Texture view**:
An interpretation of texture storage that selects subresources and a format, with component mapping where supported by the graphics API. It is distinct from a shader resource's declared element type.
_Avoid_: Shader element type when referring to the bound view

**Texture view format**:
The graphics-API format through which a bound texture view interprets texel data. It is distinct from the shader-visible texture element type and the SPIR-V image sampled type.

**Restricted texture view**:
A texture view that exposes a mip-level or array-layer range smaller than its texture allocation. Shader coordinates address the visible range relative to the view rather than the allocation's selected origin.

**Ordinary gather**:
A texture operation that returns one selected component from each of four texels without filtering the returned values together. It is distinct from retrieving four components of one texel or returning comparison outcomes.
_Avoid_: Four-channel fetch, bilinear sample

**Ordinary named gathers**:
The HLSL methods `GatherRed`, `GatherGreen`, `GatherBlue` and `GatherAlpha`, which select the corresponding component for an ordinary gather. The generic `Gather` method selects red but is not part of this four-name group.

**Raw gather**:
A texture operation that returns unsigned integers preserving the raw bits of each of four texels, including all of each texel's channels, without numerical format conversion. It is distinct from an ordinary selected-component gather or an untyped buffer load.
_Avoid_: GatherRed alias, raw buffer load

**Comparison gather**:
A texture operation that returns four per-texel comparison outcomes for one selected texture component without filtering those outcomes together. It is distinct from retrieving four components of one texel or from comparison sampling.
_Avoid_: Filtered comparison sampling, four-channel comparison

**Named comparison gathers**:
The HLSL methods `GatherCmpRed`, `GatherCmpGreen`, `GatherCmpBlue` and `GatherCmpAlpha`, which compare the corresponding component from each gathered texel with a reference. The generic `GatherCmp` method selects red but is not part of this four-name group.

**Quad**:
A subdivision of a wave consisting of four lanes computing adjacent values. A quad of shader invocations is distinct from the four texture texels returned by a gather.

**Quad layout**:
How a compute shader's `numthreads` dimensions assign lanes to quads. **1D quads** apply when X is divisible by 4 and Y = Z = 1; each quad is four consecutive `SV_GroupIndex` values. **2D quads** apply when X and Y are divisible by 2; each quad is a 2×2 block of `SV_GroupThreadID.xy`. Other shapes have no quad layout for derivatives. In Vulkan they correspond to the `DerivativeGroupLinearKHR` and `DerivativeGroupQuadsKHR` execution modes.
_Avoid_: Compute quad layout; derivative group (Vulkan's workgroup-level term)

**Derivative group**:
A set of shader invocations over which derivative-uniformity requirements are defined. In SPIR-V compute shaders it is a workgroup, not an individual four-lane quad.

**Implicit derivatives**:
Coordinate or value differences derived from neighboring shader invocations within a derivative group, rather than supplied by the caller.
_Avoid_: Explicit gradients

**Derivative-dependent entry point**:
An entry point that reaches an operation using implicit derivatives, directly or through any callee. The compute thread-group shape rule and the Vulkan derivative-group execution mode apply to it. A callee that only receives precomputed gradients does not make its caller derivative-dependent.
_Avoid_: Reached entry, derivative-using function

**Explicit gradients**:
Caller-supplied coordinate derivatives that describe a sampling footprint. For array textures, they describe spatial coordinates, not the texture-layer coordinate.
_Avoid_: Implicit derivatives

**Sampler feedback**:
Information about the mip levels or texture regions that a sampling operation would require. It is distinct from sampled texel values and from feedback about whether memory is resident.
_Avoid_: Residency feedback, sampled data

**Feedback map**:
A resource containing accumulated sampler feedback for a paired texture. Its opaque representation is distinct from application-readable decoded feedback.
_Avoid_: MinMip residency map, ordinary writable texture

**FeedbackTexture2D**:
An HLSL write-only sampler-feedback resource for a two-dimensional texture.

**FeedbackTexture2DArray**:
An HLSL write-only sampler-feedback resource for an array of two-dimensional texture layers.
_Avoid_: Descriptor array

**Mip region**:
A rectangular area of a texture represented as one region in sampler feedback. It is not necessarily a memory-residency tile.
_Avoid_: Tile

**MinMip feedback**:
Sampler feedback identifying the most detailed mip level requested for each mip region of a paired texture.
_Avoid_: Resident minimum mip

**MipRegionUsed feedback**:
Sampler feedback identifying which mip regions are requested at each mip level of a paired texture.

**Sampling footprint**:
The region of a texture represented by a sampling operation's coordinate derivatives. Its extents can differ across spatial directions.

**Anisotropic filtering**:
Texture filtering that accounts for unequal extents of the sampling footprint in different spatial directions. It is distinct from the selection of nearest or linear filtering between mip levels.

**Maximum anisotropy**:
An upper bound on the degree of anisotropy a sampler may use, not a guarantee that every sampling footprint uses that ratio.
_Avoid_: Actual anisotropy ratio

**Rasterizer-ordered resource**:
An HLSL read/write resource whose accesses from overlapping invocations take effect in primitive order. HLSL names these types `RasterizerOrdered*`; Direct3D calls them rasterizer-ordered views (ROVs).
_Avoid_: Ordered UAV, interlocked resource

**Overlapping invocations**:
Pixel-shader invocations from the same draw that share a pixel under per-pixel execution, or a pixel and a sample under sample-frequency execution.
_Avoid_: Overlapping primitives

**Sample-frequency execution**:
Execution of a pixel shader once per covered sample rather than once per pixel.
_Avoid_: Multisampling

**Fragment shader interlock**:
The Vulkan mechanism that delimits a critical section of a fragment shader and orders it between overlapping invocations. It is how SPIR-V expresses the ordering a rasterizer-ordered resource requires.
_Avoid_: Atomic access

**ROV-dependent entry point**:
An entry point that reaches an access to a rasterizer-ordered resource, directly or through any callee. Declaring such a resource without reaching an access does not make an entry point ROV-dependent.
_Avoid_: ROV-declaring entry point

### Graphics execution

**Graphics API**:
The programming interface used to submit shader work and access execution resources, such as Direct3D 12 or Vulkan. It is distinct from the runtime implementation and selected device.

**Execution backend**:
The test framework's implementation for executing shader work through a particular graphics API. It is distinct from an LLVM compiler backend and from the runtime provider used for execution.
_Avoid_: Unqualified backend when both compiler and execution paths are in scope

**D3D12 backend**:
The test execution path that interprets shader programs and resource bindings according to Direct3D 12 semantics. Its identity is independent of the runtime provider or operating system.
_Avoid_: vkd3d backend, Vulkan backend when referring to translated D3D12 execution

**D3D12 runtime provider**:
A particular implementation of Direct3D 12 used by the D3D12 backend. Different providers need not offer interchangeable binary interfaces or identical device capabilities.
_Avoid_: Backend, graphics API

**D3D12 compatibility runtime**:
A D3D12 runtime provider compatible with another provider's explicitly identified consumer interfaces and behavior while using a different implementation underneath. Compatibility for those consumers does not imply support for every Direct3D application.
_Avoid_: Library alias, complete WSL replacement without stated interface and behavior requirements

**vkd3d-proton**:
A Direct3D 12 implementation that translates shader programs and graphics operations to Vulkan. It is distinct from Valve's Proton compatibility environment; native use does not imply that environment is required.
_Avoid_: Proton when referring only to vkd3d-proton

### Tests and test artifacts

**Compiler regression test**:
A focused test input and checks for a compiler feature or previously observed compiler bug. It can check diagnostics or generated representations without executing a shader.
_Avoid_: Runtime test when no shader is executed

**lit**:
LLVM's test runner for discovering and executing tests and reporting their results. It is distinct from the compiler and result-checking tools invoked by a test.

**FileCheck**:
A tool that checks textual output against specified patterns. The checked text can come from a compiler or an executed test program.

**Auxiliary test input**:
An additional input consumed by a test, distinct from its test-driving instructions and output checks.

**Offload test**:
A test that executes a compiled shader and checks its observable results against expected results.
_Avoid_: Fixture, compilation-only check

**Runner infrastructure test**:
An offload test that checks an execution-backend feature, such as a resource kind, pipeline setting or readback path, using only shader lowering that already works. It is distinct from an offload test of compiler output.
_Avoid_: Smoke test, infrastructure test, feature test

**Overlap test**:
An offload test in which overlapping invocations apply order-dependent updates to a rasterizer-ordered resource and the result is compared with the primitive-order result. A failure shows an ordering violation; a pass does not prove ordering.
_Avoid_: Ordering proof, stress test

**Compiler under test**:
The shader compiler whose output is being evaluated. Another compiler's output is an independent result, not the expected result by definition.

**Offload test configuration**:
The selected compiler, graphics API, runtime environment and device settings for an offload test run. It is distinct from the pipeline description for an individual test.

**Test shader**:
Shader source whose behavior a test exercises. It is distinct from the test's pipeline description and expected results.
_Avoid_: Fixture

**Compiled shader**:
A compiled shader representation supplied to a runtime for execution. It is distinct from the HLSL source and need not be final GPU machine code.

**Pipeline**:
The combination of shader stages and execution settings used to execute shader work.

**Pipeline description**:
A declaration of a pipeline and its associated test data. In offload tests, it is distinct from the shader source and compiled shader.
_Avoid_: Shader, fixture

**Offload test buffer**:
A named collection of typed data used as test input, output or expected values. It can hold texture data and is not necessarily an HLSL buffer resource.

**Expected buffer**:
A buffer containing reference values for comparison with actual test output. It need not be a shader-bound resource.

**Comparison rule**:
The criterion and tolerances used to decide whether actual and expected test results agree.

**Golden image**:
An image containing the expected rendered output for a test comparison.

### Planning

**One-off**:
A ticket for one repository that belongs to a scenario but to none of its deliverables. It is parented directly under the scenario issue and comes before every deliverable in the scenario order.
_Avoid_: Position-0 deliverable, LOD-clamp one-off when referring to the whole set
