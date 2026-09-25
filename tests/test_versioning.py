import subprocess
from pathlib import Path

import pytest

from vidpull.semver import Version
from vidpull.versioning import (
    GitError,
    changelog_section,
    git,
    git_describe,
    parse_ls_remote,
    remote_tags,
    update_candidate,
)


def make_repo(root: Path, tags=("v0.1.0", "v0.2.0")) -> Path:
    """A bare repo with annotated tags, plus the working clone it was pushed from."""
    work = root / "work"
    work.mkdir()
    git(["init", "-q", "-b", "main"], cwd=work)
    for tag in tags:
        (work / "VERSION").write_text(tag + "\n")
        git(["add", "VERSION"], cwd=work)
        git(["commit", "-q", "-m", f"release {tag}"], cwd=work)
        git(["tag", "-a", tag, "-m", tag], cwd=work)
    bare = root / "origin.git"
    git(["clone", "-q", "--bare", str(work), str(bare)], cwd=root)
    return bare


def test_parse_ls_remote_counts_only_annotated_tags():
    out = (
        "a\trefs/tags/v0.1.0\na2\trefs/tags/v0.1.0^{}\n"
        "b\trefs/tags/v0.2.0-rc.1\nb2\trefs/tags/v0.2.0-rc.1^{}\n"
        "c\trefs/tags/v0.10.0\nc2\trefs/tags/v0.10.0^{}\n"
        "d\trefs/tags/release/v9\nd2\trefs/tags/release/v9^{}\n"
        "e\trefs/tags/v0.3.0\n"  # lightweight: no peeled line
        "f\trefs/heads/main\n"
    )
    assert [str(v) for v in parse_ls_remote(out)] == ["0.1.0", "0.2.0-rc.1", "0.10.0"]


def test_update_candidate():
    tags = [Version.parse(t) for t in ("v0.1.0", "v0.2.0", "v0.3.0-rc.1")]
    assert str(update_candidate(Version.parse("0.1.0"), tags)) == "0.2.0"
    assert update_candidate(Version.parse("0.2.0"), tags) is None
    assert (
        str(update_candidate(Version.parse("0.2.0"), tags, include_prerelease=True)) == "0.3.0-rc.1"
    )
    assert str(update_candidate(None, tags)) == "0.2.0"
    assert update_candidate(Version.parse("1.0.0"), []) is None


def test_changelog_section():
    text = "# Changelog\n\n## [Unreleased]\n\n- wip\n\n## [0.2.0] - 2026-09-26\n\n### Added\n- picker\n\n## [0.1.0] - 2026-09-25\n- first\n"
    assert changelog_section(text, "v0.2.0") == "### Added\n- picker"
    assert changelog_section(text, Version.parse("0.1.0")) == "- first"
    assert changelog_section(text, "9.9.9") == ""


def test_remote_tags_and_describe(tmp_path):
    bare = make_repo(tmp_path)
    tags = remote_tags(f"file://{bare}")
    assert [str(t) for t in tags] == ["0.1.0", "0.2.0"]
    assert git_describe(tmp_path / "work") == "v0.2.0"
    assert git_describe(tmp_path) is None
    with pytest.raises(GitError):
        remote_tags(f"file://{tmp_path}/missing.git")


def test_git_never_prompts(tmp_path):
    with pytest.raises(GitError):
        git(["ls-remote", "https://127.0.0.1:9/none.git"], timeout=20)
    proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0
