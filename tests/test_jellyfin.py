from library_organizer.jellyfin import compare


def test_verify_episode_identity_and_alternate_version_path():
    planned = [
        {
            "target": "/library/Shows/Show/e.mkv",
            "identity": {"media_type": "tv", "tmdb_id": 1, "season": 2, "episode": 3},
        }
    ]
    items = [
        {"Id": "series", "Type": "Series", "ProviderIds": {"Tmdb": "1"}},
        {
            "Id": "ep",
            "Type": "Episode",
            "SeriesId": "series",
            "IndexNumber": 3,
            "ParentIndexNumber": 2,
            "Path": "/media/other.mkv",
            "MediaSources": [{"Path": "/media/Shows/Show/e.mkv"}],
        },
    ]
    assert compare(planned, items, "/library", "/media")[0]["state"] == "verified"
    items[0]["ProviderIds"]["Tmdb"] = "99"
    assert compare(planned, items, "/library", "/media")[0]["state"] == "mismatch"
    assert compare(planned, [], "/library", "/media")[0]["state"] == "not_indexed"


def test_movie_wrong_tmdb_rejected():
    planned = [{"target": "/library/m.mkv", "identity": {"media_type": "movie", "tmdb_id": 1}}]
    assert (
        compare(
            planned,
            [{"Path": "/library/m.mkv", "Type": "Movie", "ProviderIds": {"Tmdb": "2"}}],
            "/library",
            "/library",
        )[0]["state"]
        == "mismatch"
    )
