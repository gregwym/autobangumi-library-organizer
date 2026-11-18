from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_engine import env as env

from library_organizer.planner import fingerprint


@pytest.mark.parametrize("copies", [1, 2])
async def test_manually_corrected_native_name_is_found_by_content(env, monkeypatch, copies):
    e, task, src = env
    renamed = src.with_name("Show (2024) S02E28.mkv")
    src.rename(renamed)
    task["files"][0].update(
        name=renamed.name, source=str(renamed), fingerprint=fingerprint(renamed)
    )
    e.bridge.metadata.return_value = {"title": "Show", "episodes": [28]}
    initial = await e.preview()
    item = initial["items"][0]
    assert item["operation"] == "create"
    work = Path(item["work_root"])
    old = work / "Season 1" / "Show (2024) S01E28.mkv"
    old.parent.mkdir(parents=True)
    old.write_bytes(renamed.read_bytes())
    if copies == 2:
        duplicate = work / "Season 1" / "Another name S01E28.mkv"
        duplicate.write_bytes(renamed.read_bytes())
    original = fingerprint(old)
    p = await e.preview()
    item = p["items"][0]
    assert not e.store.owned() and old.exists()
    if copies == 2:
        assert item["operation"] == "held" and len(item["existing_paths"]) == 2
        await e.apply(p["token"])
        assert old.exists() and duplicate.exists() and not e.store.owned()
    else:
        assert item["operation"] == "adopt"
        assert item["adopt"]["proof"] == "完整 SHA-256 内容一致"
        assert item["existing_paths"] == [str(old)]
        monkeypatch.setattr(
            "library_organizer.engine.publish", lambda *args: pytest.fail("Reuse must not copy")
        )
        await e.apply(p["token"])
        assert fingerprint(Path(item["target"])) == original and not old.exists()
        assert renamed.exists()


async def test_work_lookup_does_not_adopt_another_version_or_search_other_work(env, monkeypatch):
    from library_organizer import legacy

    e, task, src = env
    initial = await e.preview()
    work = Path(initial["items"][0]["work_root"])
    work.mkdir(parents=True)
    other_version = work / "Show S01E01 other group.mkv"
    other_version.write_bytes(b"xyz")  # Same size and episode, different bytes.
    unrelated = Path(e.options.library_root) / "Other work"
    unrelated.mkdir()
    (unrelated / "same.mkv").write_bytes(src.read_bytes())
    (work / "external").symlink_to(unrelated, target_is_directory=True)
    original = legacy.content_hash

    def guarded(path, expected, store):
        assert unrelated not in path.parents
        return original(path, expected, store)

    monkeypatch.setattr(legacy, "content_hash", guarded)
    p = await e.preview()
    assert p["items"][0]["operation"] == "create" and not p["items"][0]["existing_paths"]
    await e.apply(p["token"])
    assert other_version.read_bytes() == b"xyz" and (unrelated / "same.mkv").exists()


async def test_work_lookup_respects_another_entry_ownership(env):
    e, task, src = env
    initial = await e.preview()
    work = Path(initial["items"][0]["work_root"])
    old = work / "Season 1" / "renamed S01E01.mkv"
    old.parent.mkdir(parents=True)
    old.write_bytes(src.read_bytes())
    e.store.claim(
        {"key": "another-entry", "target": str(old), "fingerprint": fingerprint(src)},
        fingerprint(old),
        "existing",
    )
    p = await e.preview(rule_id=1)
    assert p["items"][0]["operation"] == "create"
    await e.apply(p["token"], rule_id=1)
    assert (
        old.exists()
        and next(r for r in e.store.owned() if r["target"] == str(old))["key"] == "another-entry"
    )


async def test_work_lookup_error_holds_instead_of_creating_another_copy(env, monkeypatch):
    from library_organizer import legacy

    e, task, src = env
    initial = await e.preview()
    Path(initial["items"][0]["work_root"]).mkdir(parents=True)

    def denied(*args, **kwargs):
        raise PermissionError("cannot inspect existing work directory")

    monkeypatch.setattr(legacy.os, "walk", denied)
    p = await e.preview()
    assert p["items"][0]["operation"] == "held"
    assert "cannot inspect" in p["items"][0]["explanation"]
    await e.apply(p["token"])
    assert not e.store.owned() and not Path(initial["items"][0]["target"]).exists()


@pytest.mark.parametrize("strategy", ["pn", "advance", "template", "opaque", "none"])
async def test_actual_native_strategy_outputs_reuse_or_hold_without_copy(env, strategy):
    from ab_sdk.rename import RenameInput
    from module.plugins.builtin.rename.rename_plugin import (
        AdvanceRename,
        PnRename,
        compile_template,
    )

    e, task, src = env
    native = RenameInput(
        kind="media",
        media_path=src.name,
        title="Source Show",
        bangumi_name="Official Show (2024)",
        season=1,
        episode=1,
        suffix=".mkv",
        group="Group",
    )
    strategies = {
        "pn": PnRename(),
        "advance": AdvanceRename(),
        "template": compile_template(
            "{{ group }} - {{ title }} S{{ season|pad(2) }}E{{ episode|pad(2) }}"
        ),
        "opaque": compile_template("{{ group }} - part{{ episode }}"),
    }
    name = strategies[strategy].target_name(native) if strategy != "none" else src.name
    renamed = src.with_name(name)
    src.rename(renamed)
    task["files"][0].update(name=name, source=str(renamed), fingerprint=fingerprint(renamed))
    old = Path(e.options.library_root) / name
    old.parent.mkdir()
    old.write_bytes(renamed.read_bytes())
    e.bridge.legacy_layout = AsyncMock(
        return_value={"records": {str(old): [fingerprint(renamed), fingerprint(old)]}}
    )
    p = await e.preview()
    item = p["items"][0]
    if strategy == "opaque":
        assert item["operation"] == "held" and "集" in item["explanation"]
        await e.apply(p["token"])
        assert old.exists() and not e.store.owned()
        # A single-episode release can still supply the missing episode.
        task["name"] = "[Group] Show - 01 [1080p]"
        p = await e.preview()
        assert p["items"][0]["operation"] == "adopt"
    else:
        assert item["operation"] == "adopt"
        original = fingerprint(old)
        await e.apply(p["token"])
        assert fingerprint(Path(item["target"])) == original and not old.exists()
        assert renamed.exists()
