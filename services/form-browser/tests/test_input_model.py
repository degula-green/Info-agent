import pytest
from pydantic import ValidationError

from app.models import InputRequest


def test_input_accepts_the_four_takeover_actions() -> None:
    assert InputRequest(kind="click", x=10, y=20).kind == "click"
    assert InputRequest(kind="type", text="13800000000").text == "13800000000"
    assert InputRequest(kind="key", text="Enter").text == "Enter"
    assert InputRequest(kind="scroll", delta_y=400).delta_y == 400


def test_input_rejects_an_unknown_action() -> None:
    with pytest.raises(ValidationError):
        InputRequest(kind="evaluate", text="document.cookie")
