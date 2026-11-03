import pytest

from charon import __main__ as charon


@pytest.mark.parametrize("code", [0, 1])
def test_main_exits_with_the_pass_result(monkeypatch: pytest.MonkeyPatch, code: int):
    monkeypatch.setattr(charon, "run", lambda: code)
    with pytest.raises(SystemExit) as exited:
        charon.main()
    assert exited.value.code == code
