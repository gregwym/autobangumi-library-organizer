import json
import logging

from library_organizer.audit import Audit

pytest_plugins = ["test_engine", "test_rule_api"]


def events(caplog):
    return [
        json.loads(row.message.removeprefix("audit "))
        for row in caplog.records
        if row.message.startswith("audit ")
    ]


def test_periodic_heartbeat_is_quiet_but_state_changes_are_visible(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    clock = [10]
    monkeypatch.setattr("library_organizer.audit.time.monotonic", lambda: clock[0])
    audit = Audit()
    audit.periodic("maintenance.scan", files=1)
    audit.periodic("maintenance.scan", files=1)
    clock[0] += 3600
    audit.periodic("maintenance.scan", files=1)
    audit.periodic("maintenance.scan", files=2)
    assert [row.levelno for row in caplog.records] == [
        logging.INFO,
        logging.DEBUG,
        logging.INFO,
        logging.INFO,
    ]


def test_signed_urls_and_control_characters_do_not_leak(caplog):
    caplog.set_level(logging.INFO)
    Audit().emit(
        "scan.failed",
        detail="https://user:password@example.test/feed?token=secret magnet:?xt=urn:btih:private",
        source="/library/line\nbreak.mkv",
    )
    assert (
        "secret" not in caplog.text and "password" not in caplog.text and "btih" not in caplog.text
    )
    assert events(caplog)[0]["source"] == "/library/line\nbreak.mkv"
    assert "\\n" in caplog.records[0].message


async def test_file_write_logs_share_run_id_and_noop_is_debug(env, caplog):
    caplog.set_level(logging.DEBUG)
    engine, _, source = env
    await engine.apply((await engine.preview())["token"])
    rows = events(caplog)
    start = next(r for r in rows if r["event"] == "organize.apply.start")
    finish = next(r for r in rows if r["event"] == "file.finish")
    assert start["run"] and finish["run"] == start["run"]
    assert finish["source"] == str(source)
    assert finish["action"] == "created" and finish["method"] == "hardlink"
    caplog.clear()
    await engine.apply((await engine.preview())["token"])
    result = next(
        r
        for r in caplog.records
        if json.loads(r.message.removeprefix("audit "))["event"] == "file.finish"
    )
    assert result.levelno == logging.DEBUG


async def test_api_audit_excludes_receipts_and_request_bodies(api, caplog):
    _, _, client = api
    caplog.set_level(logging.INFO)
    async with client:
        response = await client.post("/apply", json={"token": "private-preview-receipt"})
    assert response.status_code in {409, 422}
    assert "private-preview-receipt" not in caplog.text
    assert any(r["event"] == "api.start" for r in events(caplog))
