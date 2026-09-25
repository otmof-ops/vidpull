import os

import pytest

from vidpull import preflight
from vidpull.config import Config


@pytest.fixture
def fake_tools(monkeypatch, tmp_path):
    """A `which` that only knows the tools in `present`; a runner that records and 'installs'."""
    present: dict[str, str] = {}
    calls: list[list[str]] = []

    def fake_which(name, *, prefer_bin=None):
        return present.get(name)

    def fake_runner(argv):
        calls.append(argv)
        for tool in ("ffmpeg", "ffprobe") if "ffmpeg" in argv else ("deno",):
            present[tool] = f"/fake/{tool}"
        return 0

    monkeypatch.setattr(preflight, "which", fake_which)
    monkeypatch.setattr(preflight, "tool_version", lambda path: (99, 0, 0))
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    return present, calls, fake_runner


def test_missing_steps_with_package_manager(paths, fake_tools):
    present, _, _ = fake_tools
    present["apt-get"] = "/usr/bin/apt-get"
    steps = preflight.missing_steps(paths, "/venv/bin/python")
    assert [s.name for s in steps] == ["ffmpeg", "deno"]
    assert steps[0].argv == ["sudo", "apt-get", "install", "-y", "ffmpeg"] and steps[0].needs_sudo
    assert steps[1].argv == ["/venv/bin/python", "-m", "pip", "install", "--quiet", "yt-dlp[deno]"]
    text = preflight.disclosure(steps)
    assert (
        "sudo apt-get install -y ffmpeg" in text
        and "yt-dlp[deno]" in text
        and "Nothing is downloaded" in text
    )


def test_missing_steps_without_package_manager_uses_static_ffmpeg(paths, fake_tools):
    present, _, _ = fake_tools
    present["deno"] = "/venv/bin/deno"
    steps = preflight.missing_steps(paths, "/venv/bin/python")
    assert [s.name for s in steps] == ["ffmpeg"]
    assert (
        "static-ffmpeg" in steps[0].argv[-1]
        and steps[0].after is not None
        and not steps[0].needs_sudo
    )


def test_nothing_missing(paths, fake_tools):
    present, calls, runner = fake_tools
    present.update(
        {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe", "node": "/usr/bin/node"}
    )
    assert preflight.missing_steps(paths, "/venv/bin/python") == []
    assert (
        preflight.ensure(Config(), paths, "/venv/bin/python", interactive=True, runner=runner)
        is True
    )
    assert calls == []


def test_old_node_does_not_count(paths, fake_tools, monkeypatch):
    present, _, _ = fake_tools
    present.update(
        {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe", "node": "/usr/bin/node"}
    )
    monkeypatch.setattr(preflight, "tool_version", lambda path: (18, 19, 1))
    assert [s.name for s in preflight.missing_steps(paths, "/venv/bin/python")] == ["deno"]


def test_ensure_asks_then_installs(paths, fake_tools):
    present, calls, runner = fake_tools
    present["apt-get"] = "/usr/bin/apt-get"
    lines, prompts = [], []

    def ask(prompt):
        prompts.append(prompt)
        return True

    ok = preflight.ensure(
        Config(),
        paths,
        "/venv/bin/python",
        interactive=True,
        report=lines.append,
        ask=ask,
        runner=runner,
    )
    assert ok and len(prompts) == 1 and len(calls) == 2
    assert any("all set" in line for line in lines)
    assert not (paths.state_dir / "preflight-declined").exists()


def test_ensure_declined_is_remembered(paths, fake_tools):
    present, calls, runner = fake_tools
    present["apt-get"] = "/usr/bin/apt-get"
    asked = []
    ok = preflight.ensure(
        Config(),
        paths,
        "/venv/bin/python",
        interactive=True,
        ask=lambda p: asked.append(p) or False,
        runner=runner,
        report=lambda _l: None,
    )
    assert ok is False and calls == [] and (paths.state_dir / "preflight-declined").exists()
    lines = []
    ok = preflight.ensure(
        Config(),
        paths,
        "/venv/bin/python",
        interactive=True,
        ask=lambda p: asked.append(p) or True,
        runner=runner,
        report=lines.append,
    )
    assert ok is False and len(asked) == 1 and any("doctor --fix" in line for line in lines)
    # --fix (force) asks again despite the marker; --yes installs without asking
    ok = preflight.ensure(
        Config(),
        paths,
        "/venv/bin/python",
        interactive=False,
        assume_yes=True,
        runner=runner,
        report=lambda _l: None,
        force=True,
    )
    assert ok is True and len(calls) == 2


def test_ensure_non_interactive_never_installs(paths, fake_tools):
    present, calls, runner = fake_tools
    present["apt-get"] = "/usr/bin/apt-get"
    lines = []
    ok = preflight.ensure(
        Config(), paths, "/venv/bin/python", interactive=False, runner=runner, report=lines.append
    )
    assert (
        ok is False and calls == [] and any("not running in a terminal" in line for line in lines)
    )


def test_config_can_disable(paths, fake_tools):
    present, calls, runner = fake_tools
    cfg = Config()
    cfg.preflight = False
    assert preflight.ensure(cfg, paths, "/venv/bin/python", interactive=True, runner=runner) is True
    assert calls == []


def test_failed_install_is_reported(paths, fake_tools):
    present, calls, _ = fake_tools
    present["apt-get"] = "/usr/bin/apt-get"
    lines = []
    ok = preflight.ensure(
        Config(),
        paths,
        "/venv/bin/python",
        interactive=True,
        ask=lambda p: True,
        runner=lambda argv: 1,
        report=lines.append,
    )
    assert (
        ok is False
        and any("failed (exit 1)" in line for line in lines)
        and any("still missing" in line for line in lines)
    )


def test_tool_version_and_system_package_command(fake_tools):
    present, _, _ = fake_tools
    assert preflight.system_package_command("ffmpeg") is None
    present["brew"] = "/opt/homebrew/bin/brew"
    argv, sudo = preflight.system_package_command("ffmpeg")
    assert argv == ["brew", "install", "ffmpeg"] and sudo is False


def test_cli_wiring(monkeypatch, capsys, paths):
    from vidpull import cli

    step = preflight.Step("ffmpeg", "why", "how", ["true"])
    monkeypatch.setattr(preflight, "missing_steps", lambda p, py: [step])
    assert cli.main(["--no-preflight", "version", "--plain"]) == 0
    capsys.readouterr()
    from tests.helpers import FakeEngine

    monkeypatch.setattr(cli, "YtDlpEngine", lambda *a, **k: FakeEngine())
    assert (
        cli.main(["get", "https://e/v"]) == 0
    )  # not a tty under pytest: disclosed, skipped, download ran
    out = capsys.readouterr().out
    assert (
        "vidpull needs a few things" in out
        and "not running in a terminal" in out
        and "saved" in out
    )
