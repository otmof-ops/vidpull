"""Update pipeline against a local bare repo; the venv build step is replaced by a fake generation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.test_versioning import make_repo
from vidpull.config import Config
from vidpull.manifest import Generation, Manifest
from vidpull.semver import Version
from vidpull.update import (
    BusyError,
    Layout,
    NotInstalledError,
    UpdateError,
    UpdatePipeline,
    finalize_install,
    launcher_script,
)
from vidpull.versioning import git


def fake_generation(venv: Path, version: str, *, broken: bool = False) -> None:
    """A venv-shaped directory whose vidpull prints ``version`` and whose python is this one."""
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"  # a wrapper, not a symlink, so Python keeps this venv's prefix
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    script = venv / "bin" / "vidpull"
    script.write_text(f"#!/bin/sh\necho {'9.9.9' if broken else version}\n")
    script.chmod(0o755)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    bare = make_repo(root, tags=())
    work = root / "work"
    for tag, note in (("v0.1.0", "first"), ("v0.2.0", "picker")):
        (work / "pyproject.toml").write_text(
            f'[project]\nname = "vidpull"\nversion = "{tag[1:]}"\n'
        )
        (work / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## [Unreleased]\n\n## [{tag[1:]}] - 2026-09-26\n\n- {note}\n"
        )
        git(["add", "."], cwd=work)
        git(["commit", "-q", "-m", f"release {tag}"], cwd=work)
        git(["tag", "-a", tag, "-m", tag], cwd=work)
    (work / "pyproject.toml").write_text('[project]\nname = "vidpull"\nversion = "0.3.0"\n')
    git(["commit", "-q", "-am", "lightweight"], cwd=work)
    git(["tag", "v0.3.0"], cwd=work)  # lightweight on purpose
    git(["push", "-q", "--tags", str(bare), "main"], cwd=work)
    return f"file://{bare}"


@pytest.fixture
def installed(paths, repo):
    """An installed generation 0001-v0.1.0 with current pointing at it and a launcher."""
    layout = Layout(paths)
    gen_dir = layout.gen_dir("0001-v0.1.0")
    fake_generation(gen_dir / "venv", "0.1.0")
    manifest = Manifest(repo_url=repo)
    manifest.activate(
        Generation("0001-v0.1.0", "v0.1.0", "a" * 40, "0.1.0", "2026.01.01", sys.executable)
    )
    manifest.launcher = str(paths.launcher)
    layout.point_current("0001-v0.1.0")
    manifest.save(paths.manifest_file)
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.launcher.write_text(launcher_script(paths))
    return manifest


def make_pipeline(paths, repo, monkeypatch, *, broken=False, lines=None):
    pipeline = UpdatePipeline(
        paths,
        Config(),
        repo_url=repo,
        report=(lines.append if lines is not None else None),
        python=sys.executable,
    )

    def fake_build(self, gen_id, gen_dir, build_dir):
        assert (build_dir / "pyproject.toml").exists(), "build dir must be the cloned tag"
        gen_dir.mkdir(parents=True, exist_ok=True)
        fake_generation(gen_dir / "venv", gen_id.split("-", 1)[1][1:], broken=broken)
        return self._verify(gen_dir / "venv", Version.parse(gen_id.split("-", 1)[1]))

    monkeypatch.setattr(UpdatePipeline, "_build", fake_build)
    return pipeline


def test_check_finds_candidate_and_ignores_lightweight_tags(paths, repo, installed):
    pipeline = UpdatePipeline(paths, Config(), repo_url=repo, python=sys.executable)
    assert pipeline.installed
    check = pipeline.check()
    assert not check.error and str(check.current) == "0.1.0"
    assert str(check.latest) == "0.2.0" and str(check.candidate) == "0.2.0"


def test_run_builds_switches_and_prunes(paths, repo, installed, monkeypatch):
    lines: list[str] = []
    pipeline = make_pipeline(paths, repo, monkeypatch, lines=lines)
    layout = Layout(paths)
    with pytest.raises(UpdateError, match="not an annotated tag"):
        pipeline.run(target=Version.parse("0.3.0"))
    assert layout.current_target() == layout.venv_dir("0001-v0.1.0").resolve()

    result = pipeline.run(target=Version.parse("0.2.0"))
    assert result.ok and result.restart_required and result.generation.id == "0002-v0.2.0"
    assert layout.current_target() == layout.venv_dir("0002-v0.2.0").resolve()
    assert os.readlink(layout.current_link) == "gen/0002-v0.2.0/venv"
    manifest = Manifest.load(paths.manifest_file)
    assert manifest.current == "0002-v0.2.0" and manifest.previous == "0001-v0.1.0"
    assert manifest.current_generation.ytdlp_version and manifest.current_generation.commit
    assert not list(layout.build_root.glob("src-*"))
    assert any("- picker" in line for line in lines)  # changelog section reported
    assert "vidpull launcher" in paths.launcher.read_text()

    # a second update (engine only) creates a third generation and prunes the first
    result = pipeline.run(engine_only=True)
    assert result.generation.id == "0003-v0.2.0"
    manifest = Manifest.load(paths.manifest_file)
    assert [g.id for g in manifest.generations] == ["0002-v0.2.0", "0003-v0.2.0"]
    assert not layout.gen_dir("0001-v0.1.0").exists()

    # nothing newer than the highest stable tag
    assert "already up to date" in pipeline.run().message


def test_failed_verification_leaves_current_untouched(paths, repo, installed, monkeypatch):
    pipeline = make_pipeline(paths, repo, monkeypatch, broken=True)
    layout = Layout(paths)
    with pytest.raises(UpdateError, match="reports version"):
        pipeline.run(target=Version.parse("0.2.0"))
    assert layout.current_target() == layout.venv_dir("0001-v0.1.0").resolve()
    assert not layout.gen_dir("0002-v0.2.0").exists()
    assert Manifest.load(paths.manifest_file).current == "0001-v0.1.0"
    assert not list(layout.build_root.glob("src-*"))


def test_rollback_swaps_generations(paths, repo, installed, monkeypatch):
    pipeline = make_pipeline(paths, repo, monkeypatch)
    pipeline.run(target=Version.parse("0.2.0"))
    result = pipeline.rollback()
    assert result.ok and result.generation.id == "0001-v0.1.0"
    layout = Layout(paths)
    assert layout.current_target() == layout.venv_dir("0001-v0.1.0").resolve()
    manifest = Manifest.load(paths.manifest_file)
    assert manifest.current == "0001-v0.1.0" and manifest.previous == "0002-v0.2.0"


def test_guards(paths, repo, installed, monkeypatch):
    pipeline = make_pipeline(paths, repo, monkeypatch)
    with pytest.raises(BusyError):
        pipeline.run(target=Version.parse("0.2.0"), busy=lambda: True)
    plan = pipeline.run(target=Version.parse("0.2.0"), dry_run=True)
    assert plan.ok and "0002-v0.2.0" in plan.message
    assert Layout(paths).current_target() == Layout(paths).venv_dir("0001-v0.1.0").resolve()


def test_not_installed(paths, repo):
    pipeline = UpdatePipeline(paths, Config(), repo_url=repo, python=sys.executable)
    assert not pipeline.installed
    with pytest.raises(NotInstalledError):
        pipeline.run()
    with pytest.raises(NotInstalledError):
        pipeline.rollback()
    check = pipeline.check()
    assert check.candidate is not None  # checking still works from a checkout


def test_finalize_install_registers_generation(paths, repo):
    layout = Layout(paths)
    gen_dir = layout.gen_dir("0001-v0.1.0")
    fake_generation(gen_dir / "venv", "0.1.0")
    manifest = finalize_install(
        paths, gen_dir=gen_dir, tag="v0.1.0", commit="b" * 40, python=sys.executable, repo_url=repo
    )
    assert manifest.current == "0001-v0.1.0" and manifest.launcher == str(paths.launcher)
    assert layout.current_target() == (gen_dir / "venv").resolve()
    assert paths.launcher.exists() and os.access(paths.launcher, os.X_OK)
    assert f"{paths.data_dir}/current/bin/vidpull" in paths.launcher.read_text()
    # an untagged checkout install carries a build suffix that verification ignores
    gen2 = layout.gen_dir("0002-v0.1.0+abc123")
    fake_generation(gen2 / "venv", "0.1.0")
    manifest = finalize_install(
        paths,
        gen_dir=gen2,
        tag="v0.1.0+abc123",
        commit="c" * 40,
        python=sys.executable,
        repo_url=repo,
    )
    assert manifest.current == "0002-v0.1.0+abc123" and manifest.previous == "0001-v0.1.0"
