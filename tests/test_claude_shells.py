from unittest.mock import Mock, patch

from huginn.sources.claude_code import child_shell_count


@patch("huginn.sources.claude_code.subprocess.run")
def test_counts_only_direct_shell_children(run: Mock) -> None:
    run.return_value.stdout = """\
 77968 /Users/hljod/.local/bin/caffeinate
 77968 /bin/zsh
 77968 /bin/zsh
 77968 /bin/zsh
 37897 /bin/sleep
 11111 /bin/zsh
"""

    assert child_shell_count(77968) == 3


@patch("huginn.sources.claude_code.subprocess.run", side_effect=OSError)
def test_shell_count_degrades_to_zero(_run: Mock) -> None:
    assert child_shell_count(77968) == 0
