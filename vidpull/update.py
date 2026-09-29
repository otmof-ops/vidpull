"""The update pipeline.

An installed vidpull is a set of *generations* under ``$XDG_DATA_HOME/vidpull/gen``,
each a complete virtualenv built from one release tag, with ``current`` a symlink
to the live one. Updating builds the new generation beside the live one from a
fresh shallow clone of the tag, verifies it with its own interpreter, then swaps
the symlink. Nothing the running process uses is ever modified, and rolling back
is an offline symlink swap to the previous generation.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from vidpull import REPO_URL
from vidpull.config import Config
from vidpull.manifest import Generation, Manifest
from vidpull.paths import Paths
from vidpull.procs import clean_env, run, which
from vidpull.semver import Version
from vidpull.spotdl import SPOTDL_SPECS, SpotdlError
from vidpull.spotdl import install as spotdl_install
from vidpull.updatelog import Transaction, UpdateLog, installed_packages, snapshot
from vidpull.versioning import (
    GitError,
    changelog_section,
    current_version,
    git,
    remote_state,
    update_candidate,
)

log = logging.getLogger(__name__)

Reporter = Callable[[str], None]

MIN_FREE_BYTES = 600 * 1024 * 1024
PIP_TIMEOUT = 900.0
ENGINE_SPEC = "yt-dlp[default,deno]"
ENGINE_SPEC_FALLBACK = "yt-dlp[default]"


class UpdateError(Exception):
    pass


class NotInstalledError(UpdateError):
    pass


class BusyError(UpdateError):
    pass


@dataclass
class UpdateCheck:
    current: Version | None
    latest: Version | None
    candidate: Version | None
    error: str = ""
    unreleased: bool = False  # the default branch has commits after the newest release
    held: Version | None = None  # rolled back from: not offered until a newer release exists
    head: str = ""
    latest_commit: str = ""

    @property
    def available(self) -> bool:
        return self.candidate is not None

    def lines(self) -> list[str]:
        """The check in words, as the screen, the command line and the log show it."""
        if self.error:
            return [f"check failed: {self.error}"]
        current = self.current.tag if self.current else "unknown"
        latest = self.latest.tag if self.latest else "none"
        lines = [f"installed {current}, newest release {latest}"]
        if self.candidate:
            lines.append(f"update available: {self.candidate.tag}")
        elif self.held is not None:
            lines.append(
                f"{self.held.tag} is held back because you rolled back from it; "
                f"`vidpull update --to {self.held.tag}` installs it anyway"
            )
        else:
            lines.append("up to date")
        if self.unreleased:
            lines.append(
                f"note: the repository has changes newer than {latest} that are not in a release "
                f"yet (its main branch is at {self.head[:12]}); they arrive with the next release"
            )
        return lines


@dataclass
class UpdateResult:
    ok: bool
    message: str
    generation: Generation | None = None
    previous: Generation | None = None
    restart_required: bool = False


def install_hint() -> str:
    """How to update a copy that was not installed by install.sh."""
    prefix = Path(sys.prefix)
    if "pipx" in prefix.parts:
        return (
            "this copy was installed with pipx; update it with `pipx upgrade vidpull` "
            f"(or `pipx install --force git+{REPO_URL}@vX.Y.Z` for a specific release)"
        )
    if (Path(__file__).resolve().parent.parent / ".git").exists():
        return (
            "this copy runs from a source checkout; update it with `git pull` and `make dev`, "
            "or install it with ./install.sh to get in-app updates and rollback"
        )
    return (
        f"this copy was installed with pip; update it with `pip install --upgrade git+{REPO_URL}`, "
        "or install it with install.sh to get in-app updates and rollback"
    )


class Layout:
    """Where generations live inside the data dir."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self.gen_root = paths.data_dir / "gen"
        self.build_root = paths.data_dir / "build"
        self.current_link = paths.data_dir / "current"
        self.lock_file = paths.data_dir / "update.lock"

    def next_id(self, manifest: Manifest | None, tag: str) -> str:
        """The next generation id: past every sequence the manifest or the disk has seen."""
        seen = [g.sequence for g in manifest.generations] if manifest else []
        try:
            for entry in self.gen_root.iterdir():
                head = entry.name.split("-", 1)[0]
                if head.isdigit():
                    seen.append(int(head))
        except OSError:
            pass
        return f"{max(seen, default=0) + 1:04d}-{tag}"

    def in_use_file(self, gen_id: str) -> Path:
        return self.gen_dir(gen_id) / ".inuse"

    def gen_dir(self, gen_id: str) -> Path:
        return self.gen_root / gen_id

    def venv_dir(self, gen_id: str) -> Path:
        return self.gen_dir(gen_id) / "venv"

    def current_target(self) -> Path | None:
        try:
            return self.current_link.resolve() if self.current_link.is_symlink() else None
        except OSError:
            return None

    def point_current(self, gen_id: str) -> None:
        """Atomically repoint ``current`` with a relative symlink."""
        relative = Path("gen") / gen_id / "venv"
        tmp = self.current_link.with_name(f".current.{os.getpid()}.tmp")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(relative, tmp)
        os.replace(tmp, self.current_link)


def stale_generation(paths: Paths, prefix: Path | None = None) -> Generation | None:
    """The generation a restart would switch to, when this process runs an older one.

    ``None`` when this process is the live generation, or does not run from one at all.
    """
    layout = Layout(paths)
    try:
        running = Path(prefix or sys.prefix).resolve()
        if not running.is_relative_to(layout.gen_root.resolve()):
            return None
        target = layout.current_target()
    except OSError:
        return None
    if target is None or target == running:
        return None
    manifest = Manifest.load(paths.manifest_file)
    return manifest.current_generation if manifest else None


def base_python() -> str:
    """The interpreter that created the running venv, so new generations match it."""
    cfg = Path(sys.prefix) / "pyvenv.cfg"
    if cfg.exists():
        values: dict[str, str] = {}
        for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        for candidate in (values.get("executable"), values.get("home", "") + "/python3"):
            if candidate and Path(candidate).exists():
                return candidate
    return sys.executable


def launcher_script(paths: Paths) -> str:
    return (
        "#!/bin/sh\n"
        "# vidpull launcher, generated by vidpull. Runs the live generation.\n"
        f'exec "{paths.data_dir}/current/bin/vidpull" "$@"\n'
    )


def write_launcher(paths: Paths) -> Path:
    paths.bin_dir.mkdir(parents=True, exist_ok=True)
    tmp = paths.launcher.with_name(f".{paths.launcher.name}.{os.getpid()}.tmp")
    tmp.write_text(launcher_script(paths), encoding="utf-8")
    tmp.chmod(0o755)
    os.replace(tmp, paths.launcher)
    return paths.launcher


class UpdatePipeline:
    def __init__(
        self,
        paths: Paths,
        config: Config,
        *,
        manifest: Manifest | None = None,
        repo_url: str | None = None,
        report: Reporter | None = None,
        python: str | None = None,
        trigger: str = "cli",
        update_log: UpdateLog | None = None,
    ):
        self.paths = paths
        self.config = config
        self.trigger = trigger  # who asks: cli, tui, startup, auto, install.sh
        self.log = update_log or UpdateLog(paths.update_log)
        self._active = threading.local()
        self.report: Reporter = report or (lambda _line: None)
        self.layout = Layout(paths)
        self.manifest = manifest if manifest is not None else Manifest.load(paths.manifest_file)
        # a copy installed from a fork updates from that fork
        self.repo_url = repo_url or (self.manifest.repo_url if self.manifest else "") or REPO_URL
        self.python = python or base_python()

    # ------------------------------------------------------------ queries
    @property
    def installed(self) -> bool:
        return (
            self.manifest is not None
            and self.manifest.current is not None
            and self.layout.current_link.is_symlink()
        )

    def _reload(self) -> None:
        """Read the manifest again: another window or a terminal may have updated since."""
        fresh = Manifest.load(self.paths.manifest_file)
        if fresh is not None:
            self.manifest = fresh

    def installed_version(self) -> Version | None:
        self._reload()
        if self.manifest and self.manifest.current_generation:
            return Version.try_parse(self.manifest.current_generation.tag)
        return current_version()

    # ------------------------------------------------------------ logging
    def _say(self, line: str) -> None:
        """Report a line to whoever is watching, and record it in the update log."""
        txn = getattr(self._active, "txn", None)
        if txn is not None:
            txn.line(line)
        try:
            self.report(line)
        except Exception:  # a closed screen or a broken callback must never stop an update
            log.debug("update report callback failed", exc_info=True)

    @contextlib.contextmanager
    def _transaction(self, action: str, trigger: str | None) -> Iterator[Transaction]:
        """One logged transaction; lines said inside it on this thread are recorded with it."""
        txn = self.log.begin(action, trigger or self.trigger, **snapshot(self.paths, self.manifest))
        previous = getattr(self._active, "txn", None)
        self._active.txn = txn
        try:
            with txn:
                yield txn
        finally:
            self._active.txn = previous

    def check(
        self, *, include_prerelease: bool | None = None, trigger: str | None = None
    ) -> UpdateCheck:
        with self._transaction("check", trigger) as txn:
            result = self._check(include_prerelease)
            txn.end(not result.error, "; ".join(result.lines()))
            return result

    def _check(self, include_prerelease: bool | None = None) -> UpdateCheck:
        include = (
            self.config.include_prereleases if include_prerelease is None else include_prerelease
        )
        current = self.installed_version()
        try:
            state = remote_state(self.repo_url)
        except GitError as exc:
            return UpdateCheck(current=current, latest=None, candidate=None, error=str(exc))
        newest = max(state.tags) if state.tags else None
        candidate = update_candidate(current, state.tags, include_prerelease=include)
        held = (
            Version.try_parse(self.manifest.held) if self.manifest and self.manifest.held else None
        )
        if candidate is None or held is None or candidate > held:
            held = None
        else:
            candidate = None  # rolled back from it: a newer release lifts the hold
        latest_commit = state.commits.get(newest.tag, "") if newest else ""
        return UpdateCheck(
            current=current,
            latest=newest,
            candidate=candidate,
            held=held,
            unreleased=bool(state.head and latest_commit and state.head != latest_commit),
            head=state.head,
            latest_commit=latest_commit,
        )

    # ---------------------------------------------------------------- run
    def run(
        self,
        *,
        target: Version | None = None,
        engine_only: bool = False,
        dry_run: bool = False,
        busy: Callable[[], bool] | None = None,
        trigger: str | None = None,
    ) -> UpdateResult:
        action = "engine-only" if engine_only else "update"
        with self._transaction(f"{action} (dry run)" if dry_run else action, trigger) as txn:
            result = self._run(target=target, engine_only=engine_only, dry_run=dry_run, busy=busy)
            txn.end(result.ok, result.message, **snapshot(self.paths, self.manifest))
            return result

    def _run(
        self,
        *,
        target: Version | None,
        engine_only: bool,
        dry_run: bool,
        busy: Callable[[], bool] | None,
    ) -> UpdateResult:
        self._reload()
        if not self.installed or self.manifest is None:
            raise NotInstalledError(install_hint())
        if busy is not None and busy():
            raise BusyError("downloads are running; wait for them to finish or cancel them first")
        current_gen = self.manifest.current_generation
        current = Version.try_parse(current_gen.tag) if current_gen else current_version()

        exact_commit = ""
        if engine_only:
            if current is None or current_gen is None:
                raise UpdateError("cannot tell which release is installed")
            target = current
            source = current_gen.tag
            if source.endswith(".dirty"):
                raise UpdateError(
                    f"this copy was installed from a checkout with uncommitted changes ({source}), "
                    "which no update can rebuild. Install the checkout again with "
                    "./install.sh --from DIR, or update the app when a release is available"
                )
            if "+" in source:  # installed from a checkout: rebuild that commit, not the release
                exact_commit = current_gen.commit
            self._say(
                f"refreshing the engines; the app stays at {source} ({current_gen.commit[:12]})"
            )
        elif target is None:
            check = self._check()
            for line in check.lines():
                self._say(line)
            if check.error:
                raise UpdateError(f"cannot list releases: {check.error}")
            if check.candidate is None:
                return UpdateResult(
                    ok=True,
                    message=f"already up to date ({current.tag if current else 'unknown'})",
                    generation=current_gen,
                )
            target = check.candidate
            source = target.tag
            self._say(f"updating {current.tag if current else '?'} -> {target.tag}")
        else:
            if current is not None and target < current:
                self._say(f"note: {target.tag} is older than the installed {current.tag}")
            self._say(f"target release: {target.tag}")
            source = target.tag

        if dry_run:
            gen_id = self.layout.next_id(self.manifest, source)
            return UpdateResult(
                ok=True,
                message=f"dry run: would build generation {gen_id} from {source} "
                f"in {self.layout.gen_dir(gen_id)} and switch current to it",
            )

        self._preflight()
        with self._lock():
            self._reload()  # under the lock: what is installed now is what gets replaced
            if self.manifest is None or self.manifest.current != (
                current_gen.id if current_gen else None
            ):
                now = self.manifest.current if self.manifest else "nothing"
                raise UpdateError(
                    f"the installed copy changed while this update was starting (now {now}); "
                    "check again"
                )
            gen_id = self.layout.next_id(self.manifest, source)
            gen_dir = self.layout.gen_dir(gen_id)
            if gen_dir.exists():
                raise UpdateError(f"{gen_dir} already exists; nothing was changed")
            build_dir = self.layout.build_root / f"src-{gen_id}"
            try:
                if exact_commit:
                    commit = self._clone_commit(exact_commit, source, build_dir)
                else:
                    commit = self._clone(target, build_dir)
                if not engine_only:
                    self._show_changelog(build_dir, target)
                ytdlp_version, vidpull_version = self._build(gen_id, gen_dir, build_dir)
                generation = Generation(
                    id=gen_id,
                    tag=source,
                    commit=commit,
                    vidpull_version=vidpull_version,
                    ytdlp_version=ytdlp_version,
                    python=self.python,
                )
                previous = current_gen
                self._activate(generation)
            except Exception:
                if self.layout.current_target() != (gen_dir / "venv").resolve():
                    self._discard(gen_dir)  # never the directory current points at
                raise
            finally:
                shutil.rmtree(build_dir, ignore_errors=True)
            self._prune()
            self._refresh_spotdl()
        self._say(f"switched current to {gen_id}; restart vidpull to run it")
        if engine_only:
            old = previous.ytdlp_version if previous else "?"
            engines = (
                f"yt-dlp {ytdlp_version} is already the newest"
                if old == ytdlp_version
                else f"yt-dlp {old} -> {ytdlp_version}"
            )
            message = (
                f"engines refreshed ({engines}); the app is unchanged at {source} "
                f"({commit[:12]}), now generation {gen_id}"
            )
        else:
            message = f"installed {target.tag} as generation {gen_id}"
        return UpdateResult(
            ok=True,
            message=message,
            generation=generation,
            previous=previous,
            restart_required=True,
        )

    def rollback(self, *, trigger: str | None = None) -> UpdateResult:
        with self._transaction("rollback", trigger) as txn:
            result = self._rollback()
            txn.end(result.ok, result.message, **snapshot(self.paths, self.manifest))
            return result

    def _rollback(self) -> UpdateResult:
        self._reload()
        if not self.installed or self.manifest is None:
            raise NotInstalledError(f"nothing to roll back: {install_hint()}")
        previous = self.manifest.previous_generation
        if previous is None:
            raise UpdateError("no previous generation to roll back to")
        venv = self.layout.venv_dir(previous.id)
        if not (venv / "bin" / "vidpull").exists():
            raise UpdateError(f"previous generation {previous.id} is missing its venv")
        with self._lock():
            self._verify(venv, Version.try_parse(previous.tag))
            was = self.manifest.current_generation
            self.layout.point_current(previous.id)
            self.manifest.swap_previous()
            left = Version.try_parse(was.tag) if was else None
            back = Version.try_parse(previous.tag)
            if left is not None and back is not None and left > back:
                self.manifest.held = left.tag  # not offered again until a newer release exists
            self.manifest.save(self.paths.manifest_file)
        self._say(f"rolled back to {previous.id}; restart vidpull to run it")
        if self.manifest.held:
            self._say(
                f"{self.manifest.held} is held back: no update installs it again until a newer "
                f"release exists, or you run `vidpull update --to {self.manifest.held}`"
            )
        return UpdateResult(
            ok=True,
            message=f"rolled back to {previous.tag}",
            generation=previous,
            previous=was,
            restart_required=True,
        )

    def restart_argv(self) -> list[str]:
        if self.paths.launcher.exists():
            return [str(self.paths.launcher)]
        return [str(self.layout.current_link / "bin" / "vidpull")]

    # -------------------------------------------------------------- steps
    def _refresh_spotdl(self) -> None:
        """Bring the optional spotDL environment along; a failure there never fails the update."""
        if not (self.paths.spotdl_venv / "bin" / "python").exists():
            return
        names = ("spotdl", "ytmusicapi")
        before = installed_packages(self.paths.spotdl_venv, names)
        pins = dict(spec.split("==", 1) for spec in SPOTDL_SPECS if "==" in spec)
        if pins and all(before.get(name) == version for name, version in pins.items()):
            found = ", ".join(f"{name} {version}" for name, version in pins.items())
            self._say(f"spotDL environment already matches this release ({found}); left as it is")
            return
        self._say("refreshing spotDL")
        try:
            spotdl_install(self.paths, self.python, report=self._say, upgrade=True)
        except SpotdlError as exc:
            self._say(f"spotDL refresh failed ({exc}); Spotify links keep the installed copy")
            return
        after = installed_packages(self.paths.spotdl_venv, names)
        for name in names:
            old, new = before.get(name, "none"), after.get(name, "none")
            self._say(f"{name} {new} (unchanged)" if old == new else f"{name} {old} -> {new}")

    def _preflight(self) -> None:
        if which("git") is None:
            raise UpdateError("git is required to fetch releases")
        self.layout.gen_root.mkdir(parents=True, exist_ok=True)
        self.layout.build_root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.paths.data_dir).free
        if free < MIN_FREE_BYTES:
            raise UpdateError(
                f"only {free // (1024 * 1024)} MB free under {self.paths.data_dir}; "
                f"need {MIN_FREE_BYTES // (1024 * 1024)} MB to build a generation"
            )

    def _lock(self) -> _FileLock:
        return _FileLock(self.layout.lock_file)

    def _clone(self, target: Version, build_dir: Path) -> str:
        shutil.rmtree(build_dir, ignore_errors=True)
        self._say(f"fetching {target.tag} from {self.repo_url}")
        try:
            git(
                [
                    "clone",
                    "--quiet",
                    "--depth",
                    "1",
                    "--branch",
                    target.tag,
                    "--single-branch",
                    self.repo_url,
                    str(build_dir),
                ],
                timeout=600,
            )
            kind = git(["cat-file", "-t", target.tag], cwd=build_dir).strip()
            commit = git(["rev-parse", "HEAD"], cwd=build_dir).strip()
            described = git(["describe", "--tags", "--exact-match"], cwd=build_dir).strip()
        except GitError as exc:
            raise UpdateError(f"fetching {target.tag} failed: {exc}") from exc
        if kind != "tag":
            raise UpdateError(
                f"{target.tag} is not an annotated tag; releases are cut with scripts/release.sh"
            )
        if described != target.tag:
            raise UpdateError(f"checkout is at {described}, not {target.tag}")
        self._say(f"fetched {target.tag} at {commit[:12]}")
        return commit

    def _clone_commit(self, commit: str, source: str, build_dir: Path) -> str:
        """The exact commit a checkout install was built from, or a refusal: never the release."""
        refusal = (
            f"this copy was installed from a checkout ({source}) at a commit the repository does "
            "not have, so its engines cannot be refreshed without replacing the app with a "
            "release. Install the checkout again with ./install.sh --from DIR, or update the "
            "app when a release is available"
        )
        shutil.rmtree(build_dir, ignore_errors=True)
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise UpdateError(refusal)
        self._say(f"fetching commit {commit[:12]} from {self.repo_url}")
        try:
            build_dir.mkdir(parents=True)
            git(["init", "--quiet"], cwd=build_dir)
            git(["remote", "add", "origin", self.repo_url], cwd=build_dir)
            git(["fetch", "--quiet", "--depth", "1", "origin", commit], cwd=build_dir, timeout=600)
            git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=build_dir)
            head = git(["rev-parse", "HEAD"], cwd=build_dir).strip()
        except (GitError, OSError) as exc:
            raise UpdateError(f"{refusal} ({exc})") from exc
        if head != commit:
            raise UpdateError(f"fetched {head[:12]}, not {commit[:12]}")
        self._say(f"fetched {commit[:12]}")
        return commit

    def _show_changelog(self, build_dir: Path, target: Version) -> None:
        changelog = build_dir / "CHANGELOG.md"
        if not changelog.exists():
            return
        section = changelog_section(changelog.read_text(encoding="utf-8", errors="replace"), target)
        if section:
            self._say(f"changes in {target.tag}:")
            for line in section.splitlines():
                self._say(f"  {line}")

    def _build(self, gen_id: str, gen_dir: Path, build_dir: Path) -> tuple[str, str]:
        if gen_dir.exists():
            shutil.rmtree(gen_dir)
        gen_dir.mkdir(parents=True)
        venv = gen_dir / "venv"
        self._say(f"creating generation {gen_id}")
        self._exec([self.python, "-m", "venv", str(venv)], "creating the virtualenv")
        pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "--quiet"]
        self._say("installing vidpull and its dependencies")
        self._exec([*pip, str(build_dir)], "installing vidpull", timeout=PIP_TIMEOUT)
        self._say("updating yt-dlp to the latest release")
        try:
            self._exec([*pip, "--upgrade", ENGINE_SPEC], "installing yt-dlp", timeout=PIP_TIMEOUT)
        except UpdateError as exc:
            self._say(f"deno extra unavailable ({exc}); installing yt-dlp without it")
            self._exec(
                [*pip, "--upgrade", ENGINE_SPEC_FALLBACK], "installing yt-dlp", timeout=PIP_TIMEOUT
            )
        return self._verify(venv, Version.try_parse(gen_id.split("-", 1)[1]))

    def _verify(self, venv: Path, expected: Version | None) -> tuple[str, str]:
        """Prove the generation works with its own interpreter before it can go live."""
        self._say("verifying the new generation")
        out = self._exec([str(venv / "bin" / "vidpull"), "version", "--plain"], "running vidpull")
        vidpull_version = out.strip()
        if expected is not None and Version.try_parse(vidpull_version) != expected:
            raise UpdateError(
                f"the built copy reports version {vidpull_version!r}, expected {expected}"
            )
        out = self._exec(
            [
                str(venv / "bin" / "python"),
                "-c",
                "import vidpull.tui.app, vidpull.cli, yt_dlp.version; "
                "print(yt_dlp.version.__version__)",
            ],
            "importing the package",
        )
        ytdlp_version = out.strip()
        if which("ffmpeg", prefer_bin=venv / "bin") is None:
            self._say("warning: ffmpeg is not on PATH; merging and conversion will fail")
        self._say(f"verified vidpull {vidpull_version} with yt-dlp {ytdlp_version}")
        return ytdlp_version, vidpull_version

    def _activate(self, generation: Generation) -> None:
        """Make ``generation`` current: symlink, manifest and launcher, or none of them."""
        assert self.manifest is not None
        manifest = self.manifest
        before = (manifest.current, manifest.previous, list(manifest.generations), manifest.held)
        manifest.activate(generation)
        held, new = Version.try_parse(manifest.held or ""), Version.try_parse(generation.tag)
        if held is not None and new is not None and new >= held:
            manifest.held = ""  # installed on purpose, or superseded
        try:
            self.layout.point_current(generation.id)
            manifest.save(self.paths.manifest_file)
            self._write_launcher()
        except Exception:
            manifest.current, manifest.previous, manifest.generations, manifest.held = before
            with contextlib.suppress(OSError):
                if before[0]:
                    self.layout.point_current(before[0])
                manifest.save(self.paths.manifest_file)
            raise

    def _write_launcher(self) -> None:
        """Refresh the launcher vidpull installed; a file of someone else's is left alone."""
        assert self.manifest is not None
        if not self.manifest.launcher:
            return  # installed with --no-launcher
        launcher = self.paths.launcher
        try:
            foreign = launcher.exists() and "vidpull launcher" not in launcher.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            foreign = True
        if foreign:
            self._say(f"{launcher} is not vidpull's launcher; left as it is")
            return
        write_launcher(self.paths)

    def _prune(self) -> None:
        assert self.manifest is not None
        running = Path(sys.prefix).resolve()
        for gen in self.manifest.prunable():
            gen_dir = self.layout.gen_dir(gen.id)
            try:
                if gen_dir.exists() and (gen_dir / "venv").resolve() == running:
                    continue  # never delete the generation this process runs from
            except OSError:
                continue
            if generation_in_use(self.layout, gen.id):
                self._say(f"keeping generation {gen.id}: a running vidpull still uses it")
                continue
            self._say(f"pruning old generation {gen.id}")
            self._discard(gen_dir)
            self.manifest.forget(gen.id)
        self.manifest.save(self.paths.manifest_file)
        for leftover in self.layout.build_root.glob("src-*"):
            shutil.rmtree(leftover, ignore_errors=True)

    def _discard(self, gen_dir: Path) -> None:
        try:
            if gen_dir.resolve().is_relative_to(self.layout.gen_root.resolve()):
                shutil.rmtree(gen_dir, ignore_errors=True)
        except OSError:
            pass

    def _exec(self, argv: list[str], what: str, *, timeout: float = 300.0) -> str:
        env = clean_env(prefer_bin=Path(argv[0]).parent)
        try:
            proc = run(argv, timeout=timeout, env=env)
        except FileNotFoundError as exc:
            raise UpdateError(f"{what} failed: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise UpdateError(f"{what} timed out after {timeout:.0f}s") from exc
        if proc.returncode != 0:
            tail = [line for line in proc.stderr.strip().splitlines() if line.strip()][-3:]
            raise UpdateError(f"{what} failed: {' | '.join(tail) or f'exit {proc.returncode}'}")
        return proc.stdout


def finalize_install(
    paths: Paths,
    *,
    gen_dir: Path,
    tag: str,
    commit: str,
    python: str,
    repo_url: str = REPO_URL,
    launcher: bool = True,
    created_download_dir: str = "",
    report: Reporter | None = None,
    trigger: str = "install.sh",
) -> Manifest:
    """Register a generation that install.sh built and make it current (shared with updates)."""
    layout = Layout(paths)
    manifest = Manifest.load(paths.manifest_file) or Manifest(repo_url=repo_url)
    if not manifest.repo_url:
        manifest.repo_url = repo_url
    gen_id = gen_dir.name
    if gen_dir.parent.resolve() != layout.gen_root.resolve():
        raise UpdateError(f"{gen_dir} is not under {layout.gen_root}")
    pipeline = UpdatePipeline(paths, Config(), manifest=manifest, repo_url=repo_url, report=report)
    with pipeline._transaction(f"install {tag}", trigger) as txn, pipeline._lock():
        _finalize(
            pipeline, layout, manifest, gen_dir, tag, commit, python, launcher, created_download_dir
        )
        txn.end(True, f"generation {gen_id} is now current", **snapshot(paths, manifest))
    return manifest


def _finalize(
    pipeline: UpdatePipeline,
    layout: Layout,
    manifest: Manifest,
    gen_dir: Path,
    tag: str,
    commit: str,
    python: str,
    launcher: bool,
    created_download_dir: str,
) -> None:
    paths = pipeline.paths
    gen_id = gen_dir.name
    ytdlp_version, vidpull_version = pipeline._verify(gen_dir / "venv", Version.try_parse(tag))
    generation = Generation(
        id=gen_id,
        tag=tag,
        commit=commit,
        vidpull_version=vidpull_version,
        ytdlp_version=ytdlp_version,
        python=python,
    )
    manifest.activate(generation)
    if launcher:
        manifest.launcher = str(write_launcher(paths))
    if created_download_dir:
        manifest.created_download_dir = created_download_dir
    layout.point_current(gen_id)
    manifest.save(paths.manifest_file)
    pipeline.manifest = manifest
    pipeline._prune()


_held: list[object] = []  # open handles keep this process's generation locked while it runs


def hold_generation(paths: Paths, prefix: Path | None = None) -> bool:
    """Mark the generation this process runs from as in use, until the process ends."""
    layout = Layout(paths)
    try:
        running = Path(prefix or sys.prefix).resolve()
        if not running.is_relative_to(layout.gen_root.resolve()) or running.name != "venv":
            return False
        handle = open(layout.in_use_file(running.parent.name), "a+")  # noqa: SIM115
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return False
    _held.append(handle)
    return True


def generation_in_use(layout: Layout, gen_id: str) -> bool:
    """True while any vidpull process holds ``gen_id`` (see ``hold_generation``)."""
    path = layout.in_use_file(gen_id)
    if not path.exists():
        return False
    try:
        with open(path, "a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False


def install_spotdl(
    paths: Paths, python: str, *, trigger: str, report: Reporter | None = None
) -> str:
    """Install spotDL into its own environment, as a logged transaction; returns its version."""
    say = report or (lambda _line: None)
    with UpdateLog(paths.update_log).begin("spotdl-install", trigger, **snapshot(paths)) as txn:

        def both(line: str) -> None:
            txn.line(line)
            say(line)

        version = spotdl_install(paths, python, report=both)
        txn.end(True, f"spotDL {version} ready", **snapshot(paths))
        return version


def note_first_run(paths: Paths, prefix: Path | None = None) -> None:
    """Record the first start of each generation, once, in the update log."""
    try:
        running = Path(prefix or sys.prefix).resolve()
        gen_root = Layout(paths).gen_root.resolve()
        if not running.is_relative_to(gen_root) or running.name != "venv":
            return
        gen_id = running.parent.name
        marker = paths.state_dir / "last-run-generation"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == gen_id:
            return
        from vidpull import __version__

        fields = snapshot(paths)
        fields.update({"vidpull": __version__, "gen": gen_id})
        UpdateLog(paths.update_log).note(f"first start of generation {gen_id}", **fields)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(gen_id + "\n", encoding="utf-8")
    except OSError as exc:
        log.debug("first-run note not written: %s", exc)


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> _FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise BusyError("another vidpull update is already running") from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
