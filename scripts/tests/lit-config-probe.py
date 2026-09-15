#!/usr/bin/env python3
"""Does a packaged lit.site.cfg.py survive what lit.cfg.py does to it?

offload-test-suite's lit.cfg.py assigns to lit_config.maxIndividualTestTime,
which this LLVM's lit has made a read-only property: the assignment raises,
and the run dies with a traceback instead of running the tests. The packaged
configuration neutralises that. This replays it against a stand-in LitConfig
that behaves like the strict one, so the self-test needs neither lit nor a
built compiler.

Prints "survived", or the exception the prologue failed to prevent.
"""

import sys
import types


class LitConfig:
    def __init__(self):
        self._maxIndividualTestTime = 0

    @property
    def maxIndividualTestTime(self):
        return self._maxIndividualTestTime

    @maxIndividualTestTime.setter
    def maxIndividualTestTime(self, value):
        raise AttributeError("lit_config.maxIndividualTestTime is read-only.")

    def note(self, message):
        pass


def main():
    module = types.ModuleType("lit.LitConfig")
    module.LitConfig = LitConfig
    package = types.ModuleType("lit")
    package.LitConfig = module
    sys.modules["lit"] = package
    sys.modules["lit.LitConfig"] = module

    # Only the part this workspace writes: the rest is CMake's, and running it
    # would want a real lit. The prologue ends with its own marker, so this
    # does not depend on what CMake put after it.
    MARKER = "# --- end of the hlsl-package prologue"
    text = open(sys.argv[1], encoding="utf-8").read()
    if MARKER not in text:
        print("no hlsl-package prologue in {}".format(sys.argv[1]))
        return 1
    prologue = text.split(MARKER)[0]
    config = LitConfig()
    # __file__ included: the packaged prologue locates the package from it.
    namespace = {"lit_config": config, "config": config, "__file__": sys.argv[1]}
    exec(compile(prologue, sys.argv[1], "exec"), namespace)

    try:
        config.maxIndividualTestTime = 300  # what lit.cfg.py does
    except AttributeError as exc:
        print("raised: {}".format(exc))
        return 1
    print("survived")
    return 0


if __name__ == "__main__":
    sys.exit(main())
