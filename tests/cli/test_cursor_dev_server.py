"""Cursor CLI server selection in an omnidev environment."""

import pytest
from click.testing import CliRunner

from omnigent.cli import cli


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], "http://127.0.0.1:6767"),
        (["--server", "http://explicit:9000"], "http://explicit:9000"),
    ],
)
def test_cursor_dev_url_overrides_saved_server(monkeypatch, args, expected):
    monkeypatch.setenv("OMNIGENT_URL", "http://127.0.0.1:6767")
    monkeypatch.setattr(
        "omnigent.cli._load_effective_config", lambda: {"server": "https://saved.example"}
    )
    selected = []
    monkeypatch.setattr(
        "omnigent.cli._ensure_backend", lambda server: selected.append(server) or server
    )
    monkeypatch.setattr("omnigent.cursor_native.run_cursor_native", lambda **kwargs: None)
    result = CliRunner().invoke(cli, ["cursor", *args, "--", "--omnigent-native-protocol"])
    assert result.exit_code == 0, result.output
    assert selected == [expected]
