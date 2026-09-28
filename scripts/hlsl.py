#!/usr/bin/env python3
"""Run the public checkout-source HLSL command layer."""

import sys

# Do not create __pycache__ in the checkout, even when invoked
# without -B from a Python interpreter outside the developer environment.
sys.dont_write_bytecode = True

from hlsl_cli.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
