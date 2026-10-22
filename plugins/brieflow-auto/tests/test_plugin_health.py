"""A NUL-filled known_marketplaces.json left the plugin unloadable and the agent read a stale
version's skills; nothing in the harness noticed."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_manifest as bm  # noqa: E402


def _registry(home, installed_version):
    reg = home / ".claude" / "plugins"
    reg.mkdir(parents=True)
    (reg / "known_marketplaces.json").write_text(json.dumps({"brieflow-auto": {}}))
    (reg / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "brieflow-auto@brieflow-auto": [{"version": installed_version}]}}))
    return reg


def test_healthy_install_has_no_findings(tmp_path):
    _registry(tmp_path, bm.plugin_version())
    assert bm.plugin_health(tmp_path) == []


def test_corrupt_registry_and_version_skew_are_reported(tmp_path):
    reg = _registry(tmp_path, "0.5.1")
    (reg / "known_marketplaces.json").write_bytes(b"\\x00" * 64)
    issues = bm.plugin_health(tmp_path)
    assert any("known_marketplaces.json is unreadable" in i for i in issues)
    assert any("installed ['0.5.1']" in i for i in issues)


def test_no_registry_is_not_an_error(tmp_path):
    assert bm.plugin_health(tmp_path) == []
