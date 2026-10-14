#!/usr/bin/env python3
"""Check explicit human-ticket Markdown inputs, without granting semantic approval."""

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.common.utils import normalizeReference
from markdown_it.rules_inline.autolink import autolink
from markdown_it.rules_inline.image import image
from markdown_it.rules_inline.link import link as parse_link


REPOSITORIES = ("llvm-project", "offload-test-suite")
HISTORY = {"history", "discussion", "comments", "historical discussion"}


@dataclass(frozen=True)
class Diagnostic:
    source: str
    line: int
    rule: str
    message: str

    def __str__(self):
        return f"{self.source}:{self.line}: {self.rule}: {self.message}"


def visible(tokens):
    return "".join(
        token.content
        for token in tokens
        if token.type in {"text", "code_inline", "unresolved_reference"}
    )


def tracked_link(state, silent):
    """Keep parser-resolved reference identity and explicit unresolved attempts."""
    start = state.pos
    first_token = len(state.tokens)
    if parse_link(state, silent):
        if not silent:
            for token in state.tokens[first_token:]:
                if token.type == "link_open" and "offset" not in token.meta:
                    token.meta["offset"] = start
        return True
    if state.src[start : start + 1] != "[" or state.linkLevel:
        return False
    end = state.md.helpers.parseLinkLabel(state, start, True)
    if end < 0 or state.src[end + 1 : end + 2] != "[":
        # An undefined shortcut is plain CommonMark text, not a link. In
        # particular, source expressions and checkbox markers stay ordinary text.
        return False
    ref_end = state.md.helpers.parseLinkLabel(state, end + 1)
    if ref_end < 0:
        return False
    label = state.src[end + 2 : ref_end] or state.src[start + 1 : end]
    if not silent:
        token = state.push("unresolved_reference", "", 0)
        token.content = state.src[start : ref_end + 1]
        token.meta = {"label": label, "offset": start}
    state.pos = ref_end + 1
    return True


def positioned(rule):
    """Attach source offsets to parser-produced image and autolink tokens."""
    def parse(state, silent):
        start, first_token = state.pos, len(state.tokens)
        matched = rule(state, silent)
        if matched and not silent:
            for token in state.tokens[first_token:]:
                if token.type in {"link_open", "image"}:
                    token.meta["offset"] = start
        return matched

    return parse


def markdown():
    parser = MarkdownIt("commonmark", {"store_labels": True, "inline_definitions": True})
    # File URLs must remain tokens so they can be rejected rather than silently
    # becoming ordinary text under MarkdownIt's renderer-safety policy.
    parser.validateLink = lambda destination: True
    parser.inline.ruler.at("link", tracked_link)
    parser.inline.ruler.at("image", positioned(image))
    parser.inline.ruler.at("autolink", positioned(autolink))
    return parser


def active_text(text, parser):
    """Exclude explicitly headed historical discussion without moving lines."""
    tokens = parser.parse(text)
    lines = text.splitlines(keepends=True)
    original_lines = list(lines)
    ignored_level = None
    ignored_start = 0
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        level = int(token.tag[1:])
        if ignored_level is not None and level <= ignored_level:
            for line in range(ignored_start, token.map[0]):
                lines[line] = "\n"
            ignored_level = None
        title = visible(tokens[index + 1].children or []).strip().casefold()
        if ignored_level is None and title in HISTORY:
            ignored_level = level
            ignored_start = token.map[0]
    if ignored_level is not None:
        for line in range(ignored_start, len(lines)):
            lines[line] = "\n"
    # Definitions at the shared footer can follow a History section. Keep the
    # parser's real definitions available to both active and preserved links.
    for token in tokens:
        if token.type == "definition":
            start, end = token.map
            lines[start:end] = original_lines[start:end]
    preserved_uses = {
        normalizeReference(child.meta["label"]).casefold()
        for token in tokens
        if token.type == "inline"
        for child in token.children or []
        if child.type in {"link_open", "image"} and child.meta.get("label")
    }
    return "".join(lines), preserved_uses


def language_reference(url):
    """Recognize official language reference families, not backend file names."""
    parsed = urlsplit(url)
    host, path = parsed.hostname or "", unquote(parsed.path).casefold()
    if parsed.scheme != "https":
        return False
    if host == "learn.microsoft.com" and "/windows/win32/direct3dhlsl/" in path:
        page = path.rsplit("/", 1)[-1]
        return bool(page) and not any(word in page for word in ("dxil", "spirv", "spir-v"))
    if host == "learn.microsoft.com" and re.search(
        r"/windows/win32/direct3d12/hlsl-shader-model-[^/]+$", path
    ):
        return True
    if host == "github.com":
        parts = path.strip("/").split("/")
        if (
            len(parts) >= 6
            and parts[:3] == ["microsoft", "directx-specs", "blob"]
            and parts[4] == "d3d"
            and parts[-1].startswith(("hlsl_", "shadermodel"))
            and parts[-1].endswith(".md")
        ):
            return True
        return (
            len(parts) >= 5
            and tuple(parts[:2]) in {
                ("llvm", "wg-hlsl"),
                ("microsoft", "hlsl-specs"),
                ("hlsl-tc57", "tc57"),
            }
            and parts[2] == "blob"
            and parts[4] in {"proposals", "spec", "specs"}
        )
    return (
        (host == "microsoft.github.io" and path.startswith("/hlsl-specs/"))
        or (host == "llvm.github.io" and path.startswith("/wg-hlsl/"))
        or (host == "hlsl-tc57.github.io" and path.startswith("/tc57/"))
    )


def validate(text, repo, source, offset=0):
    parser = markdown()
    text, preserved_uses = active_text(text, parser)
    env = {}
    tokens = parser.parse(text, env)
    findings = []

    def report(line, rule, message):
        findings.append(Diagnostic(str(source), line + offset, rule, message))

    headings = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            headings.append(
                (index, int(token.tag[1:]), visible(tokens[index + 1].children or []).strip(), token.map[0] + 1)
            )
    titles = [heading for heading in headings if heading[1] == 1]
    tags = []
    if len(titles) != 1:
        report(1, "TITLE", "Provide exactly one descriptive level-one title.")
    else:
        title_index, _, title, title_line = titles[0]
        first_block = next(
            (item for item in tokens if item.type not in {"html_block", "definition"}),
            None,
        )
        if tokens[title_index].level != 0 or first_block is not tokens[title_index]:
            report(title_line, "TITLE", "Start the active ticket with a standalone top-level H1.")
        rest = title
        while match := re.match(r"^\[([^\]]+)\]", rest):
            tags.append(match[1])
            rest = rest[match.end() :]
            if re.match(r"^\s+\[", rest):
                report(title_line, "PREFIX", "Put adjacent title tags together without spaces.")
                rest = rest.lstrip()
        allowed = {"HLSL", "DirectX", "SPIRV", "NFC"} if repo == "llvm-project" else {"DirectX", "Vulkan", "Metal"}
        if not tags or len(tags) != len(set(tags)) or any(tag not in allowed for tag in tags):
            report(title_line, "PREFIX", f"Use applicable unique {repo} title tags: {', '.join(sorted(allowed))}.")
        if not any(character.isalpha() for character in rest):
            report(title_line, "TITLE", "Add a descriptive deliverable after the title tags.")
        next_heading = next((item[0] for item in headings if item[0] > title_index), len(tokens))
        scope = next(
            (
                tokens[index + 1]
                for index in range(title_index + 3, next_heading)
                if tokens[index].type == "paragraph_open" and tokens[index].level == 0
            ),
            None,
        )
        if scope is not None and re.match(
            r"^(?:status|state|labels|assignee):", visible(scope.children or []).strip(), re.I
        ):
            scope = None
        if not scope:
            report(title_line, "SCOPE", "Follow the title with a short deliverable paragraph.")

    section_names = {title for _, level, title, _ in headings if level == 2}
    if "Acceptance Criteria" not in section_names:
        report(1, "ACCEPTANCE", "Add a level-two Acceptance Criteria section.")
    required = set()
    if "DirectX" in tags:
        required.add("DirectX")
    if "SPIRV" in tags:
        required.add("SPIR-V")
    if "Vulkan" in tags:
        required.add("Vulkan")
    if "Metal" in tags:
        required.add("Metal")
    if set(tags) - {"NFC"}:
        required.add("HLSL")
    for name in sorted(required - section_names):
        report(1, "SECTION", f"Add a level-two {name} section for the title's technologies.")
    wrong_role = "Vulkan" if repo == "llvm-project" else "SPIR-V"
    if wrong_role in section_names:
        line = next(item[3] for item in headings if item[2] == wrong_role)
        report(line, "OWNER_SECTION", f"Use {'SPIR-V compiler' if repo == 'llvm-project' else 'Vulkan runtime'} sections for {repo}, not {wrong_role}.")

    used = set(preserved_uses)
    section = None
    section_links = {}
    group_count = 0
    acceptance = False
    acceptance_items = 0
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            title = visible(tokens[index + 1].children or []).strip()
            if token.tag in {"h1", "h2", "h3"}:
                group_count = 0
            if token.tag in {"h1", "h2"}:
                section = title if token.tag == "h2" else None
                acceptance = section == "Acceptance Criteria"
        if token.type != "inline":
            continue
        if index and tokens[index - 1].type == "heading_open" and tokens[index - 1].tag == "h1":
            # Adjacent technology tags have bracket syntax, but are not links.
            continue
        children = token.children or []
        line = token.map[0] + 1
        if re.match(r"^\[[ xX]\](?:\s|$)", token.content) and any(
            item.type == "list_item_open" and item.map[0] == token.map[0]
            for item in tokens[:index]
        ):
            group_count += 1
            if acceptance:
                acceptance_items += 1
            if group_count == 6:
                report(line, "CHECKLIST_GROUP", "Split this checklist into explicit H2/H3 groups of at most five items.")
        for child_index, child in enumerate(children):
            position = child.meta.get("offset", 0)
            link_line = line + token.content[:position].count("\n")
            if child.type == "unresolved_reference":
                report(link_line, "REFERENCE_UNDEFINED", f"Define reference [{child.meta['label']}] at the document footer.")
                continue
            if child.type not in {"link_open", "image"}:
                continue
            href = child.attrGet("href") if child.type == "link_open" else child.attrGet("src")
            href = href or ""
            label = child.meta.get("label")
            if label:
                used.add(normalizeReference(label).casefold())
            else:
                report(link_line, "REFERENCE_STYLE", "Use a reference-style link with a footer definition, not an inline link or autolink.")
            parsed = urlsplit(href)
            if parsed.scheme not in {"https", "http", "mailto"} or parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
                report(link_line, "LOCAL_LINK", "Replace local-file, local-ticket or workspace links with a published reference.")
            if section:
                section_links.setdefault(section, []).append(href)
            if parsed.hostname == "github.com":
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 5 and parts[2] == "blob":
                    end = child_index + 1
                    while end < len(children) and children[end].type != "link_close":
                        end += 1
                    text_label = visible(children[child_index + 1 : end]) if child.type == "link_open" else visible(child.children or [])
                    expected = unquote("/".join(parts[4:]))
                    if text_label != expected:
                        revision = parts[3]
                        unambiguous = (
                            re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision)
                            or "/" in unquote(revision)
                        )
                        if unambiguous:
                            report(link_line, "FILE_LABEL", f"Label this GitHub file link with its full repo-root path: {expected}.")
                        else:
                            report(link_line, "GITHUB_REVISION", "A named revision may contain slashes; use a full commit SHA or percent-encode the reference's slashes before checking this repo-root label.")
    if "Acceptance Criteria" in section_names and not acceptance_items:
        report(1, "ACCEPTANCE", "Add at least one acceptance checklist action; historical checked items may stay checked.")

    definitions = {}
    for token in tokens:
        if token.type != "definition":
            continue
        label = normalizeReference(token.meta["label"]).casefold()
        line = token.map[0] + 1
        if label in definitions:
            report(line, "REFERENCE_COLLISION", f"Reference [{token.meta['label']}] duplicates a casefold-equivalent definition on line {definitions[label] + offset}.")
        else:
            definitions[label] = line
        if label not in used:
            report(line, "REFERENCE_UNUSED", f"Remove unused reference [{token.meta['label']}], or use it in the active ticket.")
    for name in sorted(required & section_names):
        links = section_links.get(name, [])
        if not links:
            heading_line = next(item[3] for item in headings if item[2] == name)
            report(heading_line, "SECTION_REFERENCE", f"Link {name} documentation inside its own section.")
        elif name == "HLSL" and not any(language_reference(url) for url in links):
            heading_line = next(item[3] for item in headings if item[2] == name)
            report(heading_line, "HLSL_LANGUAGE", "Link official HLSL language documentation, a language proposal or shader-model language reference inside HLSL; backend files alone do not qualify.")
    return sorted(findings, key=lambda finding: (finding.line, finding.rule, finding.message))


def infer_repo(path):
    for parent in (path.parent, *path.parents):
        if parent.name in REPOSITORIES:
            return parent.name
    return None


def main(argv=None):
    arguments = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Checks explicit full/collapsed reference resolution and resolved "
            "shortcut links. Undefined bare [expressions] remain ordinary "
            "CommonMark text. History/Discussion/Comments sections are preserved "
            "but not validated as active requirements."
            " GitHub label mismatches on named refs require a full commit SHA "
            "or percent-encoded reference slashes to avoid guessing a file path."
        ),
    )
    arguments.add_argument("--repo", choices=REPOSITORIES)
    arguments.add_argument("--examples", type=Path, action="append", default=[], metavar="SKILL.md")
    arguments.add_argument("paths", type=Path, nargs="*")
    options = arguments.parse_args(argv)
    if not options.paths and not options.examples:
        arguments.error("provide explicit Markdown paths/directories or --examples SKILL.md")
    findings = []
    errors = []
    files = set()
    for path in options.paths:
        try:
            if path.is_dir():
                selected = []
                errors_before = len(errors)

                def scan_error(error):
                    errors.append(f"{path}:1: INPUT: {error}")

                for root, directories, names in os.walk(path, onerror=scan_error):
                    directories.sort()
                    selected.extend(Path(root) / name for name in names if name.endswith(".md"))
                if not selected and len(errors) == errors_before:
                    errors.append(f"{path}:1: INPUT: directory selects no Markdown files")
                files.update(selected)
            elif path.is_file():
                files.add(path)
            else:
                errors.append(f"{path}:1: INPUT: file or directory does not exist")
        except OSError as error:
            errors.append(f"{path}:1: INPUT: {error}")
    for path in sorted(files):
        try:
            repo = options.repo or infer_repo(path.resolve())
            if repo is None:
                errors.append(f"{path}:1: OWNER: cannot infer owner; pass --repo")
                continue
            findings.extend(validate(path.read_text(encoding="utf-8"), repo, path))
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{path}:1: INPUT: {error}")
    for path in options.examples:
        try:
            text = path.read_text(encoding="utf-8")
            examples = [
                token for token in markdown().parse(text)
                if token.type == "fence"
                and token.info.strip() in {f"markdown repo={repo}" for repo in REPOSITORIES}
            ]
            if not examples:
                errors.append(f"{path}:1: EXAMPLES: no annotated ticket fences found")
            for token in examples:
                repo = token.info.strip().split("=", 1)[1]
                findings.extend(validate(token.content, repo, path, offset=token.map[0] + 1))
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{path}:1: INPUT: {error}")
    for finding in findings:
        print(finding)
    for error in errors:
        print(error, file=sys.stderr)
    return 2 if errors else int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
