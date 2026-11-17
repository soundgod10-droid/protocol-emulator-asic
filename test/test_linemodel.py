"""Self-tests of the line models (test/linemodel: USB 2.0 low-speed host and 10BASE-T link partner, written
from the standards), run with pytest inside the cocotb regression so that every test run checks the models
that judge the chip. No HDL is simulated (sim time 0)."""

import contextlib
import io
import os

import cocotb
import pytest

GL = os.environ.get("GATES") == "yes"
HERE = os.path.dirname(os.path.abspath(__file__))


@cocotb.test(skip=GL)
async def test_linemodel_selftests(dut):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = pytest.main(["-q", "-p", "no:cacheprovider", "--rootdir", HERE,
                          os.path.join(HERE, "linemodel_tests")])
    lines = out.getvalue().strip().splitlines()
    for line in lines[-25:]:
        dut._log.info(line)
    assert rc == 0, "line-model self-tests failed (pytest exit code %d)" % rc
