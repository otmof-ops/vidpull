"""The install manifest: which generations exist and which one ``current`` points at.

The manifest is a record for humans and for the update pipeline's bookkeeping. The
uninstall pipeline never trusts it for paths; it recomputes the layout from the
environment instead (see ``vidpull.uninstall``).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vidpull.models import utcnow

MANIFEST_SCHEMA = 1
KEEP_GENERATIONS = 2


@dataclass
class Generation:
    id: str  # "0003-v0.2.0"
    tag: str
    commit: str
    vidpull_version: str
    ytdlp_version: str
    python: str
    created_at: str = field(default_factory=utcnow)

    @property
    def sequence(self) -> int:
        try:
            return int(self.id.split("-", 1)[0])
        except ValueError:
            return 0


@dataclass
class Manifest:
    schema: int = MANIFEST_SCHEMA
    repo_url: str = ""
    installed_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    current: str | None = None
    previous: str | None = None
    generations: list[Generation] = field(default_factory=list)
    launcher: str = ""
    created_download_dir: str = ""

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: Path) -> Manifest | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != MANIFEST_SCHEMA:
            return None
        generations = [
            Generation(**{k: v for k, v in g.items() if k in Generation.__dataclass_fields__})
            for g in data.get("generations", [])
            if isinstance(g, dict)
        ]
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        known["generations"] = generations
        return cls(**known)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.updated_at = utcnow()
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------- queries
    def generation(self, gen_id: str | None) -> Generation | None:
        return next((g for g in self.generations if g.id == gen_id), None)

    @property
    def current_generation(self) -> Generation | None:
        return self.generation(self.current)

    @property
    def previous_generation(self) -> Generation | None:
        return self.generation(self.previous)

    def next_id(self, tag: str) -> str:
        sequence = max((g.sequence for g in self.generations), default=0) + 1
        return f"{sequence:04d}-{tag}"

    # ------------------------------------------------------------ mutation
    def activate(self, gen: Generation) -> None:
        if self.generation(gen.id) is None:
            self.generations.append(gen)
        if self.current and self.current != gen.id:
            self.previous = self.current
        self.current = gen.id

    def swap_previous(self) -> Generation | None:
        """Roll back: make ``previous`` current and vice versa."""
        if not self.previous or self.generation(self.previous) is None:
            return None
        self.current, self.previous = self.previous, self.current
        return self.current_generation

    def prunable(self) -> list[Generation]:
        keep = {self.current, self.previous}
        return [g for g in self.generations if g.id not in keep]

    def forget(self, gen_id: str) -> None:
        self.generations = [g for g in self.generations if g.id != gen_id]
        if self.previous == gen_id:
            self.previous = None
