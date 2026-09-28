"""Plan and explicitly launch Compiler Explorer with local HLSL compilers."""

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from . import workspace as ws
from .build_support import _check_migration, saved_selections
from .command import Plan


@dataclass(frozen=True)
class ExplorerPlan:
    """Resolved compiler paths and the foreground service invocation."""

    root: Path
    checkout: Path
    config: Path
    llvm: ws.Worktree
    clang: Path
    clang_dxc: Path
    dxc: Path
    command: tuple[str, ...]
    text: str


def _compiler(path, correction):
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ws.SelectionError(
            f"compiler {path} is missing or not executable; {correction}"
        )
    return path.resolve()


def _dxc_path(root, context, llvm, spec):
    prebuilt = os.getenv("HLSL_DXC_PREBUILT_DIR")
    if spec in ("nix", "prebuilt", "system"):
        if not prebuilt:
            raise ws.SelectionError(
                "prebuilt dxc is unavailable; enter the devenv shell or pass --dxc"
            )
        return _compiler(Path(prebuilt) / "dxc", "enter the devenv shell")
    if spec:
        for directory in (Path(spec), root / spec):
            if directory.is_dir() and (directory / "dxc").is_file():
                return _compiler(directory / "dxc", "pass --dxc to a working directory")
        source = ws.resolve(root, spec, ("dxc",))
    else:
        source = ws.dependency(root, context or llvm, "dxc")
    if source:
        path = ws.build_directory(source) / "bin/dxc"
        if path.is_file():
            return _compiler(path, f"run 'hlsl build dxc --in {source.path}'")
        if spec:
            return _compiler(path, f"run 'hlsl build dxc --in {source.path}'")
    if not spec and prebuilt:
        return _compiler(Path(prebuilt) / "dxc", "enter the devenv shell")
    if source:
        return _compiler(path, f"run 'hlsl build dxc --in {source.path}'")
    raise ws.SelectionError(
        "no local dxc found; run 'hlsl setup' or pass --dxc to a built compiler"
    )


def _property(value):
    """Keep path/name data from adding another property to the generated file."""
    value = str(value)
    if any(character in value for character in ("\n", "\r", "\\")):
        raise ws.SelectionError(
            f"Compiler Explorer path/name contains an unsupported character: {value!r}"
        )
    return value


def plan(request):
    """Resolve only existing native compilers; never configure or build them."""
    root = request.root.resolve()
    if ws.select_platform(request.platform) != "native":
        raise ws.SelectionError("Compiler Explorer runs local compilers only; use native")
    checkout = root / "compiler-explorer"
    if not (checkout / "Makefile").is_file():
        raise ws.SelectionError(
            f"{checkout}: Compiler Explorer is not checked out; run 'hlsl setup'"
        )
    spec = request.worktree or os.getenv("HLSL_WT")
    context = (
        ws.resolve(root, spec) if spec else ws.enclosing_worktree(request.cwd or Path.cwd())
    )
    saved = saved_selections(root, context, "native") if context else {}
    llvm_spec = request.llvm or os.getenv("HLSL_LLVM") or saved.get("llvm")
    if llvm_spec:
        llvm = ws.resolve(root, llvm_spec, ("llvm",))
    elif context and context.kind == "llvm":
        llvm = context
    else:
        llvm = ws.dependency(root, context, "llvm") if context else next(
            iter(ws.worktrees(root, "llvm")), None
        )
    if llvm is None:
        raise ws.SelectionError("no LLVM checkout found; run 'hlsl setup' or pass --llvm")
    llvm_bin = ws.build_directory(llvm) / "bin"
    correction = f"run 'hlsl build clang clang-dxc --in {llvm.path}'"
    clang = _compiler(llvm_bin / "clang", correction)
    clang_dxc = _compiler(llvm_bin / "clang-dxc", correction)
    dxc_spec = request.dxc or os.getenv("HLSL_DXC") or saved.get("dxc")
    dxc = _dxc_path(root, context, llvm, dxc_spec)
    config = checkout / "etc/config/hlsl.local.properties"
    for value in (clang, clang_dxc, dxc, llvm.path.name, dxc.parent.name):
        _property(value)
    command = ("make", "dev", "EXTRA_ARGS=--language hlsl")
    lines = (
        f"Compiler Explorer {checkout}",
        "platform native",
        f"llvm {llvm.path}",
        f"clang {clang}",
        f"clang-dxc {clang_dxc}",
        f"dxc {dxc}",
        f"write config {config}",
        f"foreground in {checkout}: {' '.join(command)}",
        "cost: local service (may install its npm dependencies); no compiler build",
    )
    return ExplorerPlan(
        root, checkout, config, llvm, clang, clang_dxc, dxc, command,
        "\n".join(lines) + "\n",
    )


def _config(plan):
    llvm_name = _property(plan.llvm.path.name)
    dxc_name = _property(plan.dxc.parent.name)
    return (
        "compilers=&dxc:&clang\n\n"
        "defaultCompiler=dxc_local\n\n"
        "group.dxc.compilers=dxc_local\n"
        f"compiler.dxc_local.exe={_property(plan.dxc)}\n"
        f"compiler.dxc_local.name=DXC ({dxc_name})\n\n"
        "group.clang.compilers=clang_local:clang_dxc_local\n"
        "group.clang.compilerType=clang-dxc\n\n"
        f"compiler.clang_local.exe={_property(plan.clang)}\n"
        f"compiler.clang_local.name=Clang ({llvm_name})\n\n"
        f"compiler.clang_dxc_local.exe={_property(plan.clang_dxc)}\n"
        f"compiler.clang_dxc_local.name=Clang-DXC ({llvm_name})\n"
    )


def _write_config(path, content):
    """Replace the generated config atomically without following an old symlink."""
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".hlsl-local-", delete=False
        ) as output:
            name = output.name
            output.write(content)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def execute(request):
    """Write only the local CE config, then wait for the requested service."""
    resolved = plan(request)
    _check_migration(resolved.root)
    if not resolved.config.parent.is_dir():
        raise ws.SelectionError(
            f"{resolved.config.parent}: Compiler Explorer config directory is missing; "
            "initialize the checkout with 'hlsl setup'"
        )
    try:
        _write_config(resolved.config, _config(resolved))
        print(resolved.text, end="", flush=True)
        with subprocess.Popen(
            resolved.command, cwd=resolved.checkout, close_fds=True
        ) as service:
            try:
                status = service.wait()
            except KeyboardInterrupt:
                # Terminal Ctrl-C reaches the whole foreground group. Also forward
                # an interrupt sent only to this wrapper so the child cannot linger.
                service.send_signal(signal.SIGINT)
                try:
                    service.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    service.terminate()
                    try:
                        service.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        service.kill()
                        service.wait()
                return Plan("", 128 + signal.SIGINT)
    except OSError as error:
        raise ws.SelectionError(f"{resolved.checkout}: cannot launch Explorer: {error}") from error
    if status < 0:
        status = 128 - status
    return Plan("", status)
