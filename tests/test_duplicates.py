"""Finding downloads that were made before. No network, no engine: the job list and files."""

from __future__ import annotations

import os

import pytest

from tests.helpers import MATCHED_URL, sample_song
from vidpull.config import Config
from vidpull.duplicates import (
    DOWNLOADED,
    FILE,
    LISTED,
    NAMESAKE,
    REPEATED,
    Duplicate,
    Finder,
    describe,
    fold,
    job_keys,
    kind_of,
    link_key,
    name_key,
    settle,
)
from vidpull.formats import preset_selection
from vidpull.models import Job, JobStatus, MediaInfo, Selection, SelectionKind, Song

VIDEO = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
BEST = Selection(SelectionKind.PRESET, "best", "Best")
MP3 = preset_selection("audio-mp3", Config())
FLAC = preset_selection("audio-flac", Config())
AS_IT_COMES = preset_selection("audio-best", Config())


@pytest.mark.parametrize(
    ("link", "key"),
    [
        (VIDEO, "youtube:aqz-KE-bpKQ"),
        (f"{VIDEO}&t=42s&si=abc", "youtube:aqz-KE-bpKQ"),
        ("https://youtu.be/aqz-KE-bpKQ?si=tracking", "youtube:aqz-KE-bpKQ"),
        ("https://music.youtube.com/watch?v=aqz-KE-bpKQ&list=PL1234567", "youtube:aqz-KE-bpKQ"),
        ("https://m.youtube.com/shorts/aqz-KE-bpKQ", "youtube:aqz-KE-bpKQ"),
        ("youtube.com/embed/aqz-KE-bpKQ", "youtube:aqz-KE-bpKQ"),
        ("https://www.youtube.com/playlist?list=PL1234567", "youtube:list:PL1234567"),
        ("https://www.youtube.com/embed/videoseries?list=PLa", "youtube:list:PLa"),
        (
            "https://www.youtube.com/embed/live_stream?channel=UCx",
            "youtube.com/embed/live_stream?channel=UCx",
        ),
        (
            "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl?si=x",
            "spotify:track:11dFghVXANMlKmJXsNCbNl",
        ),
        ("spotify:track:11dFghVXANMlKmJXsNCbNl", "spotify:track:11dFghVXANMlKmJXsNCbNl"),
        (
            "https://open.spotify.com/intl-de/album/6dVIqQ8qmQ5GBnJ9shOYGE",
            "spotify:album:6dVIqQ8qmQ5GBnJ9shOYGE",
        ),
        (
            "https://vimeo.com/76979871/?utm_source=mail&b=2&a=1&fbclid=x",
            "vimeo.com/76979871?a=1&b=2",
        ),
        ("HTTPS://WWW.Vimeo.com/76979871?a=1&b=2", "vimeo.com/76979871?a=1&b=2"),
        ("https://user:secret@vimeo.com/76979871?b=2&a=1", "vimeo.com/76979871?a=1&b=2"),
        ("https://twitter.com/u/status/1", "x.com/u/status/1"),
        ("https://www.instagram.com/reel/abc/?igsh=xyz", "instagram.com/reel/abc"),
        ("https://example.com:8443/v/", "example.com:8443/v"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_one_key_for_every_spelling_of_a_link(link, key):
    assert link_key(link) == key


@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("https://site.example/watch.php?id=1", "https://site.example/watch.php?id=2"),
        ("https://site.example/player#video=1", "https://site.example/player#video=2"),
        ("https://site.example/view?111", "https://site.example/view?222"),
        ("https://site.example/file?dl", "https://site.example/file?stream"),
        ("https://site.example/watch?si=1", "https://site.example/watch?si=2"),
        ("https://site.example/a?feature=x", "https://site.example/a?feature=y"),
        ("https://site.example/Video", "https://site.example/video"),
        (
            "https://www.youtube.com/embed/videoseries?list=PLa",
            "https://www.youtube.com/embed/videoseries?list=PLb",
        ),
        (
            "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
            "https://www.youtube.com/watch?v=aqz-KE-bpKq",
        ),
        (
            "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl",
            "https://open.spotify.com/album/11dFghVXANMlKmJXsNCbNl",
        ),
    ],
)
def test_links_to_different_media_never_share_a_key(one, other):
    """With the skip setting nobody is asked: a key that two media share loses a download."""
    assert link_key(one) and link_key(one) != link_key(other)


@pytest.mark.parametrize(
    "link", ["http://[::1]:8080/x", "http://[bad", "https://e.com:99999/x", "?", "x" * 5000]
)
def test_no_link_is_too_odd_to_key(link):
    assert isinstance(link_key(link), str)


def test_names_compare_without_case_punctuation_or_file_name_substitutes():
    assert fold("AC/DC - Back In Black: Live?") == fold("ac⧸dc - back in black： live？")
    assert fold("  Diggy   Graves - Dear Dracula, ") == "diggy graves dear dracula"
    song = Song(title="Dear Dracula,", artists=["Diggy Graves"])
    assert name_key(song) == "name:diggy graves dear dracula"
    assert name_key(Song(title="Alone")) == "name:alone"


def test_the_marks_of_a_script_are_part_of_its_words():
    assert fold("เสือ") != fold("เสื้อ")  # tiger, shirt: a tone mark apart
    assert fold("ข่าว") != fold("ข้าว")
    assert len({fold("पार"), fold("पीर"), fold("पूर")}) == 3
    assert fold("Beyoncé") == fold("BEYONCÉ") != fold("Beyonce")


def test_a_title_no_name_can_be_told_by_has_no_name_key():
    for title in ("", "???", "!!!", "\U0001f3b5", "\U0001f3b6"):
        assert name_key(Song(title=title, artists=["X"])) == ""
        assert name_key(Song(title=title)) == ""


def test_what_a_choice_makes_of_a_link():
    assert kind_of(BEST) == "video" and kind_of(None) == ""
    assert kind_of(MP3) == "audio:mp3" and kind_of(FLAC) == "audio:flac"
    assert kind_of(AS_IT_COMES) == "audio"
    assert kind_of(preset_selection("1080p", Config())) == "video"


def downloaded(store, paths, name="clip.mp4", **fields) -> Job:
    target = paths.home / "dl" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"x")
    job = Job(url=VIDEO, selection=BEST, status=JobStatus.DONE, title="Big Buck Bunny")
    job.files = [str(target)]
    job.finished_at = "2026-09-28T07:55:47+00:00"
    for key, value in fields.items():
        setattr(job, key, value)
    return store.add(job)


def finder(store, paths, config=None, **kwargs) -> Finder:
    return Finder(store, paths.home / "dl", config or Config(), **kwargs)


def test_a_link_downloaded_before_is_found_while_its_file_is_there(store, paths):
    job = downloaded(store, paths)
    found = finder(store, paths).media("https://youtu.be/aqz-KE-bpKQ?si=x", None, BEST)
    assert found is not None and found.why == DOWNLOADED and found.job_id == job.id
    assert found.files == (str(paths.home / "dl" / "clip.mp4"),)
    assert found.detail.startswith("downloaded 2026-09-2") and found.detail.endswith("as Best")
    assert found.phase == "skipped: already downloaded" and found.phrase == "already downloaded"
    lines = describe(found)
    assert lines[0] == "Big Buck Bunny"  # the link has no title yet; the download has
    assert lines[1] == found.link == "https://youtu.be/aqz-KE-bpKQ?si=x"
    assert lines[3].strip() == found.files[0]

    (paths.home / "dl" / "clip.mp4").unlink()  # deleted: downloading it again is no duplicate
    assert finder(store, paths).media(VIDEO, None, BEST) is None
    assert finder(store, paths).media("https://www.youtube.com/watch?v=another-one") is None


def test_the_same_link_asked_for_something_else_is_no_duplicate(store, paths):
    downloaded(store, paths, "song.mp3", selection=MP3)
    looking = finder(store, paths)
    assert looking.media(VIDEO, None, MP3, remember=False).why == DOWNLOADED
    assert looking.media(VIDEO, None, BEST, remember=False) is None  # the video is wanted now
    assert looking.media(VIDEO, None, FLAC, remember=False) is None
    assert looking.media(VIDEO, None, AS_IT_COMES, remember=False) is None
    assert looking.media(VIDEO, remember=False).why == DOWNLOADED  # the choice is not known
    # any video is a duplicate of a video: the quality is the user's to weigh
    downloaded(store, paths, "clip.mp4", selection=preset_selection("720p", Config()))
    again = finder(store, paths).media(VIDEO, None, preset_selection("1080p", Config()))
    assert again.why == DOWNLOADED and "720" in again.detail


def test_every_file_of_a_download_has_to_be_there(store, paths):
    job = downloaded(store, paths, "a.mp4")
    second = paths.home / "dl" / "b.mp4"
    second.write_bytes(b"x")
    store.update(job.id, files=[*job.files, str(second)])
    assert finder(store, paths).media(VIDEO, None, BEST).files == (*job.files, str(second))
    second.unlink()  # skipping it would leave this one missing
    assert finder(store, paths).media(VIDEO, None, BEST) is None
    second.write_bytes(b"")  # an empty file is no download either
    assert finder(store, paths).media(VIDEO, None, BEST) is None


def test_the_newest_download_with_its_files_is_the_one_named(store, paths):
    old = downloaded(store, paths, "old.mp4")
    new = downloaded(store, paths, "new.mp4")
    assert finder(store, paths).media(VIDEO, None, BEST).job_id == new.id
    (paths.home / "dl" / "new.mp4").unlink()
    assert finder(store, paths).media(VIDEO, None, BEST).job_id == old.id


def test_a_whole_playlist_is_never_a_duplicate(store, paths):
    whole = "https://www.youtube.com/playlist?list=PL1234567"
    downloaded(store, paths, "one.mp4", url=whole, is_playlist=True)
    downloaded(store, paths, "two.mp4", url=f"{VIDEO}&list=PL1234567", is_playlist=True)
    info = MediaInfo(url=whole, id="PL1234567", title="A list", is_playlist=True)
    assert finder(store, paths).media(whole, info, BEST) is None  # what is in it changes
    assert finder(store, paths).media(whole, None, BEST) is None
    # a video downloaded with its playlist is no evidence for the video alone, nor the reverse
    assert finder(store, paths).media(VIDEO, None, BEST) is None
    downloaded(store, paths, "alone.mp4")
    lists = Selection(SelectionKind.PRESET, "best", "Playlist · Best", playlist=True)
    assert finder(store, paths).media(f"{VIDEO}&list=PL1234567", None, lists) is None
    assert finder(store, paths).media(f"{VIDEO}&list=PL1234567", None, BEST).why == DOWNLOADED


def test_what_is_on_the_list_is_found_and_what_is_over_is_not(store, paths):
    queued = store.add(Job(url=VIDEO, selection=BEST, status=JobStatus.QUEUED))
    found = finder(store, paths).media(VIDEO, None, BEST)
    assert found.why == LISTED and found.detail == f"on the list as download {queued.id}"
    assert finder(store, paths).media(VIDEO, None, MP3) is None  # queued as something else
    assert finder(store, paths, exclude=[queued.id]).media(VIDEO) is None  # the link itself
    for status in (JobStatus.FAILED, JobStatus.CANCELLED):
        store.update(queued.id, status=status)
        assert finder(store, paths).media(VIDEO) is None
    store.update(queued.id, status=JobStatus.DOWNLOADING, cleared_at="2026-09-30T00:00:00+00:00")
    assert finder(store, paths).media(VIDEO) is None  # not on the list any more

    song = sample_song()
    staged = store.add(
        Job(url=song.url, selection=MP3, status=JobStatus.STAGED, song=song, group_id=3)
    )
    found = finder(store, paths).song(song, MP3)
    assert found.why == LISTED and found.detail == "on the list as song of run G3"
    store.update(staged.id, status=JobStatus.PENDING)  # no match was found for it
    assert finder(store, paths).song(song, MP3) is None
    clip = store.add(Job(url="https://vimeo.com/1", selection=BEST, status=JobStatus.PENDING))
    assert finder(store, paths).media("https://vimeo.com/1/").job_id == clip.id  # the clipboard's


def test_a_song_is_known_by_its_link_its_name_and_its_match(store, paths):
    song = sample_song()
    job = downloaded(
        store,
        paths,
        "Carly Rae Jepsen - Cut To The Feeling.mp3",
        url=song.url,
        song=song,
        selection=MP3,
        matched_url=MATCHED_URL,
        title=song.display_title,
    )
    assert set(job_keys(job)) == {
        "spotify:track:11dFghVXANMlKmJXsNCbNl",
        "name:carly rae jepsen cut to the feeling",
        "youtube:Jg4bxoyywyA",
    }
    assert finder(store, paths).song(song, MP3).job_id == job.id
    assert finder(store, paths).song(song, FLAC) is None  # wanted as something else
    # the same recording from another album: another link, the same name, the same file
    single = sample_song(url="https://open.spotify.com/track/0000000000000000000000")
    assert finder(store, paths).song(single, MP3).job_id == job.id
    # the upload it was matched to, pasted as a link and wanted as the same MP3
    assert finder(store, paths).media("https://youtu.be/Jg4bxoyywyA", None, MP3).job_id == job.id
    assert finder(store, paths).media("https://youtu.be/Jg4bxoyywyA", None, BEST) is None
    assert finder(store, paths).song(sample_song(title="Run Away With Me"), MP3) is None


def test_a_duplicate_that_was_skipped_is_no_download(store, paths):
    song = sample_song()
    store.add(
        Job(
            url=song.url,
            selection=MP3,
            status=JobStatus.DONE,
            song=song,
            phase="skipped: already downloaded",
        )
    )
    assert finder(store, paths).song(song, MP3) is None


def test_the_second_coming_of_a_link_in_one_list(store, paths):
    looking = finder(store, paths)
    songs = [sample_song(title="Airbag"), sample_song(title="Lucky"), sample_song(title="Airbag")]
    found = [looking.song(song, MP3) for song in songs]
    assert found[0] is None and found[1] is None
    assert found[2].why == REPEATED and found[2].phase == "skipped: twice in this list"
    # two songs of one name are one file, whatever their links
    other = sample_song(title="Lucky", url="https://open.spotify.com/track/1111111111111111111111")
    namesake = looking.song(other, MP3)
    assert namesake.why == NAMESAKE and "both would be one file" in namesake.detail
    assert namesake.phase == "skipped: same name as another in this list"
    # a look that is not remembered leaves no trace
    fresh = finder(store, paths)
    assert fresh.song(songs[0], MP3, remember=False) is None
    assert fresh.song(songs[0], MP3, remember=False) is None
    assert fresh.media(VIDEO, None, BEST, remember=False) is None
    assert fresh.media(VIDEO, None, BEST) is None
    assert fresh.media(VIDEO, None, MP3) is None  # asked for something else this time
    assert fresh.media(VIDEO, None, BEST).why == REPEATED


def test_a_file_of_that_name_in_the_folder(store, paths):
    folder = paths.home / "dl" / "Hevs"
    folder.mkdir(parents=True)
    (folder / "AC⧸DC - Back In Black.mp3").write_bytes(b"x")
    (folder / "AC⧸DC - Thunderstruck.txt").write_bytes(b"x")  # not media
    song = Song(title="Back In Black", artists=["AC/DC"])
    found = finder(store, paths).song(song, MP3)
    assert found.why == FILE and found.files == (str(folder / "AC⧸DC - Back In Black.mp3"),)
    assert found.phase == "skipped: the file is already there" and found.job_id is None
    assert finder(store, paths).song(song, FLAC) is None  # an MP3 is not the FLAC asked for
    assert finder(store, paths).song(Song(title="Thunderstruck", artists=["AC/DC"]), MP3) is None
    # names are only compared when the songs are named the default way
    other = Config()
    other.song_template = "%(album)s/%(track)s.%(ext)s"
    assert finder(store, paths, other).song(song, MP3) is None


def test_files_that_are_no_finished_download(store, paths):
    folder = paths.home / "dl"
    folder.mkdir(parents=True)
    (folder / "Artist - Empty.mp3").write_bytes(b"")
    (folder / "Artist - Half.f251.mp3").write_bytes(b"x")  # a stream on its way to a merge
    (folder / "Artist - Part.mp3.part").write_bytes(b"x")
    os.symlink(folder / "nowhere.mp3", folder / "Artist - Dangling.mp3")
    (folder / "!!!.mp3").write_bytes(b"x")
    looking = finder(store, paths)
    for title in ("Empty", "Half", "Part", "Dangling"):
        assert looking.song(Song(title=title, artists=["Artist"]), MP3, remember=False) is None
    assert looking.song(Song(title="???"), MP3) is None  # nothing a name can be told by
    (folder / "Artist - Empty.mp3").write_bytes(b"x")
    assert finder(store, paths).song(Song(title="Empty", artists=["Artist"]), MP3).why == FILE


def test_a_file_with_the_video_id_at_the_end_of_its_name(store, paths):
    folder = paths.home / "dl"
    folder.mkdir(parents=True)
    (folder / "Big Buck Bunny [aqz-KE-bpKQ].mkv").write_bytes(b"x")
    (folder / "Song [Official] [dQw4w9WgXcQ].mp4").write_bytes(b"x")
    info = MediaInfo(url=VIDEO, id="aqz-KE-bpKQ", title="Big Buck Bunny", webpage_url=VIDEO)
    assert finder(store, paths).media(VIDEO, None, BEST) is None  # before the probe: no id yet
    found = finder(store, paths).media(VIDEO, info, BEST)
    assert found.why == FILE and found.title == "Big Buck Bunny"
    assert found.files == (str(folder / "Big Buck Bunny [aqz-KE-bpKQ].mkv"),)
    assert finder(store, paths).media(VIDEO, info, MP3) is None  # a video is not the MP3
    # audio as it comes can be a .webm of that very name: the download would replace this one
    (folder / "Other [abcdefghijk].webm").write_bytes(b"x")
    other = MediaInfo(url="https://e/o", id="abcdefghijk", title="Other")
    assert finder(store, paths).media("https://e/o", other, AS_IT_COMES).why == FILE
    assert finder(store, paths).media("https://e/o", other, FLAC) is None
    plain = Config()
    plain.output_template = "%(title)s.%(ext)s"
    assert finder(store, paths, plain).media(VIDEO, info, BEST) is None
    # brackets in the middle of a title are words, not an id
    official = MediaInfo(url="https://e/x", id="Official", title="Other")
    assert finder(store, paths).media("https://e/x", official, BEST) is None
    short = MediaInfo(url="https://e/y", id="fake", title="Fake")
    (folder / "Fake [fake].mp4").write_bytes(b"x")
    assert finder(store, paths).media("https://e/y", short, BEST) is None  # too short for one


def test_the_page_a_link_leads_to_counts_too(store, paths):
    job = downloaded(store, paths)
    info = MediaInfo(url="https://t.co/short", id="aqz-KE-bpKQ", title="Bunny", webpage_url=VIDEO)
    assert finder(store, paths).media("https://t.co/short", None, BEST) is None
    assert finder(store, paths).media("https://t.co/short", info, BEST).job_id == job.id


def test_a_missing_folder_is_no_trouble(store, paths):
    looking = Finder(store, paths.home / "nowhere", Config())
    assert looking.warm().song(sample_song(), MP3) is None
    (paths.home / "a-file").write_bytes(b"x")
    assert Finder(store, paths.home / "a-file", Config()).song(sample_song(), MP3) is None


def test_what_is_shown_of_a_link_holds_no_password(store, paths):
    secret = "https://alice:s3cret@cdn.example/v/1.mp4?token=SIGNED&X-Amz-Signature=abc"
    store.add(Job(url=secret, selection=BEST, status=JobStatus.QUEUED))
    found = finder(store, paths).media(secret, None, BEST)
    shown = "\n".join(describe(found)) + found.title
    assert found.why == LISTED
    assert "s3cret" not in shown and "SIGNED" not in shown and "abc" not in shown
    assert "cdn.example/v/1.mp4" in shown
    assert describe(Duplicate("", DOWNLOADED, "x"))[0] == "(no title)"


def test_what_the_setting_decides_without_asking():
    found = [Duplicate("a", DOWNLOADED, "x"), Duplicate("b", LISTED, "y")]
    assert settle("skip", found) == [False, False]
    assert settle("download", found) == [True, True]
    assert settle("ask", found) is None
    assert settle("ask", []) == []


def test_the_setting_is_kept_valid():
    config = Config()
    assert config.duplicates == "ask"
    config.set("duplicates", "skip")
    assert config.duplicates == "skip"
    config.duplicates = "whatever"
    config.validate()
    assert config.duplicates == "ask"


def test_a_link_is_shown_without_what_is_secret_in_it():
    from vidpull.procs import redact_url

    assert redact_url("https://alice:s3cret@cdn.example/v/1.mp4") == "https://cdn.example/v/1.mp4"
    assert redact_url("https://bob@cdn.example:8443/v?a=1") == "https://cdn.example:8443/v?a=1"
    shown = redact_url("https://u:p@cdn.example/v?token=SIGNED&x=1")
    assert shown == "https://cdn.example/v?token=***&x=1"
    assert redact_url("https://www.youtube.com/watch?v=aqz-KE-bpKQ") == (
        "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    )
    assert redact_url("http://[bad") == "http://[bad"
