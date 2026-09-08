#!/usr/bin/env bash
# shellcheck shell=bash
#
# Shared assertions for the scripts/tests/*.test.sh scripts. Each test script
# sources this, calls check/contains/skip, and ends with `exit $fail`.
#
#     check "what it is"    "$got" "$want"
#     contains "what it is" "$got" "substring"
#     skip "why"

# shellcheck disable=SC2034 # the sourcing test script reads it, and exits with it
fail=0

check() {
    if [ "$2" = "$3" ]; then
        printf 'ok   %s\n' "$1"
    else
        printf 'FAIL %s\n       got  [%s]\n       want [%s]\n' "$1" "$2" "$3"
        fail=1
    fi
}

contains() {
    case "$2" in
    *"$3"*) printf 'ok   %s\n' "$1" ;;
    *)
        printf 'FAIL %s\n       got [%s]\n       want it to contain [%s]\n' "$1" "$2" "$3"
        fail=1
        ;;
    esac
}

lacks() {
    case "$2" in
    *"$3"*)
        printf 'FAIL %s\n       got [%s]\n       want it NOT to contain [%s]\n' "$1" "$2" "$3"
        fail=1
        ;;
    *) printf 'ok   %s\n' "$1" ;;
    esac
}

skip() { printf 'skip %s\n' "$1"; }
