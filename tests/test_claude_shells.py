from unittest.mock import patch

from huginn.sources.claude_code import child_shell_count


@patch("huginn.sources.claude_code._platform.children", return_value=[1, 2, 3, 4])
@patch("huginn.sources.claude_code._platform.process_name",
       side_effect=["caffeinate", "zsh", "zsh", "sleep"])
def test_counts_only_direct_shell_children(_names, _children) -> None:
    assert child_shell_count(77968) == 2


@patch("huginn.sources.claude_code._platform.children", return_value=[])
def test_shell_count_degrades_to_zero(_children) -> None:
    assert child_shell_count(77968) == 0
