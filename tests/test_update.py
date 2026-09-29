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
from vidpull.spotdl import SPOTDL_SPECS
from vidpull.update import (
    BusyError,
    Layout,
    NotInstalledError,
    UpdateError,
    UpdatePipeline,
    finalize_install,
    generation_in_use,
    hold_generation,
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


def checkout_install(paths, repo, commit: str, tag: str = "v0.2.0+abc123def456") -> None:
    """A generation installed with ./install.sh --from DIR: its tag carries the commit."""
    layout = Layout(paths)
    gen_id = f"0001-{tag}"
    shutil.rmtree(layout.gen_dir(gen_id), ignore_errors=True)
    fake_generation(layout.gen_dir(gen_id) / "venv", "0.2.0")
    manifest = Manifest(repo_url=repo)
    manifest.activate(Generation(gen_id, tag, commit, "0.2.0", "2026.01.01", sys.executable))
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


def test_a_window_with_an_old_manifest_cannot_downgrade(paths, repo, installed, monkeypatch):
    window = make_pipeline(paths, repo, monkeypatch)  # opened while 0001-v0.1.0 was current
    terminal = make_pipeline(paths, repo, monkeypatch)  # another process
    assert terminal.run().generation.id == "0002-v0.2.0"

    result = window.run(engine_only=True)  # the old window presses "Update engine"
    assert result.generation.id == "0003-v0.2.0" and result.generation.tag == "v0.2.0"
    manifest = Manifest.load(paths.manifest_file)
    assert manifest.current == "0003-v0.2.0" and manifest.previous == "0002-v0.2.0"
    assert "already up to date" in window.run().message  # and "Update app" rebuilds nothing

    layout = Layout(paths)
    (layout.gen_root / "0009-v9.9.9").mkdir()  # left by a crash; the manifest never heard of it
    assert window.run(engine_only=True).generation.id == "0010-v0.2.0"
    assert (layout.gen_root / "0009-v9.9.9").exists()


def test_a_failed_switch_puts_everything_back(paths, repo, installed, monkeypatch):
    pipeline = make_pipeline(paths, repo, monkeypatch)

    def disk_full(_paths):
        raise OSError("disk full")

    monkeypatch.setattr("vidpull.update.write_launcher", disk_full)
    with pytest.raises(OSError, match="disk full"):
        pipeline.run(target=Version.parse("0.2.0"))
    layout = Layout(paths)
    assert layout.current_target() == layout.venv_dir("0001-v0.1.0").resolve()
    assert (layout.venv_dir("0001-v0.1.0") / "bin" / "vidpull").exists()
    on_disk = Manifest.load(paths.manifest_file)
    assert on_disk.current == "0001-v0.1.0" and on_disk.previous is None
    assert [g.id for g in on_disk.generations] == ["0001-v0.1.0"]
    assert not layout.gen_dir("0002-v0.2.0").exists()
    assert pipeline.manifest.current == "0001-v0.1.0"
    end = transactions(paths)[-1][1]
    assert end.startswith("failed ") and "disk full" in end


def test_a_rollback_holds_until_asked_or_until_a_newer_release(paths, repo, installed, monkeypatch):
    pipeline = make_pipeline(paths, repo, monkeypatch)
    pipeline.run()  # v0.2.0
    lines: list[str] = []
    pipeline.report = lines.append
    pipeline.rollback()
    assert Manifest.load(paths.manifest_file).held == "v0.2.0"
    assert any("v0.2.0 is held back" in line for line in lines)

    check = pipeline.check()
    assert check.candidate is None and str(check.held) == "0.2.0"
    assert check.lines()[1].startswith("v0.2.0 is held back because you rolled back from it")
    assert "already up to date" in pipeline.run().message  # what auto-update would run
    assert Manifest.load(paths.manifest_file).current == "0001-v0.1.0"

    work = Path(repo.removeprefix("file://")).parent / "work"
    (work / "pyproject.toml").write_text('[project]\nname = "vidpull"\nversion = "0.4.0"\n')
    git(["commit", "-q", "-am", "release v0.4.0"], cwd=work)
    git(["tag", "-a", "v0.4.0", "-m", "v0.4.0"], cwd=work)
    git(["push", "-q", "--tags", repo.removeprefix("file://"), "main"], cwd=work)
    check = pipeline.check()
    assert str(check.candidate) == "0.4.0" and check.held is None  # a newer release lifts it

    result = pipeline.run(target=Version.parse("0.2.0"))  # asked for by name
    assert result.generation.tag == "v0.2.0"
    assert Manifest.load(paths.manifest_file).held == ""


def test_a_foreign_launcher_is_left_alone(paths, repo, installed, monkeypatch):
    paths.launcher.write_text("#!/bin/sh\nexec pipx run vidpull\n")
    lines: list[str] = []
    make_pipeline(paths, repo, monkeypatch, lines=lines).run()
    assert "pipx" in paths.launcher.read_text()
    assert any("is not vidpull's launcher" in line for line in lines)


def test_updates_come_from_the_repository_the_copy_was_installed_from(paths, repo, installed):
    pipeline = UpdatePipeline(paths, Config(), python=sys.executable)  # no repository given
    assert pipeline.repo_url == repo
    assert str(pipeline.check().latest) == "0.2.0"


def test_spotdl_is_left_alone_when_it_matches_the_release(paths, repo, installed, monkeypatch):
    site = paths.spotdl_venv / "lib" / "python3.12" / "site-packages"
    for spec in SPOTDL_SPECS:
        name, _, version = spec.partition("==")
        (site / f"{name}-{version}.dist-info").mkdir(parents=True)
    (paths.spotdl_venv / "bin").mkdir(parents=True)
    (paths.spotdl_venv / "bin" / "python").write_text("")

    def must_not_run(*args, **kwargs):
        raise AssertionError("pip must not touch an environment that already matches")

    monkeypatch.setattr("vidpull.update.spotdl_install", must_not_run)
    lines: list[str] = []
    make_pipeline(paths, repo, monkeypatch, lines=lines).run()
    assert any("already matches this release" in line for line in lines)


def test_a_generation_in_use_is_not_pruned(paths, repo, installed, monkeypatch):
    from vidpull import update as update_module

    layout = Layout(paths)
    assert not generation_in_use(layout, "0001-v0.1.0")
    assert hold_generation(paths, prefix=layout.venv_dir("0001-v0.1.0"))  # a window left open
    assert not hold_generation(paths, prefix=paths.home)  # a checkout holds nothing
    assert generation_in_use(layout, "0001-v0.1.0")

    lines: list[str] = []
    pipeline = make_pipeline(paths, repo, monkeypatch, lines=lines)
    pipeline.run()
    pipeline.run(engine_only=True)  # 0001 is neither current nor previous any more
    assert layout.gen_dir("0001-v0.1.0").exists()
    assert any("keeping generation 0001-v0.1.0" in line for line in lines)

    update_module._held.pop().close()  # the window is closed
    assert not generation_in_use(layout, "0001-v0.1.0")
    pipeline.run(engine_only=True)
    assert not layout.gen_dir("0001-v0.1.0").exists()
    assert [g.id for g in Manifest.load(paths.manifest_file).generations] == [
        "0003-v0.2.0",
        "0004-v0.2.0",
    ]


def test_a_checkout_with_uncommitted_changes_cannot_be_rebuilt(paths, repo, monkeypatch):
    tip = git(["rev-parse", "main"], cwd=Path(repo.removeprefix("file://"))).strip()
    checkout_install(paths, repo, tip, tag="v0.2.0+abc123def456.dirty")
    pipeline = make_pipeline(paths, repo, monkeypatch)
    with pytest.raises(UpdateError, match="uncommitted changes"):
        pipeline.run(engine_only=True)
    assert Manifest.load(paths.manifest_file).current == "0001-v0.2.0+abc123def456.dirty"


def test_an_install_does_not_run_beside_an_update(paths, repo):
    layout = Layout(paths)
    gen_dir = layout.gen_dir("0001-v0.1.0")
    fake_generation(gen_dir / "venv", "0.1.0")
    pipeline = UpdatePipeline(paths, Config(), repo_url=repo, python=sys.executable)
    with pipeline._lock(), pytest.raises(BusyError, match="already running"):
        finalize_install(
            paths,
            gen_dir=gen_dir,
            tag="v0.1.0",
            commit="b" * 40,
            python=sys.executable,
            repo_url=repo,
        )
    assert Manifest.load(paths.manifest_file) is None
    assert transactions(paths)[-1][1].startswith("failed ")
