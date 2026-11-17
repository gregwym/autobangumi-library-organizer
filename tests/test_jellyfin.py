from library_organizer.jellyfin import compare


async def test_refresh_and_verify_use_current_jellyfin_authorization(monkeypatch):
    import httpx

    from library_organizer.bridge40 import Bridge40
    from library_organizer.jellyfin import verify
    from library_organizer.models import Options

    paths = []

    def server(request):
        paths.append(request.url.path)
        if request.headers.get("Authorization") != 'MediaBrowser Token="test-key"':
            return httpx.Response(401)
        assert "X-Emby-Token" not in request.headers
        if request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(200, json={"Items": [], "TotalRecordCount": 0})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(server), **kwargs),
    )
    options = Options(jellyfin_url="http://jellyfin.test", jellyfin_api_key="test-key")
    assert "已请求扫描" in await Bridge40(options, None).refresh_jellyfin()
    assert await verify(options, []) == []
    assert paths == ["/Library/Refresh", "/Items"]


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
