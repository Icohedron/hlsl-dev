"""Host-side shader compilation for compiler-free offload archives.

Only simple RUN lines with known substitutions are run on the host. Unhandled
compiles remain in the report, and their target replay fails explicitly.
"""

import json
import os
from pathlib import Path
import re
import shlex
import subprocess

from .build_support import BuildError


RUN = re.compile(r"^[#/]+ *RUN: *(.*)$")
CONDITIONAL = re.compile(
    r"%if\s+([A-Za-z0-9_-]+)\s*%\{([^%]*)%\}"
    r"(?:\s*%else\s*%\{([^%]*)%\})?"
)
SUBSTITUTION = re.compile(r"%[A-Za-z_][A-Za-z0-9_]*")


class Unsupported(Exception):
    """A compile line cannot safely be reproduced on the packaging host."""


def _lines(test):
    lines = []
    for raw in test.read_text(encoding="utf-8", errors="replace").splitlines():
        match = RUN.match(raw)
        if not match:
            continue
        text = match.group(1).strip()
        if lines and lines[-1].endswith("\\"):
            lines[-1] = lines[-1][:-1].rstrip() + " " + text
        else:
            lines.append(text)
    return lines


def _expand(line, test, scratch, features):
    known = {"Clang", "DXC", "Vulkan", "DirectX", "Metal"}

    def conditional(match):
        feature = match.group(1)
        if feature not in known:
            raise Unsupported(f"device-dependent %if {feature}")
        return (match.group(2) if feature in features else match.group(3)) or ""

    line = CONDITIONAL.sub(conditional, line)
    if "%if " in line:
        raise Unsupported("unrecognized %if clause")
    line = line.replace("%basename_t", scratch.name)
    line = line.replace("%s", str(test)).replace("%t", str(scratch))
    if SUBSTITUTION.search(line):
        raise Unsupported(f"unknown substitution: {SUBSTITUTION.search(line).group()}")
    return line


def _output(argv, scratch):
    outputs = []
    for index, word in enumerate(argv):
        if word == "-Fo" and index + 1 < len(argv):
            outputs.append(Path(argv[index + 1]).absolute())
        elif word.startswith("-Fo") and len(word) > 3:
            outputs.append(Path(word[3:]).absolute())
    if not outputs:
        raise Unsupported("compile line has no -Fo")
    for output in outputs:
        if output.is_relative_to(scratch):
            raise Unsupported("-Fo writes inside %t, which split-file recreates")
    return outputs


def _compile_words(words):
    """Locate a compile behind lit's plain negation, without running wrappers."""
    compiler = {"%dxc_target", "%dxc_target_lib"}
    if not any(word in compiler for word in words):
        return None
    index = 0
    while index < len(words) and words[index] in ("not", "!"):
        index += 1
    if index == len(words) or words[index] not in compiler:
        raise Unsupported(f"unsupported compiler wrapper: {' '.join(words[:index + 1])}")
    return words[index + 1:]


def _run(argv):
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace", check=False)
    except OSError as error:
        raise BuildError(f"cannot execute {argv[0]}: {error}") from error


def compile_suite(tests, destination, compiler, split_file, flags, features):
    """Compile installed tests into their future lit execution tree; return report."""
    compiled, skipped, objects = {}, {}, {}
    for test in sorted(tests.rglob("*")):
        if (not test.is_file() or test.suffix not in (".test", ".yaml")
                or set(test.relative_to(tests).parts) & {"Inputs", "Output"}):
            continue
        rel = test.relative_to(tests).as_posix()
        scratch = destination / test.relative_to(tests).parent / "Output" / (test.name + ".tmp")
        scratch.parent.mkdir(parents=True, exist_ok=True)
        produced = []
        try:
            for line in _lines(test):
                try:
                    words = shlex.split(line)
                except ValueError as error:
                    raise Unsupported(f"invalid RUN line: {error}") from error
                if not words:
                    continue
                compile_args = _compile_words(words)
                if compile_args is None and words[0] not in ("split-file", "%split-file"):
                    continue
                # Do not interpret a shell fragment as compiler arguments. In
                # particular, shlex.split() alone hides quoted operators.
                if any(char in line for char in "|&;<>`$\\\n\r"):
                    raise Unsupported("RUN line requires a shell")
                args = words[1:] if compile_args is None else compile_args
                expanded = shlex.split(_expand(
                    " ".join(shlex.quote(word) for word in args), test, scratch, features))
                if compile_args is None:
                    result = _run([str(split_file), *expanded])
                    if result.returncode:
                        raise Unsupported(f"split-file failed: {result.stdout[-200:]}")
                    continue
                args = [*flags, *expanded]
                outputs = _output(args, scratch)
                if any(not output.is_relative_to(destination) for output in outputs):
                    raise Unsupported("-Fo outside package")
                result = _run([str(compiler), *args])
                for output in outputs:
                    key = output.relative_to(destination).as_posix()
                    objects[key] = {"status": result.returncode, "output": result.stdout}
                    produced.append(key)
                    if result.returncode:
                        output.unlink(missing_ok=True)
            if not produced:
                raise Unsupported("no compile step")
            compiled[rel] = produced
        except Unsupported as error:
            # Do not leave any partially compiled test appearing successful.
            for key in produced:
                (destination / key).unlink(missing_ok=True)
                objects.pop(key, None)
            skipped[rel] = str(error)
    report = {"compiled": compiled, "skipped": skipped, "objects": objects}
    (destination / "precompiled.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if not compiled:
        raise BuildError(f"{tests}: no tests could be precompiled; see skipped tests")
    return report


def suite_tools(name, native, dxc):
    """Return host executable, lit-equivalent flags and fixed suite features."""
    if name not in (
        "clang-vk", "clang-d3d12", "clang-warp-d3d12", "clang-mtl",
        "vk", "d3d12", "warp-d3d12", "mtl",
    ):
        raise BuildError(f"unsupported precompiled suite: {name}")
    clang = name.startswith("clang-")
    api = ("Vulkan" if name.endswith("vk") else "Metal" if name.endswith("mtl")
           else "DirectX")
    flags = ["-spirv", "-fspv-target-env=vulkan1.3"] if api == "Vulkan" else []
    if clang and api == "Vulkan":
        flags.append("-fspv-extension=DXC")
    if clang:
        compiler = native / "clang-dxc"
        if dxc:
            flags.append(f"--dxv-path={dxc}")
    else:
        if not dxc:
            raise BuildError(f"{name} requires native DXC; pass --dxc <bin directory>")
        compiler = dxc / "dxc"
    if not compiler.is_file():
        raise BuildError(f"native compiler missing: {compiler}")
    return compiler, flags, {"Clang" if clang else "DXC", api}
