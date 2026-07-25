"""Focused type-contract tests for shared API JSON validation."""

import io

import cherrypy
import pytest

import repeater.web.api_validation as api_validation
from repeater.web.api_validation import (
    finite_float_field,
    integer_field,
    read_json_object,
    reject_control_characters,
    text_field,
)


@pytest.mark.parametrize("value", [True, False, 1.0, 1.5, "1"])
def test_integer_field_rejects_non_integer_json_types(value):
    with pytest.raises(cherrypy.HTTPError) as exc:
        integer_field({"value": value}, "value")
    assert exc.value.status == 400


def test_integer_field_accepts_json_integer():
    assert integer_field({"value": 3}, "value", low=1, high=4) == 3


@pytest.mark.parametrize("value", [True, False, "1.5"])
def test_float_field_rejects_non_numeric_json_types(value):
    with pytest.raises(cherrypy.HTTPError) as exc:
        finite_float_field({"value": value}, "value")
    assert exc.value.status == 400


@pytest.mark.parametrize(("value", "expected"), [(3, 3.0), (3.5, 3.5)])
def test_float_field_accepts_json_numbers(value, expected):
    assert finite_float_field({"value": value}, "value") == expected


def test_float_field_turns_huge_integer_overflow_into_clean_400():
    with pytest.raises(cherrypy.HTTPError) as exc:
        finite_float_field({"value": 10**1000}, "value")

    assert exc.value.status == 400
    assert "value must be finite" in str(exc.value)


def test_text_field_rejects_lone_unicode_surrogate():
    with pytest.raises(cherrypy.HTTPError) as exc:
        text_field(
            {"value": "\ud800"},
            "value",
            required=True,
            max_bytes=10,
        )

    assert exc.value.status == 400
    assert "valid UTF-8" in str(exc.value)


@pytest.mark.parametrize(
    "value",
    ("\x00", "\x7f", "\u0085", "\u200b", "\u2028", "\u202e"),
)
def test_human_readable_text_rejects_nonprinting_unicode(value):
    with pytest.raises(cherrypy.HTTPError) as exc:
        reject_control_characters(f"before{value}after", "label")

    assert exc.value.status == 400
    assert "control characters" in str(exc.value)


@pytest.mark.parametrize("value", ("plain text", "café", "single emoji 🚀"))
def test_human_readable_text_accepts_printable_unicode(value):
    reject_control_characters(value, "label")


def test_json_object_rejects_duplicate_fields_at_any_depth(monkeypatch):
    raw = b'{"outer":{"value":1,"value":2}}'
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {"headers": {"Content-Length": str(len(raw))}, "body": io.BytesIO(raw)},
        )(),
        raising=False,
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object()

    assert exc.value.status == 400
    assert "Duplicate JSON field: value" in str(exc.value)


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_json_object_rejects_non_standard_non_finite_numbers(monkeypatch, constant):
    raw = f'{{"value":{constant}}}'.encode("ascii")
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {"headers": {"Content-Length": str(len(raw))}, "body": io.BytesIO(raw)},
        )(),
        raising=False,
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object()

    assert exc.value.status == 400
    assert "Non-finite JSON number" in str(exc.value)


def test_json_object_rejects_decimal_that_overflows_to_infinity(monkeypatch):
    raw = b'{"value":1e999}'
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {"headers": {"Content-Length": str(len(raw))}, "body": io.BytesIO(raw)},
        )(),
        raising=False,
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object()

    assert exc.value.status == 400
    assert "Non-finite JSON number" in str(exc.value)


def test_json_object_rejects_negative_content_length(monkeypatch):
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {"headers": {"Content-Length": "-1"}, "body": io.BytesIO(b"{}")},
        )(),
        raising=False,
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object()

    assert exc.value.status == 400
    assert "Content-Length" in str(exc.value)


@pytest.mark.parametrize("content_type", ("", "text/plain", "application/x-www-form-urlencoded"))
def test_json_media_type_requirement_rejects_simple_cross_origin_types(
    monkeypatch,
    content_type,
):
    raw = b"{}"
    headers = {"Content-Length": str(len(raw))}
    if content_type:
        headers["Content-Type"] = content_type
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {"headers": headers, "body": io.BytesIO(raw)},
        )(),
        raising=False,
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object(require_json_content_type=True)

    assert exc.value.status == 415


def test_json_media_type_requirement_accepts_charset(monkeypatch):
    raw = b"{}"
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {
                "headers": {
                    "Content-Length": str(len(raw)),
                    "Content-Type": "Application/JSON; charset=utf-8",
                },
                "body": io.BytesIO(raw),
            },
        )(),
        raising=False,
    )

    assert read_json_object(require_json_content_type=True) == {}


def test_json_media_type_requirement_can_allow_a_truly_empty_action_body(
    monkeypatch,
):
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {
                "headers": {"Content-Length": "0"},
                "body": io.BytesIO(b""),
            },
        )(),
        raising=False,
    )

    assert read_json_object(
        require_json_content_type=True,
        allow_empty_without_content_type=True,
    ) == {}


def test_empty_action_exception_still_rejects_non_json_content(monkeypatch):
    raw = b"{}"
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {
                "headers": {
                    "Content-Length": str(len(raw)),
                    "Content-Type": "text/plain",
                },
                "body": io.BytesIO(raw),
            },
        )(),
        raising=False,
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object(
            require_json_content_type=True,
            allow_empty_without_content_type=True,
        )

    assert exc.value.status == 415


def test_json_object_turns_excessive_nesting_into_400(monkeypatch):
    raw = b'{"value":[]}'
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {"headers": {"Content-Length": str(len(raw))}, "body": io.BytesIO(raw)},
        )(),
        raising=False,
    )
    monkeypatch.setattr(
        api_validation.json,
        "loads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RecursionError()),
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object()

    assert exc.value.status == 400


def test_json_object_rejects_excessive_structural_depth(monkeypatch):
    raw = (
        b'{"value":'
        + b"[" * (api_validation.MAX_JSON_NESTING_DEPTH + 1)
        + b"0"
        + b"]" * (api_validation.MAX_JSON_NESTING_DEPTH + 1)
        + b"}"
    )
    monkeypatch.setattr(
        cherrypy,
        "request",
        type(
            "Request",
            (),
            {"headers": {"Content-Length": str(len(raw))}, "body": io.BytesIO(raw)},
        )(),
        raising=False,
    )

    with pytest.raises(cherrypy.HTTPError) as exc:
        read_json_object()

    assert exc.value.status == 400
    assert "nesting levels" in str(exc.value)
