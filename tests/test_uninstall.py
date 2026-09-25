import json
import os
import subprocess
import sys
from pathlib import Path

from vidpull import uninstall
from vidpull.update import launcher_script


def populate(paths):
    for root in paths.roots().values():
        (root / "something").mkdir(parents=True, exist_ok=True)
        (root / "something" / "file").write_text("x")
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.launcher.write_text(launcher_script(paths))
    downloads = paths.home / "Videos" / "vidpull"
    downloads.mkdir(parents=True)
    recorded = [downloads / "a.mp4", downloads / "sub" / "b.mp3"]
    for f in recorded:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("media")
    (downloads / "mine.txt").write_text("not recorded")
    return downloads, [str(f) for f in recorded]


def test_plan_and_execute_in_process(paths):
    downloads, recorded = populate(paths)
    plan = uninstall.plan(
        paths,
        purge_downloads=True,
        download_dir=downloads,
        recorded_files=recorded + ["/etc/passwd", str(paths.home / "elsewhere.mp4")],
    )
    kinds = {(item.label, item.kind) for item in plan.items}
    assert (
        ("data dir", "tree") in kinds
        and ("launcher", "file") in kinds
        and ("download", "file") in kinds
    )
    assert ("download dir (if empty)", "empty-dir") in kinds
    assert sum(1 for i in plan.items if i.label == "download") == 2
    assert all(not i.path.startswith("/etc") for i in plan.items)
    failures = uninstall.execute(plan, report=lambda _line: None)
    assert failures == 0
    for root in paths.roots().values():
        assert not root.exists()
    assert not paths.launcher.exists()
    assert not (downloads / "a.mp4").exists() and (downloads / "mine.txt").exists()
    assert downloads.exists()  # not empty, so kept


def test_plan_skips_config_when_kept_and_refuses_symlinks(paths, tmp_path):
    populate(paths)
    plan = uninstall.plan(paths, keep_config=True)
    assert not any(i.label == "config dir" for i in plan.items)
    assert any("keep-config" in why for _, why in plan.skipped)
    # replace the cache dir with a symlink to somewhere precious
    precious = tmp_path / "precious"
    precious.mkdir()
    import shutil

    shutil.rmtree(paths.cache_dir)
    os.symlink(precious, paths.cache_dir)
    plan = uninstall.plan(paths)
    assert not any(i.label == "cache dir" for i in plan.items)
    assert any(str(paths.cache_dir) == p and "symlink" in why for p, why in plan.skipped)
    assert uninstall.execute(plan, report=lambda _l: None) == 0
    assert precious.exists()


def test_root_problem_rules(paths):
    home = paths.home
    assert uninstall._root_problem(Path("relative/vidpull"), home, home)
    assert uninstall._root_problem(home / ".config" / "notvidpull", home / ".config", home)
    assert uninstall._root_problem(home / "deep" / "x" / "vidpull", home / ".config", home)
    assert uninstall._root_problem(home, home.parent, home)
    ok = home / ".config" / "vidpull"
    ok.mkdir(parents=True, exist_ok=True)
    assert uninstall._root_problem(ok, home / ".config", home) is None


def test_foreign_launcher_is_left_alone(paths):
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    paths.launcher.write_text("#!/bin/sh\necho someone else's script\n")
    plan = uninstall.plan(paths)
    assert not any(i.label == "launcher" for i in plan.items)
    assert any(p == str(paths.launcher) for p, _ in plan.skipped)


def test_purge_refuses_dangerous_download_dirs(paths):
    populate(paths)
    plan = uninstall.plan(
        paths,
        purge_downloads=True,
        download_dir=paths.home,
        recorded_files=[str(paths.home / "x.mp4")],
    )
    assert not any(i.label == "download" for i in plan.items)
    assert any("home or system" in why for _, why in plan.skipped)


def test_exec_script_under_system_python(paths, tmp_path):
    downloads, recorded = populate(paths)
    plan = uninstall.plan(
        paths, purge_downloads=True, download_dir=downloads, recorded_files=recorded
    )
    # smuggle a path outside the roots into the plan file: it must be refused
    plan.items.append(uninstall.RemovalItem(str(tmp_path / "outside"), "tree", "evil"))
    (tmp_path / "outside").mkdir()
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(plan.to_json())
    proc = subprocess.run(
        [
            uninstall.system_python(),
            "-c",
            uninstall.exec_script_source(),
            str(plan_file),
            *plan.roots,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "refused (outside allowed roots)" in proc.stdout
    assert (tmp_path / "outside").exists()
    assert not paths.data_dir.exists() and not paths.launcher.exists()
    assert not plan_file.exists()
    assert json.loads(plan.to_json())["roots"] == plan.roots


def test_system_python_is_outside_prefix():
    python = uninstall.system_python()
    assert Path(python).exists()
    if Path(sys.prefix) != Path(sys.base_prefix):
        assert not Path(python).resolve().is_relative_to(Path(sys.prefix).resolve())


def test_empty_download_dir_is_tidied_without_purge(paths):
    downloads = paths.home / "Videos" / "vidpull"
    downloads.mkdir(parents=True)
    plan = uninstall.plan(paths, download_dir=downloads)
    assert any(i.kind == "empty-dir" and i.path == str(downloads) for i in plan.items)
    (downloads / "keep.mp4").write_text("x")
    plan = uninstall.plan(paths, download_dir=downloads)
    assert not any(i.kind == "empty-dir" for i in plan.items)
    assert uninstall.execute(plan, report=lambda _l: None) == 0
    assert (downloads / "keep.mp4").exists()
