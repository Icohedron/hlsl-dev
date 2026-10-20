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


REPOSITORIES = ("llvm-project", "offload-test-suite", "wg-hlsl")
HISTORY = {"history", "discussion", "comments", "historical discussion"}
SECTIONS = ("Acceptance Criteria", "Notes", "References")
TECHNOLOGIES = {"DirectX", "SPIR-V", "Vulkan", "Metal", "HLSL"}
REFERENCE_GROUPS = ("HLSL", "DirectX", "SPIR-V", "Vulkan", "Metal", "Offload test suite")
DETAILS_SECTION = "Suggested Implementation"
DETAILS_SUMMARY = "Show details"
COLLAPSED = re.compile(r"<details>\s*<summary>([^<]+)</summary>")
METADATA_OPENER = "Filing metadata."
METADATA_FIELDS = ("Repository", "Parent", "Blocked by", "Status")
METADATA_OPTIONAL = "Existing issue"
DISCLAIMER = "_Disclaimer: This issue was written with the help of AI and may contain some inaccuracies._"


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
    tokens = parser.parse(text)
    findings = []

    def report(line, rule, message):
        findings.append(Diagnostic(str(source), line + offset, rule, message))

    headings = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            headings.append(
                (index, int(token.tag[1:]), visible(tokens[index + 1].children or []).strip(), token.map[0] + 1)
            )
    for _, _, title, line in headings:
        if title.casefold() in HISTORY:
            report(line, "HISTORY", "Remove ticket history; describe only the current work.")

    def section_at(index):
        """The level-two section containing a token, or None before the first one."""
        for position, level, title, _ in reversed(headings):
            if position < index and level <= 2:
                return title if level == 2 else None
        return None

    def opener(token):
        return token.level == 0 and (token.type.endswith("_open") or token.type in {"fence", "code_block", "hr", "html_block"})

    blocks = []
    opened = None
    for index, token in enumerate(tokens):
        if token.type != "html_block" or token.level != 0:
            continue
        content = token.content.strip()
        line = token.map[0] + 1
        if content.startswith("<details"):
            if opened is not None:
                report(line, "DETAILS", "Close each collapsed block before opening another one.")
            match = COLLAPSED.fullmatch(content)
            if not match:
                report(line, "DETAILS", "Open a collapsed block with <details> and a one-line <summary>, followed by a blank line.")
            opened = (index, match[1].strip() if match else None, line)
        elif content.startswith("</details>"):
            if opened is None:
                report(line, "DETAILS", "Remove the </details> line that closes no collapsed block.")
            else:
                blocks.append((opened[0], index, opened[1], opened[2]))
                opened = None
    if opened is not None:
        report(opened[2], "DETAILS", "Close the collapsed block with a </details> line.")
    boundaries = {index for start, end, _, _ in blocks for index in (start, end)}

    def block_at(index):
        return next((block for block in blocks if block[0] < index < block[1]), None)

    def in_details(index):
        """Whether a token sits under Notes' Suggested Implementation subsection."""
        for position, level, title, _ in reversed(headings):
            if position < index and level <= 3 and not block_at(position):
                return level == 3 and title == DETAILS_SECTION and section_at(position) == "Notes"
        return False

    titles = [heading for heading in headings if heading[1] == 1]
    tags = []
    if len(titles) != 1:
        report(1, "TITLE", "Provide exactly one descriptive level-one title.")
    else:
        title_index, _, title, title_line = titles[0]
        if tokens[title_index].level != 0 or block_at(title_index):
            report(title_line, "TITLE", "Make the title a standalone top-level H1 after the filing metadata.")
        lead = [index for index in range(title_index) if opener(tokens[index])]
        valid = [tokens[index].type for index in lead] == ["paragraph_open", "bullet_list_open", "hr"] and visible(
            tokens[lead[0] + 1].children or []
        ).startswith(METADATA_OPENER)
        if valid:
            items = []
            list_end = next(
                index for index in range(lead[1], title_index)
                if tokens[index].type == "bullet_list_close" and tokens[index].level == 0
            )
            for index in range(lead[1], list_end):
                if tokens[index].type != "list_item_open" or tokens[index].level != 1:
                    continue
                close = next(
                    item for item in range(index, list_end)
                    if tokens[item].type == "list_item_close" and tokens[item].level == 1
                )
                inline = next((tokens[item] for item in range(index, close) if tokens[item].type == "inline"), None)
                nested = any(tokens[item].type == "bullet_list_open" for item in range(index, close))
                field, _, value = visible(inline.children or []).strip().partition(":") if inline else ("", "", "")
                items.append((field.strip(), bool(value.strip()) or nested))
            fields = [field for field, _ in items]
            if fields[:1] == [METADATA_OPTIONAL]:
                fields = fields[1:]
            valid = fields == list(METADATA_FIELDS) and all(filled for _, filled in items)
        if not valid:
            report(1, "METADATA", f"Start with the filing metadata: a '{METADATA_OPENER}' paragraph, filled bullets for an optional {METADATA_OPTIONAL} and then {', '.join(METADATA_FIELDS)} in that order, and a --- line before the title.")
        rest = title
        while match := re.match(r"^\[([^\]]+)\]", rest):
            tags.append(match[1])
            rest = rest[match.end() :]
            if re.match(r"^\s+\[", rest):
                report(title_line, "PREFIX", "Put adjacent title tags together without spaces.")
                rest = rest.lstrip()
        if repo == "wg-hlsl":
            if len(tags) > 1 or any(not re.fullmatch(r"\d{4}", tag) for tag in tags):
                report(title_line, "PREFIX", "Use no title tag for a new wg-hlsl proposal, or one [NNNN] proposal-number tag for an amendment.")
        else:
            allowed = {"HLSL", "DirectX", "SPIRV", "NFC"} if repo == "llvm-project" else {"DirectX", "Vulkan", "Metal"}
            if not tags or len(tags) != len(set(tags)) or any(tag not in allowed for tag in tags):
                report(title_line, "PREFIX", f"Use applicable unique {repo} title tags: {', '.join(sorted(allowed))}.")
        if not any(character.isalpha() for character in rest):
            report(title_line, "TITLE", "Add a descriptive deliverable after the title tags.")
        next_heading = next((item[0] for item in headings if item[0] > title_index), len(tokens))
        if not any(
            tokens[index].type == "paragraph_open" and tokens[index].level == 0
            for index in range(title_index + 3, next_heading)
        ):
            report(title_line, "SCOPE", "Follow the title with a short deliverable paragraph.")

    sections = [(title, line) for _, level, title, line in headings if level == 2]
    section_names = {title for title, _ in sections}
    rank = -1
    for title, line in sections:
        if title in SECTIONS and SECTIONS.index(title) > rank:
            rank = SECTIONS.index(title)
        else:
            report(line, "SECTION_ORDER", f"Use only {', '.join(SECTIONS)} level-two sections, once each and in that order.")
    if "Acceptance Criteria" not in section_names:
        report(1, "ACCEPTANCE", "Add a level-two Acceptance Criteria section.")
    if "References" not in section_names:
        report(1, "SECTION", "Add a level-two References section after the criteria and Notes.")

    reference_sections = {}
    for start, end, summary, line in blocks:
        for index, level, _, heading_line in headings:
            if start < index < end and level <= 2:
                report(heading_line, "DETAILS_ORDER", "Use level-three or lower headings inside collapsed blocks.")
        section = section_at(start)
        if in_details(start):
            if summary is not None and summary != DETAILS_SUMMARY:
                report(line, "DETAILS", f"Use <summary>{DETAILS_SUMMARY}</summary> for the {DETAILS_SECTION} block.")
            for index, level, _, heading_line in headings:
                if start < index < end and level == 3:
                    report(heading_line, "DETAILS_ORDER", f"Use level-four or lower headings inside the {DETAILS_SECTION} block.")
        elif section == "References":
            if summary is not None and summary not in REFERENCE_GROUPS:
                report(line, "REFERENCE_FORMAT", f"Name each References block with one of: {', '.join(REFERENCE_GROUPS)}.")
            elif summary in reference_sections:
                report(line, "REFERENCE_FORMAT", f"Use one {summary} block in References.")
            elif summary is not None:
                reference_sections[summary] = line
            for index in range(start + 1, end):
                if opener(tokens[index]) and tokens[index].type != "bullet_list_open":
                    report(tokens[index].map[0] + 1, "REFERENCE_FORMAT", "Write each reference as a bullet inside its collapsed block.")
        else:
            report(line, "DETAILS_ORDER", f"Use collapsed blocks only under the {DETAILS_SECTION} subsection of Notes and in References.")
    for position, level, title, line in headings:
        if level != 3 or title != DETAILS_SECTION or block_at(position):
            continue
        if section_at(position) != "Notes":
            report(line, "DETAILS_ORDER", f"Put {DETAILS_SECTION} under Notes.")
            continue
        following = next((heading for heading in headings if heading[0] > position and heading[1] <= 3 and not block_at(heading[0])), None)
        if following is not None and following[1] == 3:
            report(following[3], "DETAILS_ORDER", f"Make {DETAILS_SECTION} the last part of Notes.")
        if sum(1 for start, _, _, _ in blocks if start > position and in_details(start)) != 1:
            report(line, "DETAILS", f"Give {DETAILS_SECTION} exactly one collapsed block.")

    paragraphs = [
        index for index, token in enumerate(tokens)
        if token.type == "paragraph_open" and token.level == 0 and not block_at(index)
    ]
    disclaimer = paragraphs[-1] if paragraphs else None
    if (
        disclaimer is None
        or tokens[disclaimer + 1].content.strip() != DISCLAIMER
        or section_at(disclaimer) != "References"
        or any(end > disclaimer for _, end, _, _ in blocks)
    ):
        disclaimer = None
        report(1, "DISCLAIMER", f"End the body, after References and before the link definitions, with: {DISCLAIMER}")
    for index, token in enumerate(tokens):
        if not opener(token) or index in boundaries or index == disclaimer or block_at(index):
            continue
        if token.type == "heading_open" and token.tag in {"h2", "h3"}:
            continue
        if in_details(index):
            report(token.map[0] + 1, "DETAILS", f"Put all {DETAILS_SECTION} content inside its one collapsed block.")
        elif section_at(index) == "References":
            report(token.map[0] + 1, "REFERENCE_FORMAT", "Group References links in collapsed blocks named by subsection.")
    for index, level, _, line in headings:
        if level == 3 and section_at(index) == "References" and not block_at(index):
            report(line, "REFERENCE_FORMAT", "Group References links in collapsed blocks named by subsection.")

    if "HLSL" in tags and "HLSL" not in reference_sections:
        report(1, "SECTION", "Add an HLSL block under References for HLSL-tagged work.")
    wrong_role = {"llvm-project": "Vulkan", "offload-test-suite": "SPIR-V"}.get(repo)
    if wrong_role in reference_sections:
        report(reference_sections[wrong_role], "OWNER_SECTION", f"Use {'SPIR-V compiler' if repo == 'llvm-project' else 'Vulkan runtime'} blocks for {repo}, not {wrong_role}.")

    used = set()
    section_links = {}
    acceptance_items = 0
    for index, token in enumerate(tokens):
        if token.type != "inline":
            continue
        if index and tokens[index - 1].type == "heading_open" and tokens[index - 1].tag == "h1":
            # Adjacent technology tags have bracket syntax, but are not links.
            continue
        children = token.children or []
        line = token.map[0] + 1
        block = block_at(index)
        section = section_at(index)
        subsection = block[2] if block and section == "References" else None
        if re.match(r"^\[[ xX]\](?:\s|$)", token.content) and any(
            item.type == "list_item_open" and item.map[0] == token.map[0]
            for item in tokens[:index]
        ):
            if block:
                report(line, "DETAILS_CHECKBOX", "Use plain bullets inside collapsed blocks; GitHub counts checkboxes as task progress.")
            elif section == "Acceptance Criteria":
                acceptance_items += 1
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
            if subsection:
                section_links.setdefault(subsection, []).append(href)
            if parsed.hostname == "github.com":
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 5 and parts[2] == "blob":
                    end = child_index + 1
                    while end < len(children) and children[end].type != "link_close":
                        end += 1
                    text_label = visible(children[child_index + 1 : end]) if child.type == "link_open" else visible(child.children or [])
                    expected = unquote("/".join(parts[4:]))
                    if (parts[0], parts[1]) != ("llvm", repo):
                        expected = f"{parts[0]}/{parts[1]}:{expected}"
                    if lines := re.fullmatch(r"L(\d+)(?:-L(\d+))?", parsed.fragment):
                        expected += f":L{lines[1]}" + (f"-{lines[2]}" if lines[2] else "")
                    elif parsed.fragment:
                        expected += f"#{unquote(parsed.fragment)}"
                    if text_label != expected:
                        revision = parts[3]
                        unambiguous = (
                            re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", revision)
                            or "/" in unquote(revision)
                        )
                        if unambiguous:
                            report(link_line, "FILE_LABEL", f"Label this GitHub file link with its full repo-root path and location: {expected}.")
                        else:
                            report(link_line, "GITHUB_REVISION", "A named revision may contain slashes; use a full commit SHA or percent-encode the reference's slashes before checking this repo-root label.")
    if "Acceptance Criteria" in section_names and not acceptance_items:
        report(1, "ACCEPTANCE", "Add at least one acceptance checklist goal.")

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
    for name in sorted(TECHNOLOGIES & reference_sections.keys()):
        links = section_links.get(name, [])
        if not links:
            report(reference_sections[name], "SECTION_REFERENCE", f"Link {name} documentation inside its own References block.")
        elif name == "HLSL" and not any(language_reference(url) for url in links):
            report(reference_sections[name], "HLSL_LANGUAGE", "Link official HLSL language documentation, a language proposal or shader-model language reference inside the HLSL block; backend files alone do not qualify.")
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
            "CommonMark text. History/Discussion/Comments sections are findings."
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
