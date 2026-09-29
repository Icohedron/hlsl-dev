"""Package named precompiled offload tests with shell or lit target replay.

Use the small auditable shell subset when possible; otherwise use lit for every
selected test. Never publish an archive with an uncompiled selected test.
"""

from contextlib import ExitStack
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from .build_support import BuildError, build_lock
from .command import Plan, Request
from . import package, precompiled


TOOLS = {"%offloader": "offloader", "imgdiff": "imgdiff",
         "%imgdiff": "imgdiff", "api-query": "api-query",
         "%api-query": "api-query", "obj2yaml": "obj2yaml",
         "%obj2yaml": "obj2yaml"}
COMPILERS = {"%dxc_target", "%dxc_target_lib"}
SPLITTERS = {"split-file", "%split-file"}
DIRECTIVE = re.compile(r"^[#/]+\s*(XFAIL|REQUIRES|UNSUPPORTED):\s*(.*)$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.+/-]+$")


@dataclass(frozen=True)
class Test:
    path: str
    # Each step is (kind, argv), where argv excludes the tool name.
    steps: tuple[tuple[str, tuple[str, ...]], ...]
    target_requirements: tuple[str, ...]


def _select(request, source, checkout):
    if not request.paths:
        raise BuildError("repro needs at least one named test")
    selected = []
    for name in request.paths:
        path = Path(name)
        if path.is_absolute():
            for root in (source, checkout):
                if path.resolve().is_relative_to(root.resolve()):
                    name = path.resolve().relative_to(root.resolve()).as_posix()
                    break
            else:
                raise BuildError(f"test outside {checkout} or {source}: {path}")
        elif name.startswith("test/"):
            name = name[5:]
        if (not SAFE_NAME.fullmatch(name) or Path(name).as_posix() != name
                or any(part in (".", "..", "Output") for part in Path(name).parts)
                or not name.endswith((".test", ".yaml"))):
            raise BuildError(f"unsafe or unsupported test name: {name}")
        file = source / name
        if not file.is_file() or not file.resolve().is_relative_to(source.resolve()):
            raise BuildError(f"no such test under {source}: {name}")
        if name in selected:
            raise BuildError(f"duplicate test: {name}")
        selected.append(name)
    return tuple(selected)


def _parse(test, name, features):
    for parent in (test.parent, *test.parents):
        if (parent / "lit.local.cfg").is_file():
            raise BuildError(f"{name}: lit.local.cfg changes test semantics")
        if parent.name == "test":
            break
    target_requirements = []
    for raw in test.read_text(encoding="utf-8", errors="replace").splitlines():
        match = DIRECTIVE.match(raw)
        if match:
            directive, value = match.group(1), match.group(2).strip()
            if directive == "XFAIL":
                continue  # Reproducers expose failures, including known XFAILs.
            if directive in ("REQUIRES", "UNSUPPORTED") and value:
                # The named repro is run only on a supported target. Preserve
                # lit's conditions as explicit preconditions, not runtime checks.
                target_requirements.append(f"{directive}: {value}")
            else:
                raise BuildError(f"{name}: unsupported {directive}: {value}")
    steps = []
    outputs = set()
    for line in precompiled._lines(test):
        def conditional(match):
            if match.group(1) not in {"Clang", "DXC", "Vulkan", "DirectX", "Metal"}:
                raise BuildError(f"{name}: device-dependent %if {match.group(1)}")
            return (match.group(2) if match.group(1) in features else match.group(3)) or ""

        line = precompiled.CONDITIONAL.sub(conditional, line)
        if "%if" in line:
            raise BuildError(f"{name}: unsupported conditional: {line}")
        # Do not mistake quoted shell operators, pipelines, redirections,
        # assignments or lit's own command wrappers for plain argv commands.
        if any(char in line for char in "|&;<>`$\n\r\\"):
            raise BuildError(f"{name}: unsupported RUN shell syntax: {line}")
        try:
            words = shlex.split(line)
        except ValueError as error:
            raise BuildError(f"{name}: invalid RUN: {error}") from error
        if not words:
            raise BuildError(f"{name}: empty RUN line")
        tool, *args = words
        if tool in SPLITTERS:
            kind = "split"
        elif tool in COMPILERS:
            kind = "compile"
        elif tool in TOOLS:
            kind = TOOLS[tool]
        else:
            raise BuildError(f"{name}: unsupported RUN tool: {tool}")
        if kind == "split" and (args != ["%s", "%t"] or steps):
            raise BuildError(f"{name}: split-file requires 'split-file %s %t' first")
        if kind == "compile":
            if not any(step[0] == "split" for step in steps):
                raise BuildError(f"{name}: compile without a preceding split-file")
            if any(step[0] not in ("split", "compile") for step in steps):
                raise BuildError(f"{name}: compile after runtime step")
            destinations = [args[i + 1] for i, arg in enumerate(args[:-1])
                            if arg == "-Fo"]
            destinations += [arg[3:] for arg in args if arg.startswith("-Fo")
                             and len(arg) > 3]
            if len(destinations) != 1 or destinations[0] in outputs:
                raise BuildError(f"{name}: compile requires one unique -Fo output")
            outputs.add(destinations[0])
        if kind not in ("split", "compile") and not any(
                step[0] == "compile" for step in steps):
            raise BuildError(f"{name}: runtime step without a compile")
        for arg in args:
            remaining = re.sub(r"%(?:basename_t|goldenimage_dir|t|s)", "", arg)
            if "%" in remaining or any(c in arg for c in "\"'!^()"):
                raise BuildError(f"{name}: unsupported RUN argument: {arg}")
        steps.append((kind, tuple(args)))
    if not steps or not outputs:
        raise BuildError(f"{name}: no supported compile step")
    if not any(step[0] not in ("split", "compile") for step in steps):
        raise BuildError(f"{name}: no supported runtime step")
    return Test(name, tuple(steps), tuple(target_requirements))


def _tests(request, plan):
    source = plan.full.build / "install/share/hlsl-test-suite/test"
    if not source.is_dir():
        source = plan.full.offload.path / "test"
    selected = _select(request, source, plan.full.offload.path / "test")
    features = {"Clang" if plan.suites[0].startswith("clang-") else "DXC",
                "Vulkan" if plan.suites[0].endswith("vk") else
                "Metal" if plan.suites[0].endswith("mtl") else "DirectX"}
    parsed = []
    unsupported = {}
    for name in selected:
        try:
            parsed.append(_parse(source / name, name, features))
        except BuildError as error:
            if "requires one unique -Fo output" in str(error):
                raise  # Duplicate outputs cannot be replayed by precompiled-cc.py.
            unsupported[name] = str(error)
    return selected, tuple(parsed), unsupported


def _plan(request):
    suite = request.suite or ("clang-d3d12" if package._is_windows(
        package.ws.select_platform(request.platform)) else "clang-vk")
    pre = package._precompiled_plan(replace(request, suites=(suite,)))
    selected, parsed, unsupported = _tests(request, pre)
    archive = package._archive_path(request, pre.full.build, pre.full.platform,
                                    "hlsl-repro")
    return pre, selected, parsed, unsupported, archive


def _warning(unsupported):
    if not unsupported:
        return ""
    return ("warning: Python-free replay unsupported; using lit/Python for ALL "
            "selected tests (no tests omitted):\n" +
            "".join(f"  {name}: {reason}\n" for name, reason in unsupported.items()) +
            "target requires Python 3; PyYAML and lit are bundled. "
            "Host compilation must still succeed for every selected test.\n")


def preview(request):
    plan, selected, parsed, unsupported, archive = _plan(request)
    runtime = ("lit/Python 3; no target compiler or DXC" if unsupported else
               "POSIX sh / Windows cmd; no Python or compiler")
    preconditions = ("" if unsupported else "".join(
        f"target precondition {test.path}: {requirement}\n"
        for test in parsed for requirement in test.target_requirements
    ))
    return Plan(plan.text + f"selected tests {', '.join(selected)}\n"
                f"repro archive {archive}\n" + _warning(unsupported) +
                preconditions + f"runtime: {runtime}\n")


def _trim_tests(source, selected):
    keep = set(selected)
    for file in source.rglob("*"):
        if file.is_file() and (file.suffix == ".test" or
                              file.suffix == ".yaml" and precompiled._lines(file)):
            if file.relative_to(source).as_posix() not in keep:
                file.unlink()


def _omit_xfails(source, selected):
    """Reproducers expose expected failures rather than reporting XFAIL."""
    for name in selected:
        path = source / name
        original = path.read_text(encoding="utf-8", errors="replace")
        lines = []
        for line in original.splitlines(keepends=True):
            match = DIRECTIVE.match(line)
            if match and match.group(1) == "XFAIL":
                continue
            lines.append(line)
        updated = "".join(lines)
        if updated != original:
            path.write_text(updated, encoding="utf-8")


def _shell_arg(arg):
    """Quote literal fragments while leaving the four documented paths live."""
    pieces = re.split(r"(%goldenimage_dir|%basename_t|%t|%s)", arg)
    variables = {"%s": '"$TEST"', "%t": '"$T"',
                 "%goldenimage_dir": '"$GOLDEN"', "%basename_t": '"$BASE"'}
    return "".join(variables.get(piece, shlex.quote(piece)) for piece in pieces if piece) or "''"


def _cmd_arg(arg):
    pieces = re.split(r"(%goldenimage_dir|%basename_t|%t|%s)", arg)
    variables = {"%s": "%TEST%", "%t": "%T%", "%goldenimage_dir": "%GOLDEN%",
                 "%basename_t": "%BASE%"}
    return '"' + "".join(variables.get(piece, piece) for piece in pieces) + '"'


def _script(stage, suite, tests, reports, compiler, flags, split,
            windows, archive_platform):
    source = stage / "share/hlsl-test-suite/test"
    bin_dir = stage / "bin"
    site = stage / "test" / suite / "lit.site.cfg.py"
    config = site.read_text()
    # lit cfg adds these to every invocation of %offloader. Refuse settings
    # we cannot reproduce (such as an environment-selected GPU adapter).
    if os.getenv("OFFLOADTEST_GPU_NAME"):
        raise BuildError("OFFLOADTEST_GPU_NAME changes %offloader; repro cannot freeze it")
    extra = []
    for setting, flag in (("offloadtest_test_warp", "-warp"),
                          ("offloadtest_enable_debug", "-debug-layer"),
                          ("offloadtest_enable_validation", "-validation-layer")):
        match = re.search(r"^config\." + setting + r"\s*=\s*(\S+)",
                          config, re.MULTILINE)
        if match and match.group(1) not in ("0", "1", "True", "False", "ON", "OFF"):
            raise BuildError(f"unsupported configured {setting}: {match.group(1)}")
        if match and match.group(1) in ("1", "True", "ON"):
            extra.append(flag)
    sh = ['#!/bin/sh', '# Generated from selected RUN lines. No Python on the target.',
          'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
          'BIN="$ROOT/bin"', 'GOLDEN="$ROOT/share/hlsl-test-suite/golden-images"',
          'failed=0']
    cmd = ['@echo off', 'setlocal DisableDelayedExpansion',
           'set "ROOT=%~dp0"', 'set "BIN=%ROOT%bin"',
           'set "GOLDEN=%ROOT%share\\hlsl-test-suite\\golden-images"',
           'set "failed=0"']
    subroutines = []
    for index, test in enumerate(tests):
        rel = test.path
        scratch = (stage / "test" / suite / Path(rel).parent / "Output" /
                   (Path(rel).name + ".tmp"))
        scratch_rel = scratch.relative_to(stage).as_posix()
        data = stage / "data" / suite / Path(scratch_rel).relative_to(
            "test/" + suite)
        if not scratch.is_dir():
            raise BuildError(f"{rel}: split-file produced no test data at {scratch}")
        shutil.copytree(scratch, data)
        data_rel = data.relative_to(stage).as_posix()
        compiled = iter(reports["compiled"].get(rel, []))
        sh.extend([f'run_{index}() {{',
                   f'  TEST="$ROOT/share/hlsl-test-suite/test/{rel}"',
                   f'  T="$ROOT/{scratch_rel}"', '  BASE=${T##*/}',
                   '  rm -rf -- "$T" || return 1',
                   '  mkdir -p -- "$T" || return 1',
                   f'  cp -R "$ROOT/{data_rel}/." "$T/" || return 1'])
        cmd.extend([f'call :test_{index}', 'if errorlevel 1 (',
                    f'  echo FAIL: {rel}', '  set "failed=1"', ') else (',
                    f'  echo PASS: {rel}', ')'])
        win_test = rel.replace("/", "\\")
        win_scratch = scratch_rel.replace("/", "\\")
        win_data = data_rel.replace("/", "\\")
        body = [f':test_{index}',
                f'set "TEST=%ROOT%share\\hlsl-test-suite\\test\\{win_test}"',
                f'set "T=%ROOT%{win_scratch}"',
                'for %%F in ("%T%") do set "BASE=%%~nxF"',
                'if exist "%T%" (rmdir /s /q "%T%" || exit /b 1)',
                'mkdir "%T%" || exit /b 1',
                f'xcopy "%ROOT%{win_data}\\*" "%T%\\" '
                '/e /i /y >nul || exit /b 1']
        for kind, argv in test.steps:
            if kind == "split":
                # split-file was run on the host; %t's extracted data is archived.
                continue
            if kind == "compile":
                output = next(compiled, None)
                if output is None:
                    raise BuildError(f"{rel}: missing precompiled verdict")
                record = reports["objects"][output]
                status = record["status"]
                log = f"test/{suite}/{output}.log"
                (stage / log).write_text(record["output"])
                if status:
                    sh.append(f'  cat "$ROOT/{log}" >&2')
                    sh.append(f'  echo "compiler exited {status}" >&2')
                    sh.append('  return 1')
                    body += [f'type "%ROOT%{log.replace("/", chr(92))}" 1>&2',
                             f'echo compiler exited {status} 1>&2', 'exit /b 1']
                else:
                    sh.append(f'  [ -f "$ROOT/test/{suite}/{output}" ] || return 1')
                    win_output = output.replace("/", "\\")
                    body += [f'if not exist "%ROOT%test\\{suite}\\'
                             f'{win_output}" exit /b 1']
                continue
            executable = kind + (".exe" if windows else "")
            if not (bin_dir / executable).is_file():
                raise BuildError(f"{rel}: runtime tool missing: bin/{executable}")
            arguments = (*extra, *argv) if kind == "offloader" else argv
            sh.append('  "$BIN/' + kind + '" ' + ' '.join(map(_shell_arg, arguments)) +
                      ' || return 1')
            body.append('"%BIN%\\' + executable + '" ' +
                        ' '.join(map(_cmd_arg, arguments)) + ' || exit /b 1')
        if next(compiled, None) is not None:
            raise BuildError(f"{rel}: unused precompiled verdict")
        sh += ['}', f'if run_{index}; then',
               f'  echo {shlex.quote("PASS: " + rel)}', 'else',
               f'  echo {shlex.quote("FAIL: " + rel)}', '  failed=1', 'fi']
        subroutines.extend(body + ['exit /b 0'])
    sh += ['exit "$failed"']
    # Subroutines must follow the main exit so execution cannot fall through.
    command_lines = cmd + ['exit /b %failed%'] + subroutines
    (stage / "run.sh").write_text("\n".join(sh) + "\n")
    (stage / "run.sh").chmod(0o755)
    (stage / "run.cmd").write_bytes(("\r\n".join(command_lines) + "\r\n").encode())
    (stage / "requirements.txt").write_text(
        "# No target Python packages required. Linux needs POSIX sh and coreutils;\n"
        "# Windows needs cmd and xcopy. The runtime tools are archived.\n"
    )
    (stage / "REPRO.md").write_text(
        "# Named offload test reproducer\n\n"
        f"Suite: `{suite}`. Target: `{archive_platform}`.\n\n"
        "Run `./run.sh` on Linux or `run.cmd` on Windows. Only a graphics "
        "driver and the target's system shell are required; no Python, "
        "compiler, DXC, lit, CMake or checkout.\n\n"
        "The archive contains selected test sources, fresh split-file test data, "
        "precompiled objects and compile exit verdicts. See provenance.json "
        "for checkout revisions and commands.json for exact RUN lines and "
        "compiler verdicts. Unsupported direct replay semantics use lit instead; "
        "no selected test is silently omitted. XFAIL directives are omitted "
        "so a known bug reports FAIL, not XFAIL.\n\n"
        "Target feature requirements are assumed, not checked by the runner. "
        "Run only on a machine satisfying REQUIRES and not matching UNSUPPORTED:\n"
        + ("".join(f"- `{test.path}`: {requirement}\n"
                   for test in tests for requirement in test.target_requirements)
           or "- None declared.\n")
        + (f"\n{package._target_warning(archive_platform)}.\n"
           if archive_platform != "native" else "")
        + "\nSelected tests:\n" + "\n".join(f"- `{t.path}`" for t in tests) + "\n"
    )
    commands = {}
    for test in tests:
        path = source / test.path
        scratch = (stage / "test" / suite / Path(test.path).parent / "Output" /
                   (Path(test.path).name + ".tmp"))
        host_commands = []
        for kind, argv in test.steps:
            if kind not in ("split", "compile"):
                continue
            expanded = precompiled._expand(
                " ".join(shlex.quote(arg) for arg in argv), path, scratch, set())
            host_commands.append(
                [str(split), *shlex.split(expanded)] if kind == "split" else
                [str(compiler), *flags, *shlex.split(expanded)])
        commands[test.path] = {
            "RUN": precompiled._lines(path),
            "host_commands": host_commands,
            "objects": {key: reports["objects"][key]
                        for key in reports["compiled"][test.path]},
        }
    (stage / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")


def _lit_script(stage, suite, tests, unsupported, platform):
    """Replay every selected test with the archived lit and compile verdicts."""
    root = stage / "test" / suite
    report = json.loads((root / "precompiled.json").read_text())
    for name in tests:
        for output in report["compiled"][name]:
            if output not in report["objects"]:
                raise BuildError(f"{name}: missing precompiled verdict for {output}")

    windows = package._is_windows(platform)
    splitter = stage / "bin" / ("split-file.exe" if windows else "split-file")
    if not splitter.is_file():
        raise BuildError(f"lit replay requires target split-file: {splitter}")
    paths = [f'test/{suite}/{name}' for name in tests]
    shell = ['#!/bin/sh', 'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)',
             'cd "$ROOT" || exit 1',
             'exec "$ROOT/bin/lit" -v ' + ' '.join(shlex.quote(path) for path in paths)]
    (stage / "run.sh").write_text("\n".join(shell) + "\n")
    (stage / "run.sh").chmod(0o755)
    command = ['@echo off', 'setlocal DisableDelayedExpansion', 'set "ROOT=%~dp0"',
               'cd /d "%ROOT%" || exit /b 1',
               'call "%ROOT%bin\\lit.cmd" -v ' +
               ' '.join('"' + path.replace('/', '\\') + '"' for path in paths),
               'exit /b %errorlevel%']
    (stage / "run.cmd").write_bytes(("\r\n".join(command) + "\r\n").encode())
    source = stage / "share/hlsl-test-suite/test"
    commands = {}
    for name in tests:
        commands[name] = {
            "RUN": precompiled._lines(source / name),
            "objects": {output: report["objects"][output]
                        for output in report["compiled"][name]},
        }
    (stage / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
    (stage / "requirements.txt").write_text(
        "# Python 3 is required. lit and pure-Python PyYAML are bundled.\n"
        "# Target runtime tools (including split-file for RUN lines) must be archived.\n"
        "# Optional: install psutil to enable per-test timeouts.\n"
    )
    (stage / "REPRO.md").write_text(
        "# Named offload test reproducer (lit fallback)\n\n"
        f"Suite: `{suite}`. Target: `{platform}`.\n\n"
        "Run `./run.sh` on Linux or `run.cmd` on Windows. Python 3 and a "
        "graphics driver are required; lit and PyYAML are bundled. psutil "
        "is optional (per-test timeouts). No compiler or DXC is required. "
        "See requirements.txt for dependencies.\n\n"
        "All selected tests run through lit, including REQUIRES and shell "
        "semantics. XFAIL directives are omitted so a known bug reports FAIL. "
        "The host compiled every selected test; "
        "precompiled-cc.py replays each compiler exit status, including "
        "expected compile failures. See commands.json and provenance.json "
        "for recorded verdicts and revisions.\n\n"
        "Python-free replay was not available for:\n" +
        "".join(f"- `{name}`: {reason}\n" for name, reason in unsupported.items()) +
        (f"\n{package._target_warning(platform)}.\n" if platform != "native" else "")
    )


def execute(request):
    initial, tests, parsed, unsupported, archive = _plan(request)
    package._check_migration(initial.full.root)
    if initial.full.needs_install:
        from . import native
        full = initial.full
        native.execute(Request(
            "build", full.root, worktree=str(full.tree.path),
            offload=str(full.offload.path) if full.tree.kind == "llvm" else None,
            llvm=str(full.llvm.path) if full.tree.kind == "offload" else None,
            platform=full.platform, targets=full.install_targets,
            jobs=request.jobs, no_auto=request.no_auto, dxc=request.dxc,
            dist_prefix=request.dist_prefix,
        ))
    full = initial.full
    prefixes = sorted({str(path) for path in (
        initial.native_bin.parent,
        full.distribution or full.build / "install",
    )})
    with ExitStack() as held:
        for path in prefixes:
            held.enter_context(build_lock(full.root, Path(path)))
        held.enter_context(build_lock(full.root, full.build))
        current, selected, current_parsed, current_unsupported, destination = _plan(request)
        if (current.full.needs_install or current.full.tree != full.tree or
                current.native_bin != initial.native_bin or
                current.dxc_bin != initial.dxc_bin or
                selected != tests or current_parsed != parsed or
                current_unsupported != unsupported or destination != archive):
            raise BuildError("repro inputs changed while waiting for lock")
        with tempfile.TemporaryDirectory(prefix="hlsl-repro-") as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            # Reuse the precompiled package staging and its host compilation,
            # but only after pruning every unselected executable test.
            package._stage(full, stage, set(current.suites))
            source = stage / "share/hlsl-test-suite/test"
            _trim_tests(source, tests)
            _omit_xfails(source, tests)
            if not unsupported:
                for path in (stage / "share/hlsl-test-suite/lit",
                             stage / "share/hlsl-test-suite/python"):
                    package._remove_staged_path(path)
                for name in ("lit", "lit.cmd"):
                    package._remove_staged_path(stage / "bin" / name)
            for path in (stage / "lib/clang", stage / "include", stage / "dxc"):
                package._remove_staged_path(path)
            for tool in ("clang", "clang++", "clang-cl", "clang-cpp", "clang-dxc",
                         "clang-tidy", "run-clang-tidy", "dxc", "dxv"):
                for suffix in ("", ".exe"):
                    package._remove_staged_path(stage / "bin" / (tool + suffix))
            compiler, flags, _ = precompiled.suite_tools(
                current.suites[0], current.native_bin, current.dxc_bin)
            split = current.native_bin / "split-file"
            if not split.is_file():
                raise BuildError(f"native split-file missing: {split}")
            report = precompiled.compile_suite(
                source, stage / "test" / current.suites[0], compiler, split,
                flags, {"Clang" if current.suites[0].startswith("clang-") else "DXC",
                        "Vulkan" if current.suites[0].endswith("vk") else
                        "Metal" if current.suites[0].endswith("mtl") else "DirectX"})
            for name in tests:
                if name not in report["compiled"]:
                    raise BuildError(f"{name}: cannot precompile: "
                                     f"{report['skipped'].get(name, 'missing verdict')}; "
                                     "no selected test may be omitted")
                outputs = report["compiled"][name]
                if len(outputs) != len(set(outputs)) or any(
                        output not in report["objects"] for output in outputs):
                    raise BuildError(f"{name}: duplicate or missing compile verdict")
            if unsupported:
                package._precompiled_site(stage, current.suites[0])
                shutil.copy2(Path(__file__).with_name("precompiled_cc.py"),
                             stage / "bin/precompiled-cc.py")
                _lit_script(stage, current.suites[0], tests, unsupported, full.platform)
            else:
                _script(stage, current.suites[0], parsed, report, compiler, flags, split,
                        package._is_windows(full.platform), full.platform)
                package._remove_staged_path(stage / "test" / current.suites[0] /
                                            "lit.site.cfg.py")
                package._remove_staged_path(source / "lit.cfg.py")
            (stage / "README.txt").write_text(
                "Named offload test reproducer. See REPRO.md for target instructions.\n"
                + ("Python 3 required; no compiler or DXC required.\n" if unsupported
                   else "No Python, compiler or DXC required.\n")
                + (f"{package._target_warning(full.platform)}.\n"
                   if full.platform != "native" else ""))
            provenance = json.loads((stage / "provenance.json").read_text())
            provenance.update({"contents": "named precompiled test reproducer",
                               "selected_tests": list(tests),
                               "runner": "lit/Python" if unsupported else "Python-free sh/cmd",
                               "fallback_reasons": unsupported,
                               "target_requirements": (
                                   {test.path: list(test.target_requirements)
                                    for test in parsed if test.target_requirements}
                                   if not unsupported else {}),
                               "host_compiler": str(compiler),
                               "host_compiler_flags": flags,
                               "host_split_file": str(split),
                               "target_python": ("Python 3; bundled lit and PyYAML; "
                                                 "optional psutil for timeouts"
                                                 if unsupported else "not required"),
                               "dxc": "not needed on target; used on host only"})
            (stage / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
            if package._is_windows(full.platform):
                package._materialize_links(stage)
            else:
                package._reject_escaping_links(stage)
            package._archive(stage, destination)
    return Plan(_warning(unsupported) +
                f"packaged {len(tests)} named tests in {destination}\n")
