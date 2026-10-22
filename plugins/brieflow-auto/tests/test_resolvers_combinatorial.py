import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_interview as bi  # noqa: E402

SCREEN = {
    "iss": {
        "channels": [{"name": "DAPI"}, {"name": "Red"}, {"name": "Green"}],
        "combinatorial": {"code": {"A": ["Red"], "C": ["Green"], "G": ["Red", "Green"], "T": []}},
    }
}

def test_bases_ordered_by_channel_names():
    out = bi._compute_combinatorial_channels(SCREEN)
    assert out == {"bases": ["Red", "Green"], "extra_channels": ["DAPI"]}

def test_none_when_code_missing():
    assert bi._compute_combinatorial_channels({"iss": {"channels": [{"name": "DAPI"}]}}) is None

def test_none_when_code_value_is_scalar_string():
    screen = {
        "iss": {
            "channels": [{"name": "DAPI"}, {"name": "Red"}, {"name": "Green"}],
            "combinatorial": {"code": {"A": "Red", "C": ["Green"]}},
        }
    }
    assert bi._compute_combinatorial_channels(screen) is None

def test_registered():
    assert "combinatorial_channels" in bi._COMPUTED_DERIVATIONS
