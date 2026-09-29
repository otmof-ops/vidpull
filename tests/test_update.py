"""Update pipeline against a local bare repo; the venv build step is replaced by a fake generation."""

from __future__ import annotations

import os
import shutil
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
    install_spotdl,
    launcher_script,
    note_first_run,
    stale_generation,
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
    assert not check.error and str(check.latest) == "0.2.0"  # checking still works from a checkout


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


def test_finalize_install_prunes_to_two_generations(paths, repo):
    layout = Layout(paths)
    for n, tag in ((1, "v0.1.0"), (2, "v0.2.0"), (3, "v0.3.0")):
        gen_dir = layout.gen_dir(f"{n:04d}-{tag}")
        fake_generation(gen_dir / "venv", tag[1:])
        finalize_install(
            paths, gen_dir=gen_dir, tag=tag, commit="d" * 40, python=sys.executable, repo_url=repo
        )
    manifest = Manifest.load(paths.manifest_file)
    assert manifest.current == "0003-v0.3.0" and manifest.previous == "0002-v0.2.0"
    assert sorted(p.name for p in layout.gen_root.iterdir()) == ["0002-v0.2.0", "0003-v0.3.0"]
    assert [g.id for g in manifest.generations] == ["0002-v0.2.0", "0003-v0.3.0"]


def log_lines(paths) -> list[str]:
    return paths.update_log.read_text().splitlines() if paths.update_log.exists() else []


def transactions(paths) -> list[tuple[str, str]]:
    """(BEGIN text, END text) per transaction, in order."""
    begins: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []
    for line in log_lines(paths):
        _stamp, txn, kind, text = line.split(None, 3)
        if kind == "BEGIN":
            begins[txn] = text
        elif kind == "END":
            pairs.append((begins[txn], text))
    return pairs


def test_every_transaction_is_logged(paths, repo, installed, monkeypatch):
    pipeline = make_pipeline(paths, repo, monkeypatch)
    pipeline.trigger = "tui"
    pipeline.check()
    pipeline.run(trigger="auto")  # installs v0.2.0
    pipeline.run(engine_only=True)
    pipeline.rollback(trigger="cli")
    with pytest.raises(UpdateError):
        pipeline.run(target=Version.parse("0.3.0"))  # a lightweight tag is not a release
    with pytest.raises(BusyError):
        pipeline.run(busy=lambda: True)
    plan = pipeline.run(target=Version.parse("0.2.0"), dry_run=True)
    assert plan.ok

    done = transactions(paths)
    assert [begin.split()[0] for begin, _ in done] == [
        "check", "update", "engine-only", "rollback", "update", "update", "update",
    ]  # fmt: skip
    check_begin, check_end = done[0]
    assert "trigger=tui" in check_begin and "gen=0001-v0.1.0" in check_begin
    assert "vidpull=0.1.0" in check_begin and 'spotdl="not installed"' in check_begin
    assert check_end.startswith("ok ") and "installed v0.1.0, newest release v0.2.0" in check_end
    assert "update available: v0.2.0" in check_end and "not in a release yet" in check_end

    update_begin, update_end = done[1]
    assert "trigger=auto" in update_begin and "gen=0001-v0.1.0" in update_begin
    assert "installed v0.2.0 as generation 0002-v0.2.0" in update_end
    assert "gen=0002-v0.2.0" in update_end and "vidpull=0.2.0" in update_end

    engine_begin, engine_end = done[2]
    assert "gen=0002-v0.2.0" in engine_begin and "gen=0003-v0.2.0" in engine_end
    assert "engines refreshed" in engine_end and "the app is unchanged at v0.2.0" in engine_end

    assert "trigger=cli" in done[3][0] and "rolled back to v0.2.0" in done[3][1]
    assert done[4][1].startswith("failed ") and "not an annotated tag" in done[4][1]
    assert done[5][1].startswith("failed ") and "BusyError: downloads are running" in done[5][1]
    assert "(dry run)" in done[6][0] and "dry run: would build generation" in done[6][1]

    text = "\n".join(log_lines(paths))
    assert "fetching v0.2.0 from" in text and "- picker" in text  # the pipeline's own lines
    assert "switched current to 0002-v0.2.0" in text and "pruning old generation" in text


def test_check_says_when_the_repository_is_ahead_of_its_newest_release(
    paths, repo, installed, tmp_path
):
    check = UpdatePipeline(paths, Config(), repo_url=repo, python=sys.executable).check()
    assert check.unreleased and len(check.head) == 40 and check.head != check.latest_commit
    assert any(
        "changes newer than v0.2.0" in line and check.head[:12] in line for line in check.lines()
    )

    root = tmp_path / "released"
    root.mkdir()
    bare = make_repo(root, tags=("v0.1.0", "v0.2.0"))  # main stops at the newest tag
    check = UpdatePipeline(
        paths, Config(), repo_url=f"file://{bare}", python=sys.executable
    ).check()
    assert not check.unreleased and check.head == check.latest_commit
    assert check.lines() == ["installed v0.1.0, newest release v0.2.0", "update available: v0.2.0"]

    broken = UpdatePipeline(
        paths, Config(), repo_url="file:///nowhere/at/all", python=sys.executable
    )
    failed = broken.check()
    assert failed.error and failed.lines()[0].startswith("check failed: ")
    assert transactions(paths)[-1][1].startswith("failed ")


def checkout_install(paths, repo, commit: str) -> None:
    """A generation installed with ./install.sh --from DIR: its tag carries the commit."""
    layout = Layout(paths)
    gen_id = "0001-v0.2.0+abc123def456"
    shutil.rmtree(layout.gen_dir(gen_id), ignore_errors=True)
    fake_generation(layout.gen_dir(gen_id) / "venv", "0.2.0")
    manifest = Manifest(repo_url=repo)
    manifest.activate(
        Generation(gen_id, "v0.2.0+abc123def456", commit, "0.2.0", "2026.01.01", sys.executable)
    )
    layout.point_current(gen_id)
    manifest.save(paths.manifest_file)


def test_engine_refresh_keeps_a_checkout_build_at_its_own_commit(paths, repo, monkeypatch):
    bare = Path(repo.removeprefix("file://"))
    tip = git(["rev-parse", "main"], cwd=bare).strip()  # one commit after the v0.2.0 release
    assert tip != git(["rev-parse", "v0.2.0^{commit}"], cwd=bare).strip()
    checkout_install(paths, repo, tip)
    pipeline = make_pipeline(paths, repo, monkeypatch)
    built: list[str] = []
    fake_build = UpdatePipeline._build

    def recording_build(self, gen_id, gen_dir, build_dir):
        built.append((build_dir / "pyproject.toml").read_text())
        return fake_build(self, gen_id, gen_dir, build_dir)

    monkeypatch.setattr(UpdatePipeline, "_build", recording_build)
    result = pipeline.run(engine_only=True)
    assert result.generation.id == "0002-v0.2.0+abc123def456"
    assert result.generation.tag == "v0.2.0+abc123def456" and result.generation.commit == tip
    assert 'version = "0.3.0"' in built[0]  # the checkout's own code, not the v0.2.0 release
    assert "the app is unchanged at v0.2.0+abc123def456" in result.message


def test_engine_refresh_refuses_rather_than_replace_a_checkout_build(paths, repo, monkeypatch):
    for commit in ("c" * 40, "unknown"):
        checkout_install(paths, repo, commit)
        pipeline = make_pipeline(paths, repo, monkeypatch)
        with pytest.raises(UpdateError, match="installed from a checkout"):
            pipeline.run(engine_only=True)
        layout = Layout(paths)
        assert layout.current_target() == layout.venv_dir("0001-v0.2.0+abc123def456").resolve()
        assert Manifest.load(paths.manifest_file).current == "0001-v0.2.0+abc123def456"
        assert not list(layout.build_root.glob("src-*"))
    assert transactions(paths)[-1][1].startswith("failed ")


def test_a_broken_report_callback_never_stops_an_update(paths, repo, installed, monkeypatch):
    pipeline = make_pipeline(paths, repo, monkeypatch)

    def closed_screen(line: str) -> None:
        raise RuntimeError("the update screen was closed")

    pipeline.report = closed_screen
    assert pipeline.run(target=Version.parse("0.2.0")).generation.id == "0002-v0.2.0"
    assert "fetching v0.2.0 from" in "\n".join(log_lines(paths))


def test_install_first_start_and_stale_window_are_recorded(paths, repo):
    layout = Layout(paths)
    for n, tag in ((1, "v0.1.0"), (2, "v0.2.0")):
        gen_dir = layout.gen_dir(f"{n:04d}-{tag}")
        fake_generation(gen_dir / "venv", tag[1:])
        finalize_install(
            paths, gen_dir=gen_dir, tag=tag, commit="d" * 40, python=sys.executable, repo_url=repo
        )
    done = transactions(paths)
    assert [begin.split()[:3] for begin, _ in done] == [
        ["install", "v0.1.0", "trigger=install.sh"],
        ["install", "v0.2.0", "trigger=install.sh"],
    ]
    assert "generation 0002-v0.2.0 is now current" in done[1][1] and "gen=0002-v0.2.0" in done[1][1]

    old, new = layout.venv_dir("0001-v0.1.0"), layout.venv_dir("0002-v0.2.0")
    assert stale_generation(paths, prefix=old).id == "0002-v0.2.0"  # a window left open on 0001
    assert stale_generation(paths, prefix=new) is None
    assert stale_generation(paths, prefix=paths.home) is None  # a checkout, not a generation

    note_first_run(paths, prefix=new)
    note_first_run(paths, prefix=new)  # recorded once
    note_first_run(paths, prefix=paths.home)  # not a generation: nothing to record
    notes = [line for line in log_lines(paths) if " NOTE " in line]
    assert len(notes) == 1 and "first start of generation 0002-v0.2.0" in notes[0]
    note_first_run(paths, prefix=old)  # rolled back: that is a first start again
    assert len([line for line in log_lines(paths) if " NOTE " in line]) == 2


def test_a_failed_spotdl_install_is_recorded(paths):
    from vidpull.spotdl import InstallError

    with pytest.raises(InstallError):
        install_spotdl(paths, "/nonexistent/python", trigger="tui")
    begin, end = transactions(paths)[-1]
    assert begin.startswith("spotdl-install trigger=tui") and end.startswith("failed ")
    assert "InstallError" in end
