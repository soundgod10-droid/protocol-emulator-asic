# SPDX-License-Identifier: Apache-2.0
"""Stand-in for the cocotb package, so that the model test modules can be imported without a simulator.

The model test modules import cocotb and mark each test function with @cocotb.test(...). Here the
decorator returns the function unchanged. Model-only tests never await a simulator event.
"""


def test(*args, **kwargs):
    return lambda fn: fn
