"""SSE 传输编码只接受固定事件名，并保持单帧 JSON 边界。"""

import json

import pytest

from app.sse import encode_event


def test_encode_event_keeps_unicode_and_escapes_payload_newlines():
    encoded = encode_event("delta", {"content": "第一行\n第二行"}).decode("utf-8")

    assert encoded.startswith("event: delta\ndata: ")
    assert encoded.endswith("\n\n")
    assert encoded.count("\ndata:") == 1
    payload = encoded.splitlines()[1].removeprefix("data: ")
    assert json.loads(payload) == {"content": "第一行\n第二行"}


@pytest.mark.parametrize("event", ["", "Result", "bad event", "x\nerror"])
def test_encode_event_rejects_names_that_can_break_frames(event):
    with pytest.raises(ValueError):
        encode_event(event, {})
