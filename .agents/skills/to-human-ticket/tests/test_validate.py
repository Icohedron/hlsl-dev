"""Consumer regressions for the standalone mechanical-ticket validator CLI."""

from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "validate.py"


def invoke(*arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
        capture_output=True,
        text=True,
        check=False,
    )


def ticket(repo="llvm-project", count=1, verb="Implement"):
    """A small, independently actionable consumer input; not a golden snapshot."""
    backend = "SPIR-V" if repo == "llvm-project" else "Vulkan"
    tag = "SPIRV" if repo == "llvm-project" else "Vulkan"
    prefix = "[HLSL]" if repo == "llvm-project" else ""
    file = "clang/lib/CodeGen/CGHLSLBuiltins.cpp" if repo == "llvm-project" else "test/Feature/Textures/GetDimensions.test"
    tasks = "\n".join(f"- [ ] {verb} query case {number} in [`{file}`][source]." for number in range(count))
    return f"""# {prefix}[DirectX][{tag}] Extend texture dimension queries

Cover texture query results with explicit mip levels.

## Acceptance Criteria

{tasks}

## DirectX

Use [DirectX query semantics][dx].

## {backend}

Use [native query semantics][native].

## HLSL

Observe [Texture2D.GetDimensions][language] output types.

[source]: https://github.com/llvm/{repo}/blob/main/{file}
[dx]: https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm
[native]: https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html#OpImageQuerySizeLod
[language]: https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions
"""


def write_ticket(tmp_path, content=None, repo="llvm-project", name="query.md"):
    path = tmp_path / repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ticket(repo) if content is None else content, encoding="utf-8")
    return path


def assert_rule(result, rule, path=None):
    assert result.returncode == 1, result.stdout + result.stderr
    assert f": {rule}:" in result.stdout
    if path is not None:
        assert f"{path}:" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("repo", ["llvm-project", "offload-test-suite"])
def test_valid_owner_inference_and_explicit_owner(tmp_path, repo):
    path = write_ticket(tmp_path, repo=repo)
    result = invoke(path)
    assert result.returncode == 0, result.stdout + result.stderr
    detached = tmp_path / "standalone.md"
    detached.write_text(ticket(repo), encoding="utf-8")
    explicit = invoke("--repo", repo, detached)
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr


@pytest.mark.parametrize("count, code", [(5, 0), (6, 1)])
def test_five_item_group_boundary(tmp_path, count, code):
    path = write_ticket(tmp_path, ticket(count=count))
    result = invoke(path)
    assert result.returncode == code, result.stdout + result.stderr
    if code:
        assert_rule(result, "CHECKLIST_GROUP", path)


def test_explicit_subgroups_reset_limit_and_historical_checks_stay_checked(tmp_path):
    content = ticket(count=5)
    content = content.replace("## DirectX", "### Preserve regression coverage\n\n- [x] Retain an existing query check.\n\n## DirectX", 1)
    path = write_ticket(tmp_path, content)
    result = invoke(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("verb", ["Reconcile", "Audit", "Characterize", "Consolidate"])
def test_arbitrary_imperative_words_are_not_a_whitelist(tmp_path, verb):
    result = invoke(write_ticket(tmp_path, ticket(verb=verb)))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "old,new,rule",
    [
        ("# [HLSL]", "## [HLSL]", "TITLE"),
        ("[HLSL][DirectX]", "[HLSL] [DirectX]", "PREFIX"),
        ("[SPIRV]", "[Vulkan]", "PREFIX"),
        ("## Acceptance Criteria", "## Work", "ACCEPTANCE"),
        ("## SPIR-V", "## Vulkan", "OWNER_SECTION"),
        ("## HLSL", "## Language", "SECTION"),
        ("Cover texture query results with explicit mip levels.\n\n", "", "SCOPE"),
    ],
)
def test_structural_contract_findings(tmp_path, old, new, rule):
    path = write_ticket(tmp_path, ticket().replace(old, new, 1))
    assert_rule(invoke(path), rule, path)


def test_second_active_title_is_rejected_but_fenced_title_is_not(tmp_path):
    path = write_ticket(tmp_path, ticket() + "\n# Another deliverable\n")
    assert_rule(invoke(path), "TITLE")
    path.write_text(ticket() + "\n```markdown\n# Not a second title\n[bogus][missing]\n```\n", encoding="utf-8")
    result = invoke(path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_empty_acceptance_section_is_not_a_completed_ticket(tmp_path):
    content = ticket().replace("- [ ] Implement query case 0 in [`clang/lib/CodeGen/CGHLSLBuiltins.cpp`][source].", "The criteria remain unwritten.")
    path = write_ticket(tmp_path, content)
    assert_rule(invoke(path), "ACCEPTANCE")


@pytest.mark.parametrize("syntax", ["[language]", "[language][]", "[a method][LANGUAGE]"])
def test_shortcut_collapsed_and_full_references_resolve_case_insensitively(tmp_path, syntax):
    content = ticket().replace("[Texture2D.GetDimensions][language]", syntax)
    result = invoke(write_ticket(tmp_path, content))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("syntax", ["[method][missing]", "[missing][]"])
def test_unresolved_explicit_references_are_findings(tmp_path, syntax):
    path = write_ticket(tmp_path, ticket().replace("[Texture2D.GetDimensions][language]", syntax))
    assert_rule(invoke(path), "REFERENCE_UNDEFINED", path)


def test_ordinary_brackets_escaped_punctuation_and_code_are_not_missing_links(tmp_path):
    prose = "\nSource expressions A[i], values[index + 1], [0, n), and \\[not a link] are literal.\n\n`[also][undefined]` and `[ ]` are code.\n"
    result = invoke(write_ticket(tmp_path, ticket() + prose))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("label", ["LANGUAGE", "language", "LaNgUaGe"])
def test_duplicate_casefolded_definitions_are_not_silently_discarded(tmp_path, label):
    path = write_ticket(tmp_path, ticket() + f"\n[{label}]: https://example.org/duplicate\n")
    assert_rule(invoke(path), "REFERENCE_COLLISION")


def test_unused_reference_definition_has_original_line(tmp_path):
    content = ticket() + "\n[unused]: https://example.org/unneeded\n"
    path = write_ticket(tmp_path, content)
    result = invoke(path)
    line = content.splitlines().index("[unused]: https://example.org/unneeded") + 1
    assert_rule(result, "REFERENCE_UNUSED")
    assert f"{path}:{line}: REFERENCE_UNUSED:" in result.stdout


@pytest.mark.parametrize("label", ["CGHLSLBuiltins.cpp", "Code generation", "`CGHLSLBuiltins.cpp`"])
def test_github_file_label_requires_full_repo_root_path(tmp_path, label):
    content = ticket().replace("[`clang/lib/CodeGen/CGHLSLBuiltins.cpp`][source]", f"[{label}][source]")
    content = content.replace("/blob/main/", "/blob/1b4e7f41268eb3db5300e79256764140e509e3f5/")
    assert_rule(invoke(write_ticket(tmp_path, content)), "FILE_LABEL")


def test_distinct_file_anchors_can_have_distinct_reference_ids(tmp_path):
    content = ticket().replace("## HLSL", "See [`clang/lib/CodeGen/CGHLSLBuiltins.cpp`][other-source].\n\n## HLSL")
    content += "\n[other-source]: https://github.com/llvm/llvm-project/blob/main/clang/lib/CodeGen/CGHLSLBuiltins.cpp#L10\n"
    result = invoke(write_ticket(tmp_path, content))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("url", ["../another-ticket.md", "/workspace/hlsl-dev/file.md", "file:///tmp/ticket.md", "http://localhost:9000/ticket", ".scratch/query.md"])
def test_local_destinations_are_rejected_even_as_definitions(tmp_path, url):
    content = ticket().replace("https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions", url)
    assert_rule(invoke(write_ticket(tmp_path, content)), "LOCAL_LINK")


def test_inline_and_autolinks_require_reference_style(tmp_path):
    content = ticket().replace("[Texture2D.GetDimensions][language]", "[Texture2D](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions)")
    assert_rule(invoke(write_ticket(tmp_path, content)), "REFERENCE_STYLE")
    content = ticket() + "\n<https://example.org/reference>\n"
    assert_rule(invoke(write_ticket(tmp_path, content)), "REFERENCE_STYLE")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/microsoft/DirectXShaderCompiler/blob/main/docs/DXIL.rst",
        "https://registry.khronos.org/SPIR-V/specs/unified1/SPIRV.html",
        "https://example.org/windows/win32/direct3dhlsl/getdimensions",
    ],
)
def test_hlsl_needs_official_language_not_only_backend_reference(tmp_path, url):
    content = ticket().replace("https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions", url)
    assert_rule(invoke(write_ticket(tmp_path, content)), "HLSL_LANGUAGE")


def test_official_language_proposal_qualifies_with_full_file_label(tmp_path):
    content = ticket().replace("[Texture2D.GetDimensions][language]", "[proposals/0033-resources-get-dimensions.md][language]")
    content = content.replace("https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions", "https://github.com/llvm/wg-hlsl/blob/main/proposals/0033-resources-get-dimensions.md")
    result = invoke(write_ticket(tmp_path, content))
    assert result.returncode == 0, result.stdout + result.stderr


def test_language_link_must_be_inside_hlsl_not_another_section(tmp_path):
    content = ticket().replace("Use [DirectX query semantics][dx].", "Use [DirectX query semantics][dx] and [Texture2D.GetDimensions][language].")
    content = content.replace("Observe [Texture2D.GetDimensions][language] output types.", "Observe [backend documentation][native].")
    assert_rule(invoke(write_ticket(tmp_path, content)), "HLSL_LANGUAGE")


def test_each_technology_section_links_its_own_reference(tmp_path):
    content = ticket().replace("Use [DirectX query semantics][dx].", "Map the native query operation.")
    assert_rule(invoke(write_ticket(tmp_path, content)), "SECTION_REFERENCE")


def test_unsupported_hardware_boundary_is_not_runner_status_accounting(tmp_path):
    content = ticket("offload-test-suite").replace("Observe [Texture2D.GetDimensions][language] output types.", "Observe [Texture2D.GetDimensions][language] output types. Unsupported hardware must reject the capability-dependent case.")
    result = invoke(write_ticket(tmp_path, content, repo="offload-test-suite"))
    assert result.returncode == 0, result.stdout + result.stderr


def test_historical_discussion_and_comments_are_preserved_but_not_active_rules(tmp_path):
    content = "<!-- [old][missing]\n# Historical title\n-->\n" + ticket()
    content += "\n## History\n\n# Not history at a higher heading\n"
    # Historical text is bounded by its heading, not by a guessed timestamp.
    content = content.replace("\n# Not history at a higher heading\n", "\n### Earlier discussion\n\n[old][missing]\n\n- [x] An obsolete criterion.\n" * 7)
    path = write_ticket(tmp_path, content)
    result = invoke(path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert path.read_text(encoding="utf-8") == content


def test_recursive_explicit_directory_includes_ignored_scratch(tmp_path):
    root = tmp_path / ".scratch" / "tickets"
    root.mkdir(parents=True)
    (tmp_path / ".gitignore").write_text(".scratch/\n", encoding="utf-8")
    write_ticket(root)
    bad = write_ticket(root, ticket("offload-test-suite", count=6), repo="offload-test-suite")
    assert_rule(invoke(root), "CHECKLIST_GROUP", bad)


@pytest.mark.parametrize("mode", ["no-input", "empty-directory", "missing-file", "owner-missing", "invalid-utf8", "invalid-repo"])
def test_invocation_and_io_errors_are_exit_two(tmp_path, mode):
    if mode == "no-input":
        result = invoke()
    elif mode == "empty-directory":
        result = invoke(tmp_path)
    elif mode == "missing-file":
        result = invoke(tmp_path / "missing.md")
    elif mode == "owner-missing":
        path = tmp_path / "ticket.md"
        path.write_text(ticket(), encoding="utf-8")
        result = invoke(path)
    elif mode == "invalid-repo":
        result = invoke("--repo", "unknown", tmp_path)
    else:
        path = write_ticket(tmp_path)
        path.write_bytes(b"\xff\xfe")
        result = invoke(path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stderr
    assert "Traceback" not in result.stderr


def test_examples_extract_both_annotated_four_backtick_blocks_and_nested_code(tmp_path):
    compiler = ticket() + "\n```hlsl\n// [not][a-reference]\nuint query(Texture2D<float4> tex) { uint w, h; tex.GetDimensions(w, h); return w; }\n```\n"
    skill = tmp_path / "SKILL.md"
    skill.write_text("# Skill prose is not a ticket\n\n````markdown repo=llvm-project\n" + compiler + "````\n\n````markdown repo=offload-test-suite\n" + ticket("offload-test-suite") + "````\n\n```markdown\n# Unannotated template\n```\n", encoding="utf-8")
    result = invoke("--examples", skill)
    assert result.returncode == 0, result.stdout + result.stderr


def test_example_diagnostics_keep_source_file_line_numbers(tmp_path):
    skill = tmp_path / "SKILL.md"
    content = "# Skill\n\n````markdown repo=llvm-project\n" + ticket(count=6) + "````\n"
    skill.write_text(content, encoding="utf-8")
    line = next(number for number, value in enumerate(content.splitlines(), 1) if "query case 5" in value)
    result = invoke("--examples", skill)
    assert_rule(result, "CHECKLIST_GROUP", skill)
    assert f"{skill}:{line}: CHECKLIST_GROUP:" in result.stdout


def test_examples_request_without_annotated_blocks_is_an_error(tmp_path):
    path = tmp_path / "SKILL.md"
    path.write_text("```markdown\n# Plain template\n```\n", encoding="utf-8")
    result = invoke("--examples", path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "EXAMPLES" in result.stderr


def test_examples_are_repeatable_and_selection_errors_do_not_disappear(tmp_path):
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("````markdown repo=llvm-project\n" + ticket() + "````\n", encoding="utf-8")
    second.write_text("````markdown repo=offload-test-suite\n" + ticket("offload-test-suite", count=6) + "````\n", encoding="utf-8")
    assert_rule(invoke("--examples", first, "--examples", second), "CHECKLIST_GROUP", second)
    result = invoke("--examples", first, tmp_path / "missing.md")
    assert result.returncode == 2


def test_reference_used_only_by_preserved_discussion_is_not_unused(tmp_path):
    content = ticket().replace(
        "[source]:", "## Discussion\n\nEarlier rationale used [old notes][archive].\n\n[source]:", 1
    )
    content += "\n[archive]: https://example.org/historical-notes\n"
    result = invoke(write_ticket(tmp_path, content))
    assert result.returncode == 0, result.stdout + result.stderr


def test_reference_collisions_use_unicode_casefolding(tmp_path):
    content = ticket().replace("[language]", "[Straße]")
    content += "\n[STRASSE]: https://example.org/duplicate\n"
    assert_rule(invoke(write_ticket(tmp_path, content)), "REFERENCE_COLLISION")


def test_metal_is_required_only_when_tagged(tmp_path):
    content = ticket("offload-test-suite").replace("[DirectX][Vulkan]", "[DirectX][Vulkan][Metal]", 1)
    path = write_ticket(tmp_path, content, repo="offload-test-suite")
    assert_rule(invoke(path), "SECTION")
    content = content.replace("## HLSL", "## Metal\n\nUse [Metal resource semantics][metal].\n\n## HLSL", 1)
    content += "\n[metal]: https://developer.apple.com/metal/Metal-Shading-Language-Specification.pdf\n"
    path.write_text(content, encoding="utf-8")
    result = invoke(path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_single_backend_ticket_does_not_require_unassigned_technology(tmp_path):
    content = ticket().replace("[DirectX][SPIRV]", "[DirectX]", 1)
    content = content.replace("## SPIR-V\n\nUse [native query semantics][native].\n\n", "")
    content = "\n".join(line for line in content.splitlines() if not line.startswith("[native]:")) + "\n"
    result = invoke(write_ticket(tmp_path, content))
    assert result.returncode == 0, result.stdout + result.stderr


def test_status_metadata_alone_is_not_a_scope_paragraph(tmp_path):
    content = ticket().replace("Cover texture query results with explicit mip levels.", "**Status:** ready")
    assert_rule(invoke(write_ticket(tmp_path, content)), "SCOPE")


def test_nested_h1_is_not_a_standalone_ticket_title(tmp_path):
    content = ticket().replace("# [HLSL]", "> # [HLSL]", 1)
    assert_rule(invoke(write_ticket(tmp_path, content)), "TITLE")


def test_multiline_reference_diagnostic_uses_the_link_line(tmp_path):
    content = ticket().replace(
        "Observe [Texture2D.GetDimensions][language] output types.",
        "Observe the parameter rules\nfor [this method][undefined] output types.",
    )
    path = write_ticket(tmp_path, content)
    result = invoke(path)
    line = next(number for number, value in enumerate(content.splitlines(), 1) if "[this method]" in value)
    assert_rule(result, "REFERENCE_UNDEFINED")
    assert f"{path}:{line}: REFERENCE_UNDEFINED:" in result.stdout


def test_official_shader_model_language_page_qualifies(tmp_path):
    content = ticket().replace(
        "https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/sm5-object-texture2d-getdimensions",
        "https://learn.microsoft.com/en-us/windows/win32/direct3d12/hlsl-shader-model-6-8-features",
    )
    result = invoke(write_ticket(tmp_path, content))
    assert result.returncode == 0, result.stdout + result.stderr


def test_unreadable_nested_directory_is_an_input_error(tmp_path):
    root = tmp_path / "offload-test-suite"
    write_ticket(tmp_path, repo="offload-test-suite")
    blocked = root / "restricted"
    blocked.mkdir()
    (blocked / "hidden.md").write_text(ticket("offload-test-suite"), encoding="utf-8")
    blocked.chmod(0)
    try:
        try:
            list(blocked.iterdir())
        except PermissionError:
            pass
        else:
            pytest.skip("filesystem permissions are not enforced for this user")
        result = invoke(root)
        assert result.returncode == 2, result.stdout + result.stderr
        assert "INPUT" in result.stderr and str(blocked) in result.stderr
        assert "Traceback" not in result.stderr
    finally:
        blocked.chmod(0o700)


def test_raw_slash_revision_reports_ambiguity_not_an_invented_file_label(tmp_path):
    content = ticket().replace(
        "[`clang/lib/CodeGen/CGHLSLBuiltins.cpp`][source]", "[README.md][source]"
    ).replace(
        "https://github.com/llvm/llvm-project/blob/main/clang/lib/CodeGen/CGHLSLBuiltins.cpp",
        "https://github.com/llvm/llvm-project/blob/release/20.x/README.md",
    )
    result = invoke(write_ticket(tmp_path, content))
    assert_rule(result, "GITHUB_REVISION")
    assert "FILE_LABEL" not in result.stdout


def test_encoded_slash_revision_preserves_the_repository_root_path(tmp_path):
    content = ticket().replace(
        "[`clang/lib/CodeGen/CGHLSLBuiltins.cpp`][source]", "[README.md][source]"
    ).replace(
        "https://github.com/llvm/llvm-project/blob/main/clang/lib/CodeGen/CGHLSLBuiltins.cpp",
        "https://github.com/llvm/llvm-project/blob/release%2F20.x/README.md",
    )
    result = invoke(write_ticket(tmp_path, content))
    assert result.returncode == 0, result.stdout + result.stderr


def test_encoded_revision_does_not_allow_a_basename_only_label(tmp_path):
    content = ticket().replace(
        "[`clang/lib/CodeGen/CGHLSLBuiltins.cpp`][source]", "[CGHLSLBuiltins.cpp][source]"
    ).replace("/blob/main/", "/blob/release%2F20.x/")
    assert_rule(invoke(write_ticket(tmp_path, content)), "FILE_LABEL")
