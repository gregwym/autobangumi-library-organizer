from pathlib import Path

import pytest

from library_organizer.models import Mapping, Options
from library_organizer.planner import build, contained, episode, fingerprint


@pytest.mark.parametrize(
    "name",
    [
        "[Group] Hibike! Euphonium 3 [NCOP].mkv",
        "SPs/[Group] Hibike! Euphonium 3 [Making01].mkv",
        "[Group] Hibike! Euphonium 3.mkv",
        "Show - 12.5.mkv",
        "Show S01E12.5.mkv",
    ],
)
def test_never_interpret_season_or_decimal_as_episode(name):
    assert episode(name) is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Show - 26v2 [1080p].mkv", 26),
        ("Show [13].mkv", 13),
        ("Show S03E02.mkv", 2),
        ("Show 第4话.mkv", 4),
    ],
)
def test_explicit_episode(name, expected):
    assert episode(name) == expected


def fixture(tmp_path, names):
    src = tmp_path / "downloads"
    src.mkdir()
    lib = tmp_path / "library"
    files = []
    for i, name in enumerate(names):
        p = src / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"content")
        files.append(
            {
                "index": i,
                "name": name,
                "source": str(p),
                "fingerprint": fingerprint(p),
                "progress": 1,
            }
        )
    task = {"key": "a" * 32, "name": "[Group] Show [1080p]", "files": files}
    opts = Options(source_root=str(src), library_root=str(lib))
    return task, opts


def test_all_resources_and_precise_subtitle_pairing(tmp_path):
    task, opts = fixture(
        tmp_path,
        [
            "[Group] Show - 01 [1080p].mkv",
            "[Group] Show - 01 [1080p].chs.ass",
            "SPs/[Group] Show [NCOP].mkv",
            "Fonts/font.ttf",
            "release.nfo",
        ],
    )
    plan = build(
        task, Mapping(tmdb_id=9, season=3), {"title": "Show", "year": "2020", "episodes": [1]}, opts
    )
    assert all(i["target"] for i in plan)
    assert "S03E01" in plan[0]["target"]
    assert plan[1]["target"] == str(Path(plan[0]["target"]).with_suffix("")) + ".zh-Hans.ass"
    assert "/extras/" in plan[2]["target"]
    assert Path(plan[2]["target"]).parent.name == "extras"
    assert "/_resources/" in plan[3]["target"]
    assert "/_resources/" in plan[4]["target"]
    assert all(Path(f["source"]).exists() for f in task["files"])


def test_sp_requires_explicit_tmdb_mapping(tmp_path):
    task, opts = fixture(tmp_path, ["SPs/Show [01].mkv"])
    plan = build(
        task,
        Mapping(tmdb_id=1, season=0, episodes={"SPs/Show [01].mkv": 5}),
        {"title": "Show", "episodes": [5]},
        opts,
    )
    assert "S00E05" in plan[0]["target"]


def test_movie_requires_explicit_main_when_ambiguous(tmp_path):
    task, opts = fixture(tmp_path, ["MovieA.mkv", "MovieB.mkv"])
    metadata = {"title": "Movie", "year": "2020", "episodes": []}
    assert all(
        not i["target"] for i in build(task, Mapping(media_type="movie", tmdb_id=1), metadata, opts)
    )
    plan = build(
        task, Mapping(media_type="movie", tmdb_id=1, main_file="MovieA.mkv"), metadata, opts
    )
    assert Path(plan[0]["target"]).name.startswith(Path(plan[0]["target"]).parent.name)
    assert "/extras/" in plan[1]["target"]


def test_collision_is_held_before_execution(tmp_path):
    task, opts = fixture(tmp_path, ["[A] Show - 01.mkv", "[B] Show - 01.mkv"])
    opts.version_suffix = False
    plan = build(task, Mapping(tmdb_id=1), {"title": "Show", "episodes": [1]}, opts)
    assert all(not p["target"] and "同一目标" in p["reason"] for p in plan)


def test_tmdb_bounds_and_offset(tmp_path):
    task, opts = fixture(tmp_path, ["Show - 26.mkv"])
    assert (
        build(task, Mapping(tmdb_id=1), {"title": "Show", "episodes": [1]}, opts)[0]["target"]
        is None
    )
    assert (
        "S01E01"
        in build(
            task, Mapping(tmdb_id=1, episode_offset=-25), {"title": "Show", "episodes": [1]}, opts
        )[0]["target"]
    )


def test_paths_cannot_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        contained(root, root / "link" / "file")
    with pytest.raises(ValueError):
        contained(root, root / ".." / "file")
    with pytest.raises(ValueError):
        Options(source_root=str(root), library_root=str(root / "lib"))


def test_unknown_subtitle_not_guessed(tmp_path):
    task, opts = fixture(tmp_path, ["Show - 01.mkv", "a.ass"])
    plan = build(task, Mapping(tmdb_id=1), {"title": "Show", "episodes": [1]}, opts)
    assert "/_resources/" in plan[1]["target"]
    assert "无法唯一对应" in plan[1]["notice"]


@pytest.mark.parametrize("name", ["Show S01E01-E02.mkv", "Show S01E01-02.mkv", "Show [01-12].mkv"])
def test_episode_ranges_are_not_collapsed(name):
    assert episode(name) is None


def test_movie_main_override_can_rescue_a_classified_extra(tmp_path):
    task, opts = fixture(tmp_path, ["extras/Film.mkv"])
    plan = build(
        task,
        Mapping(media_type="movie", tmdb_id=1, main_file="extras/Film.mkv"),
        {"title": "Film", "episodes": []},
        opts,
    )
    assert plan[0]["identity"] == {"media_type": "movie", "tmdb_id": 1}


def test_jellyfin_default_version_is_group_only(tmp_path):
    task, opts = fixture(tmp_path, ["[Group] Show - 01v2 [1080p][x265].mkv"])
    p = build(task, Mapping(tmdb_id=1), {"title": "Show", "episodes": [1]}, opts)
    assert Path(p[0]["target"]).name == "Show S01E01 - Group.mkv"


def test_custom_filename_template_and_matching_subtitle(tmp_path):
    task, opts = fixture(tmp_path, ["[Group] Show - 01.mkv", "[Group] Show - 01.chs.ass"])
    opts.episode_template = "{title} ({year}) - S{season:02d}E{episode:02d} [{group}]"
    p = build(task, Mapping(tmdb_id=1), {"title": "Show", "year": "2024", "episodes": [1]}, opts)
    assert Path(p[0]["target"]).name == "Show (2024) - S01E01 [Group].mkv"
    assert Path(p[1]["target"]).name.startswith("Show (2024) - S01E01 [Group].")


@pytest.mark.parametrize(
    "template",
    [
        "../{title}",
        "{title.__class__}",
        "{title[0]}",
        "{episode:999999999d}",
        "{unknown}",
        "{",
        "{title!r}",
    ],
)
def test_filename_template_rejects_unsafe_fields(template):
    with pytest.raises(ValueError):
        Options(episode_template=template)


def test_same_group_versions_are_held_without_hash_names(tmp_path):
    task, opts = fixture(
        tmp_path, ["[Group] Show - 01 [1080p].mkv", "[Group] Show - 01 [720p].mkv"]
    )
    p = build(task, Mapping(tmdb_id=1), {"title": "Show", "episodes": [1]}, opts)
    assert all(not x["target"] and "同一目标" in x["reason"] for x in p)


def test_renamed_single_episode_uses_release_number_once(tmp_path):
    task, opts = fixture(tmp_path, ["Show S02E01.mkv", "Show - 26.chs.ass"])
    task["name"] = "[Group] Show - 26 [1080p]"
    p = build(
        task,
        Mapping(tmdb_id=1, season=2, episode_offset=-25),
        {"title": "Show", "episodes": [1]},
        opts,
    )
    assert "S02E01" in p[0]["target"]
    assert "S02E01" in p[1]["target"]


def test_unknown_legacy_offset_is_held_instead_of_added_twice(tmp_path):
    task, opts = fixture(tmp_path, ["Show S02E01.mkv", "Show S02E02.mkv"])
    p = build(
        task,
        Mapping(tmdb_id=1, season=2, episode_offset=25),
        {"title": "Show", "episodes": [26, 27]},
        opts,
    )
    assert all(not x["target"] and "已应用" in x["reason"] for x in p)
    p = build(
        task,
        Mapping(
            tmdb_id=1,
            season=2,
            episode_offset=25,
            episodes={"Show S02E01.mkv": 26, "Show S02E02.mkv": 27},
        ),
        {"title": "Show", "episodes": [26, 27]},
        opts,
    )
    assert all(x["target"] for x in p)


@pytest.mark.parametrize("subdir", ["", "Shows"])
def test_existing_jellyfin_library_can_omit_classification_folder(tmp_path, subdir):
    task, opts = fixture(tmp_path, ["Show - 01.mkv"])
    opts.tv_subdirectory = subdir
    item = build(
        task, Mapping(tmdb_id=9), {"title": "Show", "year": "2020", "episodes": [1]}, opts
    )[0]
    assert (
        Path(item["target"]).parent.parent
        == Path(opts.library_root) / subdir / "Show (2020) [tmdbid-9]"
    )


@pytest.mark.parametrize("subdir", ["..", "/tmp", "a/b", "a\\b"])
def test_classification_folder_cannot_escape_root(subdir):
    with pytest.raises(ValueError):
        Options(tv_subdirectory=subdir)
