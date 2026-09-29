"""Finding downloads that were made before. No network, no engine: the job list and files."""

from __future__ import annotations

import pytest

from tests.helpers import MATCHED_URL, sample_song
from vidpull.config import Config
from vidpull.duplicates import (
    DOWNLOADED,
    FILE,
    LISTED,
    REPEATED,
    Duplicate,
    Finder,
    describe,
    fold,
    job_keys,
    link_key,
    name_key,
    settle,
)
from vidpull.models import Job, JobStatus, MediaInfo, Selection, SelectionKind, Song

VIDEO = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
MP3 = Selection(SelectionKind.CONVERT, "bestaudio/best", "MP3 320 kbps", audio_only=True)
BEST = Selection(SelectionKind.PRESET, "best", "Best")


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
        (
            "https://open.spotify.com/track/11dFghVXANMlKmJXsNCbNl?si=x",
            "spotify:track:11dFghVXANMlKmJXsNCbNl",
        ),
        ("spotify:track:11dFghVXANMlKmJXsNCbNl", "spotify:track:11dFghVXANMlKmJXsNCbNl"),
        (
            "https://open.spotify.com/intl-de/album/6dVIqQ8qmQ5GBnJ9shOYGE",
            "spotify:album:6dVIqQ8qmQ5GBnJ9shOYGE",
        ),
        ("https://vimeo.com/76979871/?utm_source=mail&b=2&a=1#t=3", "vimeo.com/76979871?a=1&b=2"),
        ("HTTPS://WWW.Vimeo.com/76979871?a=1&b=2", "vimeo.com/76979871?a=1&b=2"),
        ("https://example.com:8443/v/", "example.com:8443/v"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_one_key_for_every_spelling_of_a_link(link, key):
    assert link_key(link) == key


def test_names_compare_without_case_punctuation_or_file_name_substitutes():
    assert fold("AC/DC - Back In Black: Live?") == fold("ac⧸dc - back in black： live？")
    assert fold("  Diggy   Graves - Dear Dracula, ") == "diggy graves dear dracula"
    song = Song(title="Dear Dracula,", artists=["Diggy Graves"])
    assert name_key(song) == "name:diggy graves dear dracula"
    assert name_key(Song(title="Alone")) == "name:alone"
    assert name_key(Song(title="")) == ""


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
    found = finder(store, paths).media("https://youtu.be/aqz-KE-bpKQ?si=x")
    assert found is not None and found.why == DOWNLOADED and found.job_id == job.id
    assert found.files == (str(paths.home / "dl" / "clip.mp4"),)
    assert found.detail.startswith("downloaded 2026-09-2") and found.detail.endswith("as Best")
    assert found.phase == "skipped: already downloaded" and found.phrase == "already downloaded"
    lines = describe(found)
    assert lines[0] == "Big Buck Bunny"  # the link has no title yet; the download has
    assert lines[1] == found.link == "https://youtu.be/aqz-KE-bpKQ?si=x"
    assert lines[3].strip() == found.files[0]

    (paths.home / "dl" / "clip.mp4").unlink()  # deleted: downloading it again is no duplicate
    assert finder(store, paths).media(VIDEO) is None
    assert finder(store, paths).media("https://www.youtube.com/watch?v=another-one") is None


def test_the_newest_download_with_a_file_is_the_one_named(store, paths):
    old = downloaded(store, paths, "old.mp4")
    new = downloaded(store, paths, "new.mp4", selection=MP3)
    found = finder(store, paths).media(VIDEO)
    assert found.job_id == new.id and found.detail.endswith("as MP3 320 kbps")
    (paths.home / "dl" / "new.mp4").unlink()
    assert finder(store, paths).media(VIDEO).job_id == old.id


def test_what_is_on_the_list_is_found_and_what_is_over_is_not(store, paths):
    queued = store.add(Job(url=VIDEO, selection=BEST, status=JobStatus.QUEUED))
    found = finder(store, paths).media(VIDEO)
    assert found.why == LISTED and found.detail == f"on the list as download {queued.id}"
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
    found = finder(store, paths).song(song)
    assert found.why == LISTED and found.detail == "on the list as song of run G3"
    store.update(staged.id, status=JobStatus.PENDING)  # no match was found for it
    assert finder(store, paths).song(song) is None
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
    assert finder(store, paths).song(song).job_id == job.id
    # the same recording from another album: another link, the same name, the same file
    single = sample_song(url="https://open.spotify.com/track/0000000000000000000000")
    assert finder(store, paths).song(single).job_id == job.id
    # the upload it was matched to, pasted as a link
    assert finder(store, paths).media("https://youtu.be/Jg4bxoyywyA").job_id == job.id
    assert finder(store, paths).song(sample_song(title="Run Away With Me")) is None


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
    assert finder(store, paths).song(song) is None


def test_the_second_coming_of_a_link_in_one_list(store, paths):
    looking = finder(store, paths)
    songs = [sample_song(title="Airbag"), sample_song(title="Lucky"), sample_song(title="Airbag")]
    found = [looking.song(song) for song in songs]
    assert found[0] is None and found[1] is None
    assert found[2].why == REPEATED and found[2].phase == "skipped: twice in this list"
    # a look that is not remembered leaves no trace
    fresh = finder(store, paths)
    assert fresh.song(songs[0], remember=False) is None
    assert fresh.song(songs[0], remember=False) is None
    assert fresh.media(VIDEO, remember=False) is None and fresh.media(VIDEO) is None
    assert fresh.media(VIDEO).why == REPEATED


def test_a_file_of_that_name_in_the_folder(store, paths):
    folder = paths.home / "dl" / "Hevs"
    folder.mkdir(parents=True)
    (folder / "AC⧸DC - Back In Black.mp3").write_bytes(b"x")
    (folder / "AC⧸DC - Thunderstruck.txt").write_bytes(b"x")  # not media
    song = Song(title="Back In Black", artists=["AC/DC"])
    found = finder(store, paths).song(song)
    assert found.why == FILE and found.files == (str(folder / "AC⧸DC - Back In Black.mp3"),)
    assert found.phase == "skipped: the file is already there" and found.job_id is None
    assert finder(store, paths).song(Song(title="Thunderstruck", artists=["AC/DC"])) is None
    # names are only compared when the songs are named the default way
    other = Config()
    other.song_template = "%(album)s/%(track)s.%(ext)s"
    assert finder(store, paths, other).song(song) is None


def test_a_file_with_the_video_id_in_its_name(store, paths):
    folder = paths.home / "dl"
    folder.mkdir(parents=True)
    (folder / "Big Buck Bunny [aqz-KE-bpKQ].mkv").write_bytes(b"x")
    info = MediaInfo(url=VIDEO, id="aqz-KE-bpKQ", title="Big Buck Bunny", webpage_url=VIDEO)
    assert finder(store, paths).media(VIDEO) is None  # before the probe: no id yet
    found = finder(store, paths).media(VIDEO, info)
    assert found.why == FILE and found.title == "Big Buck Bunny"
    assert found.files == (str(folder / "Big Buck Bunny [aqz-KE-bpKQ].mkv"),)
    plain = Config()
    plain.output_template = "%(title)s.%(ext)s"
    assert finder(store, paths, plain).media(VIDEO, info) is None
    whole = MediaInfo(url=VIDEO, id="aqz-KE-bpKQ", title="A list", is_playlist=True)
    assert finder(store, paths).media(VIDEO, whole) is None
    short = MediaInfo(url="https://e/x", id="fake", title="Fake")
    (folder / "Fake [fake].mp4").write_bytes(b"x")
    assert finder(store, paths).media("https://e/x", short) is None  # too short to be an id


def test_the_page_a_link_leads_to_counts_too(store, paths):
    job = downloaded(store, paths)
    info = MediaInfo(url="https://t.co/short", id="aqz-KE-bpKQ", title="Bunny", webpage_url=VIDEO)
    assert finder(store, paths).media("https://t.co/short") is None
    assert finder(store, paths).media("https://t.co/short", info).job_id == job.id


def test_a_missing_folder_is_no_trouble(store, paths):
    looking = Finder(store, paths.home / "nowhere", Config())
    assert looking.song(sample_song()) is None


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
