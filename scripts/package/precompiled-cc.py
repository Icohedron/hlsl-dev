#!/usr/bin/env python3
"""Stand in for the compiler in a package whose shaders are already compiled.

`hlsl-precompile` runs each test's compile step before the package is made, and
this replays the result on the test machine: it prints what the compiler said
and exits with the status the compiler exited with.

Faking success instead (`echo` in place of the compiler) looks like it works
and quietly changes what the suite means. A lot of these tests *expect* the
compile to fail -- `XFAIL: Clang && Vulkan` on a test whose SPIR-V does not
validate, for one -- and clang writes its output file before the validator
rejects it, so "the object is there" is not the same as "the compile passed".
Replaying the status keeps an expected failure expected, and keeps a *new*
failure legible: the message is the one the compiler produced.

Invoked as the `%dxc_target` substitution, so it receives the compile line's
own arguments and finds the test by the `-Fo` path in them.
"""

import json
import os
import sys


def main(argv):
    out = None
    for index, arg in enumerate(argv):
        if arg == "-Fo" and index + 1 < len(argv):
            out = argv[index + 1]
        elif arg.startswith("-Fo") and len(arg) > 3:
            out = arg[3:]
    if out is None:
        sys.stderr.write("precompiled-cc: no -Fo in this compile line; nothing to replay\n")
        return 2

    # The manifest sits at the root of the suite's execution tree, which is two
    # directories above the Output/ the object is written to.
    out = os.path.abspath(out)
    root = os.path.dirname(out)
    manifest = None
    for _ in range(12):
        candidate = os.path.join(root, "precompiled.json")
        if os.path.isfile(candidate):
            manifest = candidate
            break
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    if manifest is None:
        sys.stderr.write("precompiled-cc: no precompiled.json above {}\n".format(out))
        return 2

    with open(manifest, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    key = os.path.relpath(out, os.path.dirname(manifest)).replace(os.sep, "/")
    record = data.get("objects", {}).get(key)
    if record is None:
        sys.stderr.write(
            "precompiled-cc: {} was not compiled when this package was made.\n"
            "See PRECOMPILED.md for which tests were left out and why.\n".format(key)
        )
        return 2

    output = record.get("output", "")
    if output:
        sys.stderr.write(output if output.endswith("\n") else output + "\n")
    return int(record.get("status", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
