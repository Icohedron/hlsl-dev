#!/usr/bin/env python3
"""Replay the recorded compiler exit status in a compiler-free test package."""

import json
import os
from pathlib import Path
import sys


def main(argv):
    output = None
    for index, argument in enumerate(argv):
        if argument == "-Fo" and index + 1 < len(argv):
            output = argv[index + 1]
        elif argument.startswith("-Fo") and len(argument) > 3:
            output = argument[3:]
    if output is None:
        print("precompiled-cc: missing -Fo; no verdict to replay", file=sys.stderr)
        return 2
    output = Path(output).absolute()
    for parent in output.parents:
        manifest = parent / "precompiled.json"
        if manifest.is_file():
            try:
                record = json.loads(manifest.read_text())["objects"].get(
                    output.relative_to(parent).as_posix()
                )
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"precompiled-cc: invalid {manifest}: {error}", file=sys.stderr)
                return 2
            if record is None:
                print(f"precompiled-cc: {output} has no compile record; "
                      "see PRECOMPILED.md", file=sys.stderr)
                return 2
            try:
                status = int(record["status"])
                message = str(record["output"])
            except (KeyError, TypeError, ValueError) as error:
                print(f"precompiled-cc: invalid verdict: {error}", file=sys.stderr)
                return 2
            if message:
                sys.stderr.write(message if message.endswith("\n") else message + "\n")
            return status
    print(f"precompiled-cc: no precompiled.json above {output}; "
          "see PRECOMPILED.md", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
