from pathlib import Path

SKILLS = Path(__file__).resolve().parent.parent / "skills"
# Read-only or reporting skills make no in-place workaround.
EXEMPT = {"brieflow-status", "brieflow-improve", "brieflow-issue"}


def test_every_acting_skill_requires_record_finding():
    missing = [
        d.name
        for d in sorted(SKILLS.iterdir())
        if d.is_dir() and d.name not in EXEMPT
        and "record_finding()" not in (d / "SKILL.md").read_text()
    ]
    assert not missing, f"skills without the record_finding() rule: {missing}"
