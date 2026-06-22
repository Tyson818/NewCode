import sys

from newcode.tools.command_tool import RunCommandTool
from newcode.tools.types import ToolContext


def context(tmp_path):
    return ToolContext(workspace_root=tmp_path, command_timeout_seconds=1)


def test_run_command_success(tmp_path):
    result = RunCommandTool().run(
        {"command": f'"{sys.executable}" -c "print(123)"'},
        context(tmp_path),
    )

    assert result.ok is True
    assert result.data["exit_code"] == 0
    assert "123" in result.data["stdout"]


def test_run_command_failure_keeps_output(tmp_path):
    command = (
        f'"{sys.executable}" -c "import sys; print(\'out\'); '
        f'print(\'err\', file=sys.stderr); sys.exit(2)"'
    )

    result = RunCommandTool().run({"command": command}, context(tmp_path))

    assert result.ok is False
    assert result.error.code == "command_failed"
    assert result.error.details["exit_code"] == 2
    assert "out" in result.error.details["stdout"]
    assert "err" in result.error.details["stderr"]


def test_run_command_timeout(tmp_path):
    result = RunCommandTool().run(
        {
            "command": f'"{sys.executable}" -c "import time; time.sleep(2)"',
            "timeout_seconds": 0.1,
        },
        context(tmp_path),
    )

    assert result.ok is False
    assert result.error.code == "timeout"


def test_run_command_rejects_empty_command(tmp_path):
    result = RunCommandTool().run({"command": ""}, context(tmp_path))

    assert result.ok is False
    assert result.error.code == "invalid_arguments"
