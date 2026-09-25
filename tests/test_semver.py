import pytest

from vidpull.semver import Version, latest


@pytest.mark.parametrize(
    "text, expected",
    [
        ("v1.2.3", (1, 2, 3, ())),
        ("1.2.3", (1, 2, 3, ())),
        ("v0.10.0-rc.1", (0, 10, 0, ("rc", "1"))),
        ("1.0.0-beta+build.7", (1, 0, 0, ("beta",))),
    ],
)
def test_parse(text, expected):
    v = Version.parse(text)
    assert (v.major, v.minor, v.patch, v.pre) == expected


@pytest.mark.parametrize("text", ["", "v1.2", "1.2.3.4", "release/v1.2.3", "v1.2.3-", "abc"])
def test_parse_rejects(text):
    assert Version.try_parse(text) is None
    with pytest.raises(ValueError):
        Version.parse(text)


def test_ordering():
    order = [
        "0.9.9",
        "0.9.10",
        "0.10.0",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-beta",
        "1.0.0-rc.1",
        "1.0.0-rc.2",
        "1.0.0",
        "1.0.1",
    ]
    parsed = [Version.parse(t) for t in order]
    assert sorted(parsed) == parsed
    assert Version.parse("v1.0.0") == Version.parse("1.0.0")
    assert Version.parse("1.0.0-rc.1") < Version.parse("1.0.0")
    assert Version.parse("1.0.0-rc.2") < Version.parse("1.0.0-rc.10")


def test_str_tag_bump():
    v = Version.parse("v1.2.3-rc.1")
    assert str(v) == "1.2.3-rc.1"
    assert v.tag == "v1.2.3-rc.1"
    assert v.is_prerelease
    assert str(Version.parse("1.2.3").bump("minor")) == "1.3.0"
    assert str(Version.parse("1.2.3").bump("major")) == "2.0.0"
    assert str(Version.parse("1.2.3").bump("patch")) == "1.2.4"
    with pytest.raises(ValueError):
        v.bump("nope")


def test_latest_skips_prereleases_by_default():
    versions = [Version.parse(t) for t in ["1.0.0", "1.1.0-rc.1", "0.9.0"]]
    assert str(latest(versions)) == "1.0.0"
    assert str(latest(versions, include_prerelease=True)) == "1.1.0-rc.1"
    assert latest([]) is None
    assert latest([Version.parse("1.0.0-rc.1")]) is None
