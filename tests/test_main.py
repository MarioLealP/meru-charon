import logging

import pytest

from charon.__main__ import main, run


def test_a_pass_with_nothing_to_purge_succeeds(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.INFO, logger="charon"):
        assert run() == 0
    assert "pass done" in caplog.text


def test_main_exits_with_the_pass_result():
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
