"""Semantic-version parsing and ordering for release tags (``vX.Y.Z[-pre]``).

Only the subset of SemVer 2.0.0 that release tags use is implemented: three
numeric components, an optional dot-separated pre-release, and an ignored build
suffix. Pre-releases sort before the release they precede.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_PATTERN = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _identifier_key(part: str) -> tuple[int, int, str]:
    # Numeric identifiers sort before alphanumeric ones, and numerically among themselves.
    if part.isdigit():
        return (0, int(part), "")
    return (1, 0, part)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _PATTERN.match(text.strip())
        if match is None:
            raise ValueError(f"not a semantic version: {text!r}")
        pre = tuple(match.group("pre").split(".")) if match.group("pre") else ()
        if any(not part for part in pre):
            raise ValueError(f"empty pre-release identifier in {text!r}")
        return cls(int(match["major"]), int(match["minor"]), int(match["patch"]), pre)

    @classmethod
    def try_parse(cls, text: str) -> Version | None:
        try:
            return cls.parse(text)
        except ValueError:
            return None

    @property
    def is_prerelease(self) -> bool:
        return bool(self.pre)

    @property
    def tag(self) -> str:
        return f"v{self}"

    def _key(self) -> tuple:
        # A release outranks any pre-release of the same triple.
        return (
            self.major,
            self.minor,
            self.patch,
            0 if self.pre else 1,
            tuple(_identifier_key(p) for p in self.pre),
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return self._key() < other._key()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{'.'.join(self.pre)}" if self.pre else base

    def bump(self, part: str) -> Version:
        if part == "major":
            return Version(self.major + 1, 0, 0)
        if part == "minor":
            return Version(self.major, self.minor + 1, 0)
        if part == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"unknown bump part: {part!r}")


def latest(versions: list[Version], *, include_prerelease: bool = False) -> Version | None:
    """Return the highest version, ignoring pre-releases unless asked for."""
    candidates = [v for v in versions if include_prerelease or not v.is_prerelease]
    return max(candidates) if candidates else None
