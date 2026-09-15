#!/usr/bin/env python3
"""Compile every shader in an offload test suite ahead of time.

`hlsl-prerun` ships a test suite to a machine that has no compiler: DXIL and
SPIR-V are the same bytes everywhere, and so is dxv's signature, so the
compiling half of each test can happen here and the target machine only has to
run the offloader and the checks around it.

What a test looks like (and they are strikingly uniform -- 653 of them, and not
one pipes the compiler's output anywhere):

    # RUN: split-file %s %t
    # RUN: %dxc_target -T cs_6_5 -Fo %t.o %t/source.hlsl
    # RUN: %offloader %t/pipeline.yaml %t.o

This runs the first two lines, here, with this machine's compiler, into the
package's own test execution root -- exactly the paths lit will use on the
target, because the packaged lit configuration puts its object root inside the
package. On the target, `split-file` runs again (it is a 200 KB tool, and it is
what turns the .test file into pipeline.yaml), `%dxc_target` expands to a no-op
because the object is already there, and `%offloader` does the real work.

A test whose RUN lines this cannot account for is *not* compiled and *not*
guessed at: it is reported, and the caller decides. The cases that matter:

  * a substitution this does not expand (the test would compile something
    different from what lit would);
  * a -Fo inside %t/, which split-file would delete on the target before the
    offloader ever saw it.
"""

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import sys

RUN_LINE = re.compile(r"^[#/]+ *RUN: *(.*)$")

# lit's conditional substitution: %if FEATURE %{...%} [%else %{...%}]. Only the
# features that are decided by *which compiler this suite uses* can be answered
# here -- Clang, DXC and the graphics API. A test that asks about anything else
# (device capabilities, which are the target machine's business) is left alone.
IF_CLAUSE = re.compile(
    r"%if\s+(?P<feature>[A-Za-z0-9_-]+)\s*%\{(?P<then>[^%]*)%\}"
    r"(?:\s*%else\s*%\{(?P<otherwise>[^%]*)%\})?"
)

# The substitutions a compile step may contain. Anything else in a line this
# script has to run is a reason to leave the test alone.
KNOWN = ("%s", "%t", "%dxc_target_lib", "%dxc_target", "%basename_t")


class Skipped(Exception):
    pass


def run_lines(path):
    """The RUN lines of a test, with line continuations joined."""
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            match = RUN_LINE.match(raw.rstrip("\n"))
            if not match:
                continue
            text = match.group(1).strip()
            if lines and lines[-1].endswith("\\"):
                lines[-1] = lines[-1][:-1].rstrip() + " " + text
            else:
                lines.append(text)
    return lines


def resolve_conditionals(line, features, unknown_features):
    """Answer %if clauses whose feature this machine can decide."""

    def answer(match):
        feature = match.group("feature")
        if feature not in features and feature not in unknown_features:
            raise Skipped("%if asks about '{}', which only the test machine knows".format(feature))
        return (match.group("then") if feature in features else match.group("otherwise")) or ""

    return IF_CLAUSE.sub(answer, line)


def expand(line, test_path, tmp):
    """Apply the substitutions a compile step is allowed to use."""
    out = line.replace("%basename_t", os.path.basename(tmp))
    out = out.replace("%s", test_path).replace("%t", tmp)
    leftover = re.findall(r"%[A-Za-z_][A-Za-z0-9_]*", out)
    if leftover:
        raise Skipped("substitution this does not expand: " + " ".join(sorted(set(leftover))))
    return out


def compile_one(test_path, rel, out_root, tools):
    """Run the split-file and compile steps of one test. Returns a report."""
    tmp = os.path.join(out_root, os.path.dirname(rel), "Output", os.path.basename(rel) + ".tmp")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)

    produced = []
    records = {}
    for line in run_lines(test_path):
        compiles_here = 0
        line = resolve_conditionals(line, tools["features"], tools["unknown_features"])
        words = shlex.split(line, posix=True)
        if not words:
            continue
        head = words[0]

        if head in ("split-file", "%split-file"):
            argv = [tools["split_file"]] + [
                expand(word, test_path, tmp) for word in words[1:]
            ]
        elif head in ("%dxc_target", "%dxc_target_lib"):
            argv = [tools["compiler"]] + tools["compiler_args"] + [
                expand(word, test_path, tmp) for word in words[1:]
            ]
            # -Fo inside %t/ would be deleted by the split-file that runs on
            # the target, so precompiling it is worse than not precompiling
            # the test at all: the failure would appear there, as a missing
            # file, with nothing to point at this.
            for index, word in enumerate(argv):
                if word == "-Fo" and index + 1 < len(argv):
                    target = argv[index + 1]
                    if os.path.abspath(target).startswith(os.path.abspath(tmp) + os.sep):
                        raise Skipped("-Fo writes inside %t, which split-file recreates")
                    produced.append(target)
                    compiles_here += 1
        else:
            # An offloader/imgdiff/FileCheck line: the target machine's job.
            continue

        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True
        )
        if head in ("split-file", "%split-file"):
            # Nothing to replay: split-file runs again on the target. Its
            # failing here means the test file itself is unreadable.
            if result.returncode != 0:
                raise Skipped("split-file failed: " + " ".join(result.stdout.split())[:200])
            continue

        # The compiler's verdict travels with the object, because a compile
        # that fails is how a good many of these tests are *expected* to end
        # (XFAIL), and because clang writes its output file before the
        # validator rejects it -- so the object being there proves nothing.
        for target in produced[-compiles_here:] if compiles_here else []:
            records[target] = {
                "status": result.returncode,
                "output": result.stdout[-4000:],
            }

    if not produced:
        raise Skipped("no compile step (nothing to precompile)")
    return produced, records


# What lit calls a test here (offload-test-suite's lit.cfg.py: config.suffixes).
# The .yaml ones are tests too -- Bugs/UAV-Sequental-Consistency.yaml is a
# shader like any other -- and missing them means the target finds no object.
def collect(test_root, suffixes=(".test", ".yaml")):
    for base, dirs, files in os.walk(test_root):
        dirs[:] = [d for d in dirs if d not in ("Inputs", "Output", "__pycache__")]
        for name in sorted(files):
            if name.endswith(suffixes):
                yield os.path.relpath(os.path.join(base, name), test_root)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tests", required=True, help="the installed test tree (share/hlsl-test-suite/test)")
    p.add_argument("--out", required=True, help="the suite's execution root in the package (test/<suite>)")
    p.add_argument("--compiler", required=True, help="clang-dxc or dxc, for *this* machine")
    p.add_argument("--split-file", required=True, dest="split_file")
    p.add_argument(
        "--compiler-arg",
        dest="compiler_args",
        action="append",
        default=[],
        help="an argument every compile gets, as lit.cfg.py would add it (repeatable)",
    )
    p.add_argument(
        "--feature",
        dest="features",
        action="append",
        default=[],
        help="a lit feature that is true for this suite whatever the device (Clang, DXC, Vulkan)",
    )
    p.add_argument(
        "--not-feature",
        dest="unknown_features",
        action="append",
        default=[],
        help="a lit feature that is false for this suite whatever the device",
    )
    p.add_argument(
        "--only",
        dest="only",
        action="append",
        default=[],
        metavar="REL",
        help="compile just this test, named as lit names it (repeatable)",
    )
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    p.add_argument("--manifest", help="write a JSON report here")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    tools = {
        "compiler": args.compiler,
        "compiler_args": args.compiler_args,
        "split_file": args.split_file,
        "features": set(args.features),
        "unknown_features": set(args.unknown_features),
    }

    if args.only:
        tests = []
        for rel in args.only:
            if not os.path.isfile(os.path.join(args.tests, rel)):
                raise SystemExit("precompile-shaders.py: no such test: " + rel)
            tests.append(rel)
    else:
        tests = list(collect(args.tests))
    compiled, skipped = {}, {}

    def work(rel):
        try:
            produced, records = compile_one(os.path.join(args.tests, rel), rel, args.out, tools)
            return rel, produced, records, None
        except Skipped as exc:
            return rel, None, None, str(exc)

    objects = {}
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for rel, produced, records, reason in pool.map(work, tests):
            if reason is not None:
                skipped[rel] = reason
                continue
            relative = []
            for path in produced:
                key = os.path.relpath(path, args.out).replace(os.sep, "/")
                relative.append(key)
                objects[key] = records.get(path, {"status": 0, "output": ""})
            compiled[rel] = relative
            if any(objects[key]["status"] for key in relative):
                failed += 1
                # The object a failed compile left behind must not travel: on
                # the target the replay reports the failure, and a stale object
                # beside it would be read by nothing but confusing to find.
                for path in produced:
                    if os.path.isfile(path) and objects[
                        os.path.relpath(path, args.out).replace(os.sep, "/")
                    ]["status"]:
                        os.remove(path)

    if args.manifest:
        with open(args.manifest, "w", encoding="utf-8") as handle:
            json.dump(
                {"compiled": compiled, "skipped": skipped, "objects": objects},
                handle,
                indent=1,
                sort_keys=True,
            )

    if not args.quiet:
        print(
            "precompiled {} of {} tests ({} of them the compiler rejects, "
            "which is replayed on the target)".format(len(compiled), len(tests), failed)
        )
        reasons = {}
        for rel, reason in skipped.items():
            reasons.setdefault(reason.split(":")[0], []).append(rel)
        for reason, names in sorted(reasons.items()):
            print("  not precompiled -- {} ({}): {}".format(reason, len(names), ", ".join(sorted(names)[:3]) + (" ..." if len(names) > 3 else "")))
    return 0 if compiled else 1


if __name__ == "__main__":
    sys.exit(main())
