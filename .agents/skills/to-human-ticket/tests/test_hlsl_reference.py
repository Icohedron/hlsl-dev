"""Exercise the supported offline index interface through the public CLI."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hlsl_reference.py"
BASE_URL = "https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/"


def invoke(cache, receiver="Texture2D", method="GetDimensions"):
    return subprocess.run(
        [sys.executable, str(SCRIPT), receiver, method, "--toc", str(cache)],
        capture_output=True,
        text=True,
        timeout=10,
    )


def cached_index(tmp_path, index):
    cache = tmp_path / "toc.json"
    cache.write_text(json.dumps(index), encoding="utf-8")
    return cache


def receiver_node(name="Texture2D", methods=None):
    return {
        "toc_title": name,
        "children": methods if methods is not None else [
            {
                "toc_title": "GetDimensions",
                "href": "sm5-object-texture2d-getdimensions",
            }
        ],
    }


def test_exact_receiver_and_method_return_only_scoped_url(tmp_path):
    index = {
        "items": [
            {
                "toc_title": "GetDimensions (DirectX HLSL Texture Object)",
                "href": "dx-graphics-hlsl-to-getdimensions",
            },
            receiver_node("RWTexture2D", [
                {
                    "toc_title": "GetDimensions",
                    "href": "sm5-object-rwtexture2d-getdimensions",
                }
            ]),
            receiver_node(methods=[
                {
                    "toc_title": "GetDimensionsExtra",
                    "href": "must-not-select-prefix",
                },
                {
                    "toc_title": "GetDimensions",
                    "href": "sm5-object-texture2d-getdimensions",
                },
            ]),
        ]
    }

    result = invoke(cached_index(tmp_path, index))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [BASE_URL + "sm5-object-texture2d-getdimensions"]
    assert result.stderr == ""


def test_nested_groups_and_overloads_use_both_child_container_forms(tmp_path):
    index = {
        "children": [
            {
                "toc_title": "Object reference",
                "items": [receiver_node(methods=[
                    {
                        "toc_title": "Methods",
                        "items": [
                            {
                                "toc_title": "Load methods",
                                "href": "texture2d-load",
                                "children": [
                                    {
                                        "toc_title": "Load(int,int,uint)",
                                        "href": "t2d-load-float-int-uint-",
                                    },
                                    {
                                        "toc_title": "LoadExtra(int)",
                                        "href": "not-an-exact-method",
                                    },
                                ],
                            }
                        ],
                    }
                ])],
            }
        ]
    }

    result = invoke(cached_index(tmp_path, index), method="Load")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        BASE_URL + "texture2d-load",
        BASE_URL + "t2d-load-float-int-uint-",
    ]


def test_duplicate_urls_are_deduplicated_after_relative_resolution(tmp_path):
    href = "sm5-object-texture2d-getdimensions"
    index = {
        "items": [receiver_node(methods=[
            {"toc_title": "GetDimensions", "href": href},
            {"toc_title": "GetDimensions", "href": "./" + href},
            {"toc_title": "GetDimensions", "href": BASE_URL + href},
            {
                "toc_title": "GetDimensions",
                "href": "/en-us/windows/win32/direct3dhlsl/" + href,
            },
        ])]
    }

    result = invoke(cached_index(tmp_path, index))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [BASE_URL + href]


@pytest.mark.parametrize("receiver", ["TextureCube", "TextureCubeArray", "texture2d"])
def test_no_exact_receiver_method_never_uses_generic_or_other_receiver(tmp_path, receiver):
    generic = {
        "toc_title": "GetDimensions",
        "href": "dx-graphics-hlsl-to-getdimensions",
    }
    index = {
        "items": [
            generic,
            generic,
            receiver_node(),
            receiver_node("TextureCube", [
                {"toc_title": "Sample", "href": "texturecube-sample"}
            ]),
        ]
    }

    result = invoke(cached_index(tmp_path, index), receiver=receiver)

    assert result.returncode == 1
    assert result.stdout == ""
    assert f"{receiver}::GetDimensions" in result.stderr
    assert "no exact" in result.stderr


def test_method_matching_is_case_sensitive(tmp_path):
    result = invoke(cached_index(tmp_path, {"items": [receiver_node()]}), method="getdimensions")

    assert result.returncode == 1
    assert result.stdout == ""


@pytest.mark.parametrize("index", [
    None,
    [],
    {},
    {"items": {}},
    {"items": [None]},
    {"items": ["Texture2D"]},
    {"items": [{"toc_title": 7, "href": "some-page"}]},
    {"items": [{"href": "some-page"}]},
    {"items": [{"toc_title": "Texture2D", "children": {}}]},
    {"items": [{"toc_title": "Texture2D", "href": 7}]},
    {"items": [{"toc_title": "Texture2D"}]},
])
def test_malformed_index_shapes_are_errors_not_missing_matches(tmp_path, index):
    result = invoke(cached_index(tmp_path, index))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot use documentation index" in result.stderr


@pytest.mark.parametrize("href", [
    "http://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/page",
    "https://example.com/page",
    "//example.com/page",
    "https://learn.microsoft.com.example.com/page",
    "https://user@learn.microsoft.com/page",
    "https://learn.microsoft.com:444/page",
    "https://learn.microsoft.com:invalid/page",
    "javascript:alert(1)",
    "file:///etc/passwd",
    "https://learn.microsoft.com\\@example.com/page",
    "some-page\nother-page",
    "",
])
def test_unsafe_or_nonofficial_hrefs_are_rejected_without_partial_output(tmp_path, href):
    index = {"items": [receiver_node(), receiver_node(methods=[
        {"toc_title": "GetDimensions", "href": href}
    ])]}

    result = invoke(cached_index(tmp_path, index))

    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot use documentation index" in result.stderr


def test_malformed_json_is_an_index_error(tmp_path):
    cache = tmp_path / "toc.json"
    cache.write_text('{"items": [', encoding="utf-8")

    result = invoke(cache)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot use documentation index" in result.stderr


@pytest.mark.parametrize("cache_kind", ["missing", "directory"])
def test_unreadable_cache_is_an_io_error(tmp_path, cache_kind):
    cache = tmp_path if cache_kind == "directory" else tmp_path / "missing.json"

    result = invoke(cache)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "cannot use documentation index" in result.stderr


def test_empty_valid_index_has_no_match(tmp_path):
    result = invoke(cached_index(tmp_path, {"items": []}))

    assert result.returncode == 1
    assert result.stdout == ""
    assert "no exact" in result.stderr
