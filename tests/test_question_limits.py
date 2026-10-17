"""问答、工作流和追问以去除首尾空白后的 Unicode 码点计数。"""

import pytest
from pydantic import ValidationError

from app.schemas import AskRequest, TurnIn, WorkflowRequest


@pytest.mark.parametrize("symbol", ["a", "中", "😀", "e\u0301"])
@pytest.mark.parametrize("model,field,extra", [
    (AskRequest, "question", {}),
    (WorkflowRequest, "task", {}),
    (TurnIn, "question", {"answer": "回答"}),
])
def test_question_code_point_limit(model, field, extra, symbol):
    text = (symbol * 500)[:500]
    assert getattr(model(**{field: f" \t{text}\n", **extra}), field) == text
    with pytest.raises(ValidationError):
        model(**{field: text + "中", **extra})


@pytest.mark.parametrize("text", ["", " \t\r\n", "\u3000\u0085", "\x1c"])
def test_question_rejects_empty_after_trimming(text):
    with pytest.raises(ValidationError):
        AskRequest(question=text)
