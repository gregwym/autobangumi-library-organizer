from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_engine import env as env

from library_organizer.planner import fingerprint


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
