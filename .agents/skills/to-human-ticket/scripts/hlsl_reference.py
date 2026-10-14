#!/usr/bin/env python3
"""Find receiver-scoped HLSL reference URLs in the official documentation index."""

import argparse
import http.client
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE_URL = "https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/"
INDEX_URL = BASE_URL + "toc.json"
NETWORK_TIMEOUT = 20


class InvalidIndex(ValueError):
    """The index cannot be safely used for an exact lookup."""


def official_url(href):
    if not isinstance(href, str) or not href or any(
        character.isspace() or ord(character) < 32 or character == "\\"
        for character in href
    ):
        raise InvalidIndex("href must be a nonempty URL without whitespace or backslashes")
    url = urllib.parse.urljoin(BASE_URL, href)
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "learn.microsoft.com"
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
    ):
        raise InvalidIndex(f"unsafe or nonofficial documentation URL: {href!r}")
    return url


class OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        official_url(new_url)
        return super().redirect_request(
            request, response, code, message, headers, new_url
        )


def load_index(cache):
    if cache is not None:
        with cache.open(encoding="utf-8-sig") as stream:
            return json.load(stream)
    opener = urllib.request.build_opener(OfficialRedirectHandler())
    with opener.open(INDEX_URL, timeout=NETWORK_TIMEOUT) as response:
        official_url(response.geturl())
        return json.load(response)


def child_nodes(node, location):
    children = []
    for key in ("items", "children"):
        if key not in node:
            continue
        if not isinstance(node[key], list):
            raise InvalidIndex(f"{location}.{key} must be an array")
        children.extend(
            (child, f"{location}.{key}[{index}]")
            for index, child in enumerate(node[key])
        )
    return children


def method_title_matches(title, method):
    # These are the index's explicit method, method-group and overload labels.
    # In particular, GetDimensions must not match GetDimensionsExtra.
    return (
        title == method
        or title == f"{method} methods"
        or (title.startswith(f"{method}(") and title.endswith(")"))
    )


def lookup(index, receiver, method):
    if not isinstance(index, dict) or not any(
        key in index for key in ("items", "children")
    ):
        raise InvalidIndex("index must be an object containing an items or children array")
    pending = [
        (node, location, False)
        for node, location in reversed(child_nodes(index, "index"))
    ]
    urls = {}
    while pending:
        node, location, in_receiver = pending.pop()
        if not isinstance(node, dict):
            raise InvalidIndex(f"{location} must be an object")
        title = node.get("toc_title")
        if not isinstance(title, str) or not title.strip():
            raise InvalidIndex(f"{location}.toc_title must be a nonempty string")
        url = official_url(node["href"]) if "href" in node else None
        children = child_nodes(node, location)
        if url is None and not children:
            raise InvalidIndex(f"{location} must have an href or child nodes")
        if in_receiver and method_title_matches(title, method) and url is not None:
            urls[url] = None
        pending.extend(
            (child, child_location, in_receiver or title == receiver)
            for child, child_location in reversed(children)
        )
    return urls


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receiver", help="exact receiver name, for example Texture2D")
    parser.add_argument("method", help="exact method name, for example GetDimensions")
    parser.add_argument(
        "--toc", type=Path, metavar="PATH", help="use a locally cached official toc.json"
    )
    args = parser.parse_args(argv)
    try:
        urls = lookup(load_index(args.toc), args.receiver, args.method)
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException, RecursionError) as error:
        print(f"hlsl-reference: cannot use documentation index: {error}", file=sys.stderr)
        return 2
    if not urls:
        print(
            f"hlsl-reference: no exact official entry for {args.receiver}::{args.method}; "
            "verify a generic reference separately",
            file=sys.stderr,
        )
        return 1
    print("\n".join(urls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
