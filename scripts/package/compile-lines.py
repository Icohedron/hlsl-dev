#!/usr/bin/env python3
"""Print a test's compile line the way a person could type it.

`hlsl-repro` ships the shader sources beside the objects it precompiled, so
whoever picks up the bug report can rebuild one with their own compiler and see
whether the failure moves. This turns the test's own RUN line into that
command: lit's substitutions resolved, and the paths pointing at where the
archive keeps the parts (shaders/<test>/…) rather than at a run directory that
only exists while lit is running.

    %dxc_target -T cs_6_5 -Fo %t.o %t/source.hlsl
 -> clang-dxc -spirv ... -T cs_6_5 -Fo log2.32.o shaders/Feature/HLSLLib/log2.32/source.hlsl
"""

import argparse
import os
import re
import sys

RUN_LINE = re.compile(r"^[#/]+ *RUN: *(.*)$")
IF_CLAUSE = re.compile(
    r"%if\s+(?P<feature>[A-Za-z0-9_-]+)\s*%\{(?P<then>[^%]*)%\}"
    r"(?:\s*%else\s*%\{(?P<otherwise>[^%]*)%\})?"
)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("test", help="the .test file")
    p.add_argument("--compiler", required=True, help="what to call the compiler in the printed line")
    p.add_argument("--stem", required=True, help="the test's path without its suffix")
    p.add_argument("--compiler-arg", dest="args", action="append", default=[])
    p.add_argument("--feature", dest="features", action="append", default=[])
    args = p.parse_args()

    extra = " ".join(args.args)
    shaders = "shaders/{}/".format(args.stem)
    basename = os.path.basename(args.stem)

    for raw in open(args.test, "r", encoding="utf-8", errors="replace"):
        match = RUN_LINE.match(raw.strip())
        if not match or "%dxc_target" not in match.group(1):
            continue
        line = IF_CLAUSE.sub(
            lambda m: (m.group("then") if m.group("feature") in args.features else m.group("otherwise")) or "",
            match.group(1),
        )
        line = line.replace("%dxc_target_lib", args.compiler).replace("%dxc_target", args.compiler)
        if extra:
            line = line.replace(args.compiler, args.compiler + " " + extra, 1)
        # %t is lit's per-test scratch path: %t/x is a file split-file wrote,
        # which the archive keeps under shaders/; %t on its own is the stem of
        # the outputs, which land wherever the reader runs this.
        line = line.replace("%t/", shaders).replace("%t", basename)
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
