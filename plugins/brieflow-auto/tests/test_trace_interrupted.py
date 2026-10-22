"""Normal operator stops (tee/tmux pipe closed, Ctrl-C) were recorded as runner exceptions."""

import json
from argparse import Namespace

import pytest

from brieflow_trace import traced


@pytest.mark.parametrize("error,status", [(BrokenPipeError, "interrupted"),
                                          (KeyboardInterrupt, "interrupted"),
                                          (AttributeError, "exception")])
def test_stop_is_not_an_exception(tmp_path, error, status):
    @traced
    def run(args):
        raise error()

    with pytest.raises(error):
        run(Namespace(analysis_dir=tmp_path))
    record = json.loads(next((tmp_path / ".brieflow/traces").glob("*/trace.json")).read_text())
    assert record["status"] == status and record["exception_type"] == error.__name__
