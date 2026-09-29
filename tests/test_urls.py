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


def test_a_link_is_one_word():
    from vidpull.urls import looks_like_url

    for text in (
        "https://youtu.be/A\nhttps://youtu.be/B",
        "https://youtu.be/A\thttps://youtu.be/B",
        "https://youtu.be/A\rB",
        "https://youtu.be/\x00A",
        "https://youtu.be/A\x1b[31m",
        "https://youtu.be/A B",  # a space that does not break is a space
        "\n",
    ):
        assert not looks_like_url(text), repr(text)
    assert looks_like_url("  https://youtu.be/A \n")  # what is around it is not in it
    assert looks_like_url("https://例え.jp/動画")


def test_a_bracket_the_link_opened_is_the_links_own():
    from vidpull.urls import extract_urls, normalize_url

    wiki = "https://en.wikipedia.org/wiki/Hurt_(Nine_Inch_Nails_song)"
    assert normalize_url(wiki) == wiki
    assert normalize_url(f"{wiki}).") == wiki  # the sentence closed its own after it
    assert normalize_url("https://e/a_[b]") == "https://e/a_[b]"
    assert normalize_url("https://e/a)") == "https://e/a"
    assert normalize_url("https://e/a]},") == "https://e/a"
    assert extract_urls(f"(see {wiki})") == [wiki]
    assert extract_urls("(see https://e/a)") == ["https://e/a"]
    assert normalize_url(")") == "https://"  # nothing is left of it, and nothing breaks
