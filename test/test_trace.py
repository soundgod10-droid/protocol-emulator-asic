# SPDX-License-Identifier: Apache-2.0
"""Traceability matrix in the regression (test/trace_check.py, rows in test/trace.csv).

What it does:
  Checks that every numbered section of docs/isa.md, docs/host_port.md and docs/helpers.md has a row, that each row's
  functional coverage groups, tests and proofs exist, and that the four planted faults of the self-test are caught.

Why:
  A new or renamed section, or a test or proof that is renamed or removed, fails here at once. The sweep workflow's
  table job runs the same check with its results files, so there the cited tests must also have passed.

No simulation (sim_time 0): this is not counted as an RTL testcase.
"""

import os
import sys

import cocotb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import trace_check  # noqa: E402


@cocotb.test()
async def test_trace_matrix(dut):
    rows = trace_check.load_rows()
    errors, table = trace_check.check(rows)
    for e in errors:
        dut._log.error(e)
    assert not errors, "traceability matrix: %d failures, first %s" % (len(errors), errors[0])
    assert trace_check.selftest(rows, log=dut._log.info), "a planted fault of the self-test was not caught"
    dut._log.info(trace_check.summary(table, errors))
