import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_runner as ap  # noqa: E402

URL = "https://hooks.slack.com/services/T000/B000/xyz"


def test_writes_webhook(tmp_path):
    env = tmp_path / ".brieflow_env"
    ap.set_slack_webhook(URL, env_path=env)
    assert f'BRIEFLOW_SLACK_WEBHOOK="{URL}"' in env.read_text()


def test_replaces_not_duplicates(tmp_path):
    env = tmp_path / ".brieflow_env"
    env.write_text('SOMETHING=1\nBRIEFLOW_SLACK_WEBHOOK="old"\n')
    ap.set_slack_webhook(URL, env_path=env)
    body = env.read_text()
    assert body.count("BRIEFLOW_SLACK_WEBHOOK=") == 1
    assert '"old"' not in body and "SOMETHING=1" in body


def test_rejects_non_https(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        ap.set_slack_webhook("ftp://nope", env_path=tmp_path / ".brieflow_env")
