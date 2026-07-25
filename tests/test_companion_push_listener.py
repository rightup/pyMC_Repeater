"""Bounds and retention checks for the development push capture relay."""

import urllib.error
import urllib.request

import pytest

from companion_client.push_listener import (
    MAX_CAPTURED_PUSHES,
    MAX_PUSH_BODY_BYTES,
    PushListener,
)


def test_push_listener_rejects_oversized_body_before_retaining_it():
    listener = PushListener().start()
    try:
        request = urllib.request.Request(
            listener.url,
            data=b"x" * (MAX_PUSH_BODY_BYTES + 1),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=2)

        assert exc.value.code == 413
        assert list(listener.pushes) == []
    finally:
        listener.stop()


@pytest.mark.parametrize(
    "body",
    [
        b'{"value":NaN}',
        b'{"value":1e999}',
        b'{"value":1,"value":2}',
    ],
)
def test_push_listener_rejects_non_strict_json(body):
    listener = PushListener().start()
    try:
        request = urllib.request.Request(
            listener.url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=2)

        assert exc.value.code == 400
        assert list(listener.pushes) == []
    finally:
        listener.stop()


def test_push_listener_retains_only_the_latest_bounded_window():
    listener = PushListener().start()
    try:
        for index in range(MAX_CAPTURED_PUSHES + 1):
            listener._record({"index": index})

        latest, pushes = listener.captured_after(0)
        assert latest == MAX_CAPTURED_PUSHES + 1
        assert len(pushes) == MAX_CAPTURED_PUSHES
        assert pushes[0].body == {"index": 1}
        assert pushes[-1].body == {"index": MAX_CAPTURED_PUSHES}
    finally:
        listener.stop()


def test_push_listener_rejects_unretainable_wait_count():
    listener = PushListener().start()
    try:
        with pytest.raises(ValueError, match="between 1 and 256"):
            listener.wait_for_push(MAX_CAPTURED_PUSHES + 1, timeout=0)
    finally:
        listener.stop()
