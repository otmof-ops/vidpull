from vidpull.urls import extract_urls, looks_like_url, normalize_url


def test_extract_from_messy_text():
    text = "watch https://youtu.be/a?x=1, then (www.vimeo.com/2). again https://youtu.be/a?x=1\n"
    assert extract_urls(text) == ["https://youtu.be/a?x=1", "https://www.vimeo.com/2"]
    assert extract_urls("youtu.be/abc  example.org/v/1") == [
        "https://youtu.be/abc",
        "https://example.org/v/1",
    ]
    assert (
        extract_urls("plain words only") == []
        and extract_urls("") == []
        and extract_urls(None) == []
    )


def test_looks_like_and_normalize():
    assert (
        looks_like_url("https://a.b/c") and looks_like_url("www.a.b") and looks_like_url("a.co/x")
    )
    assert (
        not looks_like_url("get")
        and not looks_like_url("a b.c")
        and not looks_like_url("clip.mp4x")
    )
    assert normalize_url(" youtu.be/x). ") == "https://youtu.be/x"
