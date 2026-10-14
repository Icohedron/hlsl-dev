---
name: hlsl-godbolt-repro
description: Create or reduce HLSL compilation repros for Clang and DXC on Compiler Explorer (godbolt.org), compare diagnostics or generated code through its REST API, and publish verified share links.
---

# HLSL Godbolt repro

Create a self-contained compilation repro on [Compiler Explorer](https://godbolt.org/).
Use its [REST API](https://github.com/compiler-explorer/compiler-explorer/blob/main/docs/API.md)
for discovery and compilation; use the browser to check the final shared page.
This proves compiler behavior, not GPU execution or runtime correctness.

## 1. Fix the repro contract

Read the supplied source, command line, diagnostics and existing link before
changing anything. For `/z/<id>` links, retrieve `/api/shortlinkinfo/<id>` and
inspect the saved source and compiler settings.

Record the expected observable result: a specific diagnostic, crash, missing
instruction, or code-generation difference. Preserve the shader stage/model,
entry point, HLSL version, optimization, defines, layout/precision flags and
DXIL versus SPIR-V target. Keep changes needed to port the original command to
Compiler Explorer explicit. Treat DXC as a compatibility baseline, not a language
specification; consult the workspace's HLSL references for normative claims.

**Publication boundary:** compilation uploads source and supplied files to a
public third-party service. Use only source authorized for public disclosure;
ask before uploading private/proprietary material. Short links persist state
indefinitely. Remove credentials, private paths and unrelated source first.

Proceed when the exact behavior and publishable input are identified.

## 2. Discover the hosted compilers

Read the current API documentation when an endpoint or schema is uncertain.
Send `Accept: application/json` on every request and `Content-Type:
application/json` on JSON POSTs. Shell examples below require `curl` and `jq`;
use an equivalent HTTP client and JSON serializer if unavailable.

```bash
curl --fail-with-body --silent --show-error \
  -H 'Accept: application/json' \
  https://godbolt.org/api/compilers/hlsl --output compilers.json
jq -r '.[] | select(.compilerType == "clang-dxc" or .compilerType == "hlsl") |
  [.id, .name, .semver] | @tsv' compilers.json
```

Set `CLANG_ID` and `DXC_ID` to exact IDs from that response. Select the HLSL
Clang adapter (`compilerType: clang-dxc`), not a C++ Clang compiler. It supplies
`--driver-mode=dxc` itself, so both compiler families take DXC-style arguments
such as `-T cs_6_0 -E main`. Use the requested versions; otherwise choose hosted
Clang trunk and a versioned DXC release. Add DXC trunk or assertion-enabled
Clang only when relevant. Record the returned names and versions and the UTC
run date: a trunk ID and a saved link do not pin an immutable compiler build.

If a requested version or local patch is unavailable, state the mismatch;
a hosted result does not verify a local checkout. For non-HLSL Clang repros,
discover the appropriate language via `/api/languages` and its compiler list
rather than forcing them through the HLSL adapter.

Proceed when every selected compiler exists in the live inventory.

## 3. Compile the baseline

Write JSON to files with a JSON serializer or file-writing tool, then send it
with `--data-binary @file`; keep source out of shell-escaped inline JSON.
The following `compile.json` is a smoke example, not a replacement for the
user's shader or flags:

```json
{
  "source": "RWStructuredBuffer<uint> output : register(u0);\n[numthreads(1, 1, 1)]\nvoid main(uint3 tid : SV_DispatchThreadID) {\n  output[tid.x] = tid.x + 1;\n}\n",
  "lang": "hlsl",
  "options": {
    "userArguments": "-T cs_6_0 -E main -HV 2021 -O3",
    "compilerOptions": {"skipAsm": false, "executorRequest": false},
    "filters": {
      "binary": false, "execute": false, "labels": false,
      "directives": false, "commentOnly": false, "trim": false
    }
  }
}
```

```bash
for id in "$DXC_ID" "$CLANG_ID"; do
  curl --fail-with-body --silent --show-error \
    -H 'Accept: application/json' -H 'Content-Type: application/json' \
    --data-binary @compile.json \
    "https://godbolt.org/api/compiler/$id/compile" --output "$id.result.json" || exit 1
  jq '{code, timedOut, truncated, stdout, stderr, asm}' "$id.result.json"
done
```

Read `code`, `stdout`, `stderr` and the `asm[].text` lines. HTTP success is not
compiler success; nonzero `code` may be precisely the requested repro. Distinguish
a source diagnostic from an adapter failure, timeout, truncated response or
service error. Require the actual target diagnostic/instruction, not merely a
nonzero exit or nonempty assembly. The smoke example should produce `main` and
a buffer store with both compilers.

For DXIL, inspect the returned text as DXIL/LLVM-style IR, not CPU assembly.
Keep filtering disabled when comments, metadata or declarations are evidence.
For SPIR-V repros, retain `-spirv` and the original target-environment options;
check that the output is SPIR-V. Use per-compiler request files when flags must
differ and explain each difference; never silently change language versions or
optimization to make a comparison pass. Compiler Explorer supplies input/output
paths and some adapter flags; inspect returned `compilationOptions` when an
injected flag appears relevant. Report adapter warnings separately from source
diagnostics rather than suppressing them.

For necessary headers, prefer a self-contained reduction. Otherwise the compile
API accepts top-level `files: [{"filename": "helper.h", "contents": "..."}]`.
Supply every dependency: compile nodes cannot fetch URL includes or read local
workspace paths. The simple share state below does not carry these files;
inline them while preserving the failure, or create and verify a multi-file
project through the UI before sharing.

Proceed when the original observable behavior has been checked for each compiler.

## 4. Reduce without losing the behavior

Remove unrelated declarations, includes and expressions incrementally, compiling
each candidate against the fixed observable result. Retain a failing input and
a control/baseline when the contrast is the evidence. Keep resource operations
reachable and observable so optimization cannot discard the path under study.
Preserve required semantics, resource declarations and stage attributes. For a
codegen repro, a syntax-only pass is not verification.

Finish by recompiling the exact final source and options for every displayed
compiler. Proceed only when the reduction still demonstrates the intended
behavior; if the hosted compilers cannot reproduce it, report that limitation
instead of publishing an unrelated failure as a repro.

## 5. Create and check the share link

For one self-contained source with identical flags, derive a ClientState from
the verified request. Its `language` and compiler `options` fields differ from
the compile API's `lang` and `options.userArguments`:

```bash
jq --arg clang "$CLANG_ID" --arg dxc "$DXC_ID" '
  . as $request | {sessions: [{id: 1, language: $request.lang,
    source: $request.source,
    compilers: ([$clang, $dxc] | map({id: ., options: $request.options.userArguments,
      filters: $request.options.filters}))}]}' compile.json > share.json
curl --fail-with-body --silent --show-error \
  -H 'Accept: application/json' -H 'Content-Type: application/json' \
  --data-binary @share.json https://godbolt.org/api/shortener --output shortlink.json
jq -r '.url' shortlink.json
```

When requests differ, populate each compiler's `options` and `filters` from its
own verified request. For distinct sources, use separate sessions with unique
numeric session IDs. Post ClientState directly, or under `config`; a compile
request is not a share state. Use the server-returned URL, not an invented ID
or a hand-built `#` fragment.

Extract the ID from the returned `/z/<id>` URL and GET
`/api/shortlinkinfo/<id>`. Compare the saved source, language, compiler IDs,
options and filters with the final requests. Open the returned URL in the
browser and verify that the editor and both compiler panes load, compilation
completes, and the intended diagnostic or generated code is visible. Inspect
a screenshot and close the tab. API replay alone is not a UI check; state any
browser-access limitation explicitly. A short link preserves inputs/settings,
not previously observed output or future trunk behavior.

Proceed when the stored state matches and the shared page reproduces the result.

## 6. Deliver the repro

Return the verified Godbolt link, the minimal source (or its saved path), and
one row per compiler: exact ID/name/version, exact flags, compiler exit code,
and the decisive diagnostic or generated-code excerpt. State the expected
behavior, observed difference, run date, and any version/target or verification
limitation. Keep raw request/response files with an existing issue's artifacts
when needed for evidence; remove disposable scratch files. Successful shader
compilation is not evidence of a GPU runtime pass.
