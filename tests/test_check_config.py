"""The config drift check that an update runs.

An update cannot overwrite .env or config/*.yaml -- they are gitignored, so
a pull leaves them alone. The cost of that safety is silence: a setting
added upstream exists only in the template, and the deployment keeps running
on the code's default without anyone being told a choice appeared. These
tests pin that telling, and pin that the tool never writes anything.
"""
import shutil

import pytest

from tools.check_config import compare, env_keys, main, report, yaml_keys

ENV_TEMPLATE = """\
# A comment, and a blank line follow.

HALO_BASE_URL=https://example.halo.example.com
POLL_INTERVAL_SECONDS=15
#RETIRED_SETTING=1
STALLED_AFTER_MINUTES=30
"""


@pytest.fixture
def deployment(tmp_path):
    """A deployment directory: templates plus the live copies beside them."""
    (tmp_path / "config").mkdir()
    (tmp_path / ".env.example").write_text(ENV_TEMPLATE)
    (tmp_path / ".env").write_text(ENV_TEMPLATE)
    return tmp_path


def test_a_commented_out_setting_is_not_a_setting():
    keys = env_keys(ENV_TEMPLATE)
    assert "RETIRED_SETTING" not in keys
    assert keys["POLL_INTERVAL_SECONDS"] == "15"


def test_nested_yaml_reads_as_dotted_paths():
    keys = yaml_keys("fields:\n  qty:\n    id: 7\n    name: Cables to Label\n")
    assert keys == {"fields.qty.id": "7", "fields.qty.name": "Cables to Label"}


def test_a_blank_yaml_value_is_still_a_key():
    """`id:` with nothing after it is how the template ships. It must count
    as present, or every fresh copy would look like it was missing keys."""
    assert yaml_keys("fields:\n  color:\n    id:\n") == {"fields.color.id": ""}


def test_matching_config_is_reported_as_clean(deployment, capsys):
    assert report(deployment) is True
    assert "nothing to carry across" in capsys.readouterr().out


def test_a_setting_added_upstream_is_named_with_its_default(deployment, capsys):
    """The case this exists for: an update adds a setting, and the running
    deployment has never heard of it."""
    (deployment / ".env").write_text(ENV_TEMPLATE.replace("STALLED_AFTER_MINUTES=30\n", ""))

    assert report(deployment) is False
    out = capsys.readouterr().out
    assert "+ STALLED_AFTER_MINUTES (template: 30)" in out


def test_a_setting_dropped_upstream_is_flagged_as_ignored(deployment, capsys):
    """A leftover does no harm, but it silently does nothing, and someone
    reading .env would reasonably believe it still applies."""
    (deployment / ".env").write_text(ENV_TEMPLATE + "MAX_CABLES_PER_REQUEST=250\n")

    assert report(deployment) is False
    out = capsys.readouterr().out
    assert "- MAX_CABLES_PER_REQUEST" in out
    assert "now ignored" in out


def test_a_missing_live_file_says_to_copy_the_template(deployment, capsys):
    """What a fresh install looks like."""
    (deployment / "config" / "layout.example.yaml").write_text("style:\n  pad_width: 4\n")

    assert report(deployment) is False
    assert "layout.yaml does not exist yet" in capsys.readouterr().out


def test_secrets_are_never_printed(deployment, capsys):
    """Values come from the committed template only. A live-only value --
    which in .env means a credential -- must never reach the output."""
    (deployment / ".env").write_text(ENV_TEMPLATE + "HALO_CLIENT_SECRET=hunter2-do-not-print\n")

    report(deployment)
    assert "hunter2" not in capsys.readouterr().out


def test_it_writes_nothing(deployment):
    """Merging config automatically is how a deployment ends up running on a
    value nobody chose. The tool suggests; a human edits."""
    (deployment / ".env").write_text(ENV_TEMPLATE.replace("STALLED_AFTER_MINUTES=30\n", ""))
    before = {p: p.read_bytes() for p in deployment.rglob("*") if p.is_file()}

    report(deployment)

    after = {p: p.read_bytes() for p in deployment.rglob("*") if p.is_file()}
    assert before == after


def test_broken_yaml_is_reported_not_raised(deployment, capsys):
    (deployment / "config" / "layout.example.yaml").write_text("style:\n  pad_width: 4\n")
    (deployment / "config" / "layout.yaml").write_text("style:\n  : : not yaml : :\n")

    assert report(deployment) is False
    assert "not valid YAML" in capsys.readouterr().out


def test_strict_exits_non_zero_only_when_asked(deployment, capsys):
    """An update script reports drift and carries on -- every new setting has
    a default, so drift is not a reason to refuse to restart."""
    (deployment / ".env").write_text(ENV_TEMPLATE.replace("STALLED_AFTER_MINUTES=30\n", ""))

    assert main(["--root", str(deployment)]) == 0
    assert main(["--root", str(deployment), "--strict"]) == 1


def test_the_real_repo_templates_are_comparable(repo_root):
    """A guard on the templates themselves: every config/*.example.yaml must
    parse, or the check would be reporting noise on a real deployment."""
    templates = sorted((repo_root / "config").glob("*.example.yaml"))
    assert templates, "the repo should ship config templates"
    for template in templates:
        notes, _, _ = compare(template, template)   # a copy of itself is clean
        assert notes == []


def test_a_fresh_copy_of_every_template_is_clean(repo_root, tmp_path):
    """Copying each template to its live name is the documented first step of
    an install. Doing exactly that must produce no findings."""
    (tmp_path / "config").mkdir()
    shutil.copy(repo_root / ".env.example", tmp_path / ".env.example")
    shutil.copy(repo_root / ".env.example", tmp_path / ".env")
    for template in (repo_root / "config").glob("*.example.yaml"):
        shutil.copy(template, tmp_path / "config" / template.name)
        shutil.copy(template, tmp_path / "config" / template.name.replace(".example", ""))

    assert report(tmp_path) is True
