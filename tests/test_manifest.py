from vidpull.manifest import Generation, Manifest


def gen(m: Manifest, tag: str) -> Generation:
    return Generation(m.next_id(tag), tag, "c" * 40, tag[1:], "2026.08.19", "/usr/bin/python3")


def test_activate_swap_prune_roundtrip(tmp_path):
    m = Manifest(repo_url="https://example/repo")
    g1, g2, g3 = (gen(m, "v0.1.0"), None, None)
    m.activate(g1)
    g2 = gen(m, "v0.2.0")
    m.activate(g2)
    g3 = gen(m, "v0.3.0")
    m.activate(g3)
    assert (g1.id, g2.id, g3.id) == ("0001-v0.1.0", "0002-v0.2.0", "0003-v0.3.0")
    assert m.current == g3.id and m.previous == g2.id
    assert [g.id for g in m.prunable()] == [g1.id]
    m.forget(g1.id)
    path = tmp_path / "manifest.json"
    m.save(path)
    loaded = Manifest.load(path)
    assert loaded is not None and loaded.current == g3.id and loaded.previous == g2.id
    assert [g.id for g in loaded.generations] == [g2.id, g3.id]
    assert (
        loaded.swap_previous().id == g2.id and loaded.current == g2.id and loaded.previous == g3.id
    )
    assert loaded.next_id("v0.4.0") == "0004-v0.4.0"


def test_load_rejects_garbage(tmp_path):
    path = tmp_path / "m.json"
    assert Manifest.load(path) is None
    path.write_text("{not json")
    assert Manifest.load(path) is None
    path.write_text('{"schema": 99}')
    assert Manifest.load(path) is None
    assert Manifest().swap_previous() is None
