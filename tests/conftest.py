"""Shared pytest fixtures.

The fake-client fixtures live in ``test_agent_tool_response`` because that is
where the harness started. Re-exporting them here makes every fixture available
to the whole suite without each test module importing another test module —
which also means a test can request ``isolated_bot`` without ruff reporting the
import and the parameter as a redefinition.
"""

from test_agent_tool_response import isolated_bot  # noqa: F401  (a pytest fixture, re-exported on purpose)
