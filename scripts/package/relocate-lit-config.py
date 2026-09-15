#!/usr/bin/env python3
"""Rewrite a build tree's lit.site.cfg.py into a relocatable one.

`hlsl-precompile` ships a prefix that already knows where everything is, so the
machine that runs the tests types one lit command and nothing else -- no
configure script, no --dxc-path, no environment. That is what this does: it
takes the lit.site.cfg.py CMake generated for one suite, and rewrites every
path in it as a path *relative to the config file itself*, pointing into the
package.

Why rewrite CMake's file rather than substitute the template (which is what
offload-test-suite's configure-test-suite.py does on the target machine):
CMake's file is the configuration this build actually has -- SPIR-V support as
probed, the WARP architecture, the debug/validation layers, the OS name -- so
nothing has to be re-derived, and a suite that was not configured here has no
file and is simply not in the package.

The rewrite is mechanical:

  * every path(r"...") is resolved against the source config's directory and
    then re-expressed relative to the packaged config's directory, through the
    --map prefixes (build bin dir -> bin/, dxc build -> dxc/bin, ...);
  * the two values the template does not wrap in path() -- the dxc directory
    and the golden image directory -- are wrapped, so they relocate too;
  * anything still absolute afterwards is an error, not a silent path to a
    directory that exists only on the build machine.

path() itself is LLVM's: configure_lit_site_cfg emits a header that resolves a
relative path against os.path.dirname(__file__), which is exactly the
behaviour a relocatable config needs, so the header is kept as it is.
"""

import argparse
import os
import posixpath
import re
import sys

# The template assigns these two as raw strings rather than through path();
# lit.cfg.py uses both as directories, so they have to relocate as well.
UNWRAPPED = ("offloadtest_dxc_dir", "goldenimage_dir")

# Dropped into the packaged config above everything else. Two things the
# machine running the tests would otherwise have to install:
#
#   yaml    lit.cfg.py imports it to read api-query's device description.
#   psutil  not bundled -- it is a C extension, so it cannot be copied here
#           for another machine. lit enforces the per-test timeout lit.cfg.py
#           asks for by killing the process and its children through psutil,
#           and refuses the timeout outright when it cannot import it. So
#           without it this configuration runs without a timeout and says so,
#           and `pip install psutil` on the test machine turns it on.
#
# yaml is appended to sys.path rather than prepended: a PyYAML installed on the
# test machine is the one that gets imported.
# Installed before lit.llvm.initialize() runs, which is what emits two of
# these. Each is a true statement about a package that carries no compiler,
# and none of them is something the reader can act on.
QUIET = '''
_expected_notes = (
    # lit.cfg.py asks lit to find clang-dxc; there is none here, and the
    # substitution this file installs at the end is used instead.
    "Did not find dxc_target",
    # Windows only: lit locates GnuWin tools (cmp, grep, sed, diff, echo) for
    # RUN lines that use them. No test in this suite does.
    "using lit tools:",
)
_real_note = lit_config.note


def _note(message):
    if not any(expected in str(message) for expected in _expected_notes):
        _real_note(message)


lit_config.note = _note

'''

PROLOGUE = '''\
# Rewritten by hlsl-package: every path below is relative to this file, so the
# package runs from wherever it was unpacked. Regenerate with `hlsl-package`.
import os as _os
import sys as _sys

_pkg = _os.path.abspath(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "{root}"))
{quiet}
_bundled = _os.path.join(_pkg, "share", "hlsl-test-suite", "python")
if _os.path.isdir(_bundled) and _bundled not in _sys.path:
    _sys.path.append(_bundled)

# lit.cfg.py was written against lit 18, where lit_config.maxIndividualTestTime
# was writable; this lit makes it read-only ("Use config.maxIndividualTestTime
# instead") and raises on assignment -- which happens whenever the value is 0,
# i.e. on every `--timeout=0` run, and takes the whole run down with a
# traceback. The assignment is made a no-op here and honoured by the block at
# the end of this file, which sets the per-suite knob lit actually reads.
import lit.LitConfig as _lit_config_module

if isinstance(
    getattr(_lit_config_module.LitConfig, "maxIndividualTestTime", None), property
):
    _lit_config_module.LitConfig.maxIndividualTestTime = property(
        lambda self: self._maxIndividualTestTime,
        lambda self, value: None,
    )

try:
    import psutil  # noqa: F401
except ImportError:
    # Nothing can kill a timed-out test's process tree here, and lit refuses a
    # timeout it cannot enforce, so the run goes without one rather than not
    # happening at all.
    _HLSL_NO_TIMEOUT = True
    lit_config.note(
        "psutil not installed: running without a per-test timeout "
        "(pip install psutil to enable it)"
    )
else:
    _HLSL_NO_TIMEOUT = False
# --- end of the hlsl-package prologue; CMake's own configuration follows ----

'''


def parse_maps(values):
    """--map <abs path>=<path inside the package> -> longest prefix first."""
    maps = []
    for value in values:
        if "=" not in value:
            raise SystemExit("relocate-lit-config.py: --map wants <abs path>=<rel>, got " + value)
        src, dst = value.split("=", 1)
        src = os.path.realpath(src)
        maps.append((src, dst.strip("/")))
    # Longest first, so <build>/bin wins over <build>.
    maps.sort(key=lambda pair: len(pair[0]), reverse=True)
    return maps


class Relocator:
    def __init__(self, src_dir, maps, to_root):
        self.src_dir = src_dir
        self.maps = maps
        self.to_root = to_root
        self.unmapped = []

    def __call__(self, value):
        if not value:
            return value
        path = value if os.path.isabs(value) else os.path.join(self.src_dir, value)
        # The directories are resolved, the last component is not: `bin/dxc` in
        # DXC's build tree is a symlink to `dxc-3.7` beside it, and the package
        # carries it under the name the tests call it by.
        named = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
        for candidate in (named, os.path.realpath(path)):
            for src, dst in self.maps:
                if candidate == src:
                    rest = ""
                elif candidate.startswith(src + os.sep):
                    rest = candidate[len(src) + 1:]
                else:
                    continue
                rest = rest.replace(os.sep, "/")
                return posixpath.normpath(posixpath.join(self.to_root, dst, rest))
        self.unmapped.append(named)
        return value


# Appended after the config has loaded lit.cfg.py, so it sees what the suite
# asked for and can fill in what did not take.
EPILOGUE = '''
# --- hlsl-package: make the suite's per-test timeout stick -------------------
# lit.cfg.py asks for a {timeout}-second cap by assigning to
# lit_config.maxIndividualTestTime "if it is 0" -- which is what lit 18 passed
# when --timeout was not given. This lit passes None instead, so that branch
# never runs, and a test that hangs the GPU hangs the whole run rather than
# failing by itself.
#
# config.maxIndividualTestTime is the per-suite knob lit points at (its own
# error message: "lit_config.maxIndividualTestTime is read-only. Use
# config.maxIndividualTestTime instead"), and it is what TestRunner enforces.
# --timeout=N on the command line still wins: TestingConfig.finish() prefers
# lit_config's value over this one.
if _HLSL_NO_TIMEOUT:
    config.maxIndividualTestTime = 0
elif lit_config.maxIndividualTestTime is not None:
    # --timeout=N was given, including --timeout=0 for "no limit". Honour it:
    # TestingConfig.finish() copies it over this value anyway.
    config.maxIndividualTestTime = lit_config.maxIndividualTestTime
elif not getattr(config, "maxIndividualTestTime", 0):
    config.maxIndividualTestTime = {timeout}
    lit_config.note(
        "per-test timeout: {timeout}s (--timeout=N to change, --timeout=0 to switch off)"
    )
'''

# Inserted between CMake's values and the `import lit.llvm` that precedes
# load_config, because lit.cfg.py reads these two the moment it runs.
MIDDLE = '''
# --- hlsl-package: fall back to a DXC on PATH -------------------------------
# The package points at <package>/dxc/bin. When that directory is not there --
# packaged with --no-dxc, or not filled in yet -- use whatever dxc and dxv are
# on PATH instead of nothing at all. It matters most for the D3D12 suites:
# clang-dxc signs DXIL by running dxv, and D3D12 refuses a shader that nothing
# has signed, so "no dxv" is the difference between a suite that runs and one
# that fails on every test.
if not config.offloadtest_dxc_dir or not os.path.isdir(config.offloadtest_dxc_dir):
    import shutil as _shutil

    _dxv = _shutil.which("dxv")
    if _dxv:
        config.offloadtest_dxc_dir = os.path.dirname(os.path.abspath(_dxv))
        lit_config.note("no dxv in this package; using the one on PATH: " + _dxv)
    _dxc = _shutil.which("dxc")
    if _dxc and not os.path.isfile(config.offloadtest_dxc.strip(\'"\')):
        config.offloadtest_dxc = \'"\' + os.path.abspath(_dxc) + \'"\'
        lit_config.note("no dxc in this package; using the one on PATH: " + _dxc)

'''

# Where MIDDLE goes: the template's own import of lit.llvm, which is the last
# thing before it hands over to lit.cfg.py.
MIDDLE_ANCHOR = "\nimport lit.llvm\n"

# Appended for a package whose shaders were compiled before it was made.
PRECOMPILED = '''
# --- hlsl-precompile: the shaders in this package are already compiled -------
# Each test's object file is next to the test's Output directory, at exactly
# the path its %dxc_target line writes. The compile step is replaced by
# bin/precompiled-cc.py, which replays what the compiler said and the status
# it exited with -- not an `echo`, because a compile that *fails* is how a
# good many of these tests are expected to end, and clang writes the object
# before the validator rejects it. There is no compiler in this package; lit
# warns that it cannot find clang-dxc and then uses the substitution below,
# which is inserted at the front of the list so that it wins either way.
#
# The RUN lines are not edited: the tests are upstream's, and lit's report
# should still show the command the test asked for.
_replay = \'"\' + _sys.executable + \'" "\' + _os.path.join(_pkg, "bin", "precompiled-cc.py") + \'"\'
config.substitutions.insert(0, ("%dxc_target_lib", _replay))
config.substitutions.insert(1, ("%dxc_target", _replay))

# Both notes this file suppresses are emitted while the configuration loads,
# which is now over: put lit's own back, so that anything said later reports
# the line it came from rather than this one.
try:
    lit_config.note = _real_note
except NameError:
    pass
'''

# What lit.cfg.py asks for, repeated here because the assignment it makes no
# longer reaches the tests (see EPILOGUE).
DEFAULT_TIMEOUT = 300


def insert_middle(text):
    if MIDDLE_ANCHOR not in text:
        raise SystemExit(
            "relocate-lit-config.py: no 'import lit.llvm' in the generated config; "
            "the template changed shape and the DXC fallback has nowhere to go"
        )
    return text.replace(MIDDLE_ANCHOR, "\n" + MIDDLE + MIDDLE_ANCHOR, 1)


def relocate(text, relocator):
    text = re.sub(
        r'path\(r"([^"]*)"\)',
        lambda m: 'path(r"{}")'.format(relocator(m.group(1))),
        text,
    )
    for name in UNWRAPPED:
        text = re.sub(
            r'^(config\.{} = )r"([^"]*)"$'.format(re.escape(name)),
            lambda m: '{}path(r"{}")'.format(m.group(1), relocator(m.group(2))),
            text,
            flags=re.M,
        )
    return text


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--in", dest="src", required=True, help="lit.site.cfg.py from the build tree")
    p.add_argument("--out", dest="dst", required=True, help="where to write the packaged one")
    p.add_argument("--root", required=True, help="the package root --out lives inside")
    p.add_argument(
        "--precompiled",
        action="store_true",
        help="the shaders are already compiled: make the compile step a no-op",
    )
    p.add_argument(
        "--map",
        dest="maps",
        action="append",
        default=[],
        metavar="ABS=REL",
        help="a build-machine directory and where it is in the package (repeatable)",
    )
    args = p.parse_args()

    src = os.path.realpath(args.src)
    if not os.path.isfile(src):
        raise SystemExit("relocate-lit-config.py: no such config: " + src)
    out_dir = os.path.realpath(os.path.dirname(args.dst))
    root = os.path.realpath(args.root)

    maps = parse_maps(args.maps)
    # The suite's object root is two levels above its config (<obj>/test/<suite>),
    # in both layouts: a standalone build and tools/OffloadTest in an LLVM one.
    # It becomes the package root, which is what makes <root>/test/<suite> the
    # directory lit.cfg.py computes as the test execution root.
    obj_root = os.path.realpath(os.path.join(os.path.dirname(src), "..", ".."))
    maps.append((obj_root, ""))
    maps.sort(key=lambda pair: len(pair[0]), reverse=True)

    to_root = os.path.relpath(root, out_dir).replace(os.sep, "/")
    relocator = Relocator(os.path.dirname(src), maps, to_root)

    with open(src, "r", encoding="utf-8") as fh:
        text = fh.read()
    body = relocate(text, relocator)
    # The fallback to a DXC on PATH is for a package that expects to compile.
    # A precompiled one does not, and picking up (say) the Vulkan SDK's dxc
    # and saying so is a line of output that leads nowhere.
    if not args.precompiled:
        body = insert_middle(body)
    text = (
        PROLOGUE.format(root=to_root, quiet=QUIET if args.precompiled else "")
        + body
        + EPILOGUE.format(timeout=DEFAULT_TIMEOUT)
        + (PRECOMPILED if args.precompiled else "")
    )

    if relocator.unmapped:
        raise SystemExit(
            "relocate-lit-config.py: {} has paths that are not in the package:\n  {}".format(
                src, "\n  ".join(sorted(set(relocator.unmapped)))
            )
        )
    left = re.findall(r'r"((?:/|[A-Za-z]:[\\/])[^"]*)"', text)
    if left:
        raise SystemExit(
            "relocate-lit-config.py: {} still has absolute paths:\n  {}".format(
                src, "\n  ".join(sorted(set(left)))
            )
        )

    os.makedirs(out_dir, exist_ok=True)
    with open(args.dst, "w", encoding="utf-8") as fh:
        fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
