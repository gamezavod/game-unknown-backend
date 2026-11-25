import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.models import WorldSave
from app.world.generator import generate_world
from app.world.schemas import StoredSave
from app.world.state import decode_save, encode_save


def player() -> dict[str, str]:
    return {"player_id": str(uuid4())}


def open_world(
    client: TestClient, params: dict[str, str], seed: str = "api"
) -> StoredSave:
    response = client.post("/game/session", params=params, json={"seed": seed})
    assert response.status_code == 200, response.text
    return StoredSave.model_validate_json(response.text)


def action(
    client: TestClient,
    params: dict[str, str],
    stored: StoredSave,
    body: dict[str, object],
) -> httpx.Response:
    response: httpx.Response = client.post(
        "/game/actions",
        params=params,
        json={"action": body, "expectedServerSavedAt": stored.serverSavedAt},
    )

    return response


def test_public_players_keep_separate_progress(client: TestClient) -> None:
    assert client.get("/game/progress").status_code == 404
    first, second = player(), player()
    a, b = open_world(client, first, "one"), open_world(client, second, "two")
    assert a != b
    assert (
        decode_save(
            client.get("/game/progress?user_id=other", params=first).json()["data"]
        ).progress.world.seed
        == generate_world("one").seed
    )
    assert (
        decode_save(
            client.get("/game/progress", params=second).json()["data"]
        ).progress.world.seed
        == generate_world("two").seed
    )
    assert open_world(client, first, "replacement") == a
    assert (
        client.put(
            "/game/progress",
            params=first,
            json={"data": b.data, "expectedServerSavedAt": a.serverSavedAt},
        ).status_code
        == 400
    )


def test_landscape_and_revealed_cells_persist(client: TestClient) -> None:
    params = player()
    stored = open_world(client, params, "landscape")
    forest = next(
        c for c in decode_save(stored.data).progress.world.cells if c.type == "forest"
    )
    response = action(
        client,
        params,
        stored,
        {"type": "chopForest", "position": {"q": forest.q, "r": forest.r}},
    )
    assert response.status_code == 200
    chopped = StoredSave.model_validate_json(response.text)
    preview = chopped.fogPreview[0]
    position = {"q": preview.q, "r": preview.r}
    assert preview.state == "unexplored"
    response = action(client, params, chopped, {"type": "reveal", "position": position})
    assert response.status_code == 200
    restored = StoredSave.model_validate_json(
        client.get("/game/progress", params=params).text
    )
    saved = decode_save(restored.data)
    assert len(saved.progress.world.cells) == 26
    assert {c.key for c in restored.fogPreview}.isdisjoint(
        c.key for c in saved.progress.world.cells
    )
    assert next(
        c for c in saved.progress.world.cells if c.key == preview.key
    ) == preview.model_copy(update={"state": "visible"})
    assert all(c.state == "visible" for c in saved.progress.world.cells)
    assert (
        next(c for c in saved.progress.world.cells if c.key == forest.key).type
        == "grass"
    )
    with Session(get_engine()) as db:
        assert db.scalars(select(WorldSave)).one().data == saved.model_dump(mode="json")


def test_paid_excavation_and_client_clock(client: TestClient) -> None:
    params = player()
    stored = open_world(client, params)
    saved = decode_save(stored.data)
    data = saved.model_dump(mode="json")
    data["savedAt"] = data["serverSavedAt"] = 999999999999999
    p = data["progress"]
    p["excavationSites"] = [{"id": "dig", "q": 0, "r": 0}]
    p["activeExcavation"] = {"siteId": "dig", "depth": 8, "paid": True}
    p["collection"] = [{"id": "item", "siteId": "dig", "foundAt": 100}]
    next(c for c in p["world"]["cells"] if c.get("building") == "townHall")[
        "buildingLevel"
    ] = 3
    response = client.put(
        "/game/progress",
        params=params,
        json={
            "data": json.dumps(data),
            "expectedServerSavedAt": stored.serverSavedAt,
        },
    )
    assert response.status_code == 200
    result = StoredSave.model_validate_json(response.text)
    assert stored.serverSavedAt < result.serverSavedAt < 999999999999999
    loaded = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert loaded.progress.activeExcavation is not None
    assert (
        loaded.progress.activeExcavation.model_dump(mode="json")
        == p["activeExcavation"]
    )
    assert loaded.progress.collection[0].id == "item"
    assert (
        next(
            c for c in loaded.progress.world.cells if c.building == "townHall"
        ).buildingLevel
        == 3
    )


def test_two_devices_choose_server_snapshot_and_do_not_merge(
    client: TestClient,
) -> None:
    params = player()
    original = open_world(client, params)
    with TestClient(client.app) as device2:
        assert device2.get("/game/progress").status_code == 404
        assert device2.get(
            "/game/progress", params=params
        ).json() == original.model_dump(mode="json")
        first_preview, second_preview = original.fogPreview[:2]
        first = action(
            client,
            params,
            original,
            {
                "type": "reveal",
                "position": {"q": first_preview.q, "r": first_preview.r},
            },
        )
        assert first.status_code == 200
        second = action(
            device2,
            params,
            original,
            {
                "type": "reveal",
                "position": {"q": second_preview.q, "r": second_preview.r},
            },
        )
        assert second.status_code == 409
        assert second.json()["current"] == first.json()
        assert device2.get("/game/progress", params=params).json() == first.json()


def test_concurrent_writes_are_atomic(client: TestClient) -> None:
    params = player()
    saved = open_world(client, params)

    def write(_: int) -> int:
        return action(client, params, saved, {"type": "completeShift"}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == [200, 409]
    assert (
        decode_save(
            client.get("/game/progress", params=params).json()["data"]
        ).progress.completedShifts
        == 1
    )


def test_growth_uses_server_time_and_matures_on_login(client: TestClient) -> None:
    params = player()
    saved = open_world(client, params)
    grass = next(
        c
        for c in decode_save(saved.data).progress.world.cells
        if c.type == "grass" and not c.building
    )
    response = action(
        client,
        params,
        saved,
        {
            "type": "plantForest",
            "position": {"q": grass.q, "r": grass.r},
            "now": 99999999999999,
            "growthDurationMs": 1,
        },
    )
    assert response.status_code == 200
    planted = StoredSave.model_validate_json(response.text)
    p = decode_save(planted.data)
    assert p.progress.saplings[0].plantedAt < 99999999999999
    assert p.progress.saplings[0].growthDurationMs == get_settings().sapling_growth_ms
    assert action(
        client, params, planted, {"type": "advanceGrowth", "now": 99999999999999}
    ).json() == planted.model_dump(mode="json")
    with Session(get_engine()) as db:
        row = db.scalars(select(WorldSave)).one()
        data = p.model_dump(mode="json")
        data["progress"]["saplings"][0]["plantedAt"] = 0
        row.data = data
        db.commit()
    restored = open_world(client, params)
    grown = decode_save(restored.data).progress
    assert not grown.saplings
    assert next(c for c in grown.world.cells if c.key == grass.key).type == "forest"
    assert restored.serverSavedAt > planted.serverSavedAt


def test_corrupt_and_unknown_saves_do_not_overwrite(client: TestClient) -> None:
    params = player()
    saved = open_world(client, params)
    for raw in ["broken", '{"schemaVersion":999}']:
        assert (
            client.put(
                "/game/progress",
                params=params,
                json={"data": raw, "expectedServerSavedAt": saved.serverSavedAt},
            ).status_code
            == 422
        )
    assert client.get("/game/progress", params=params).json() == saved.model_dump(
        mode="json"
    )
    with Session(get_engine()) as db:
        row = db.scalars(select(WorldSave)).one()
        row.data = {"schemaVersion": 999}
        db.commit()
    assert action(client, params, saved, {"type": "completeShift"}).status_code == 422
    with Session(get_engine()) as db:
        assert db.scalars(select(WorldSave)).one().data == {"schemaVersion": 999}


@pytest.mark.parametrize("generator_version", [1, 999])
def test_import_migration_and_unsupported_generator(
    client: TestClient, generator_version: int
) -> None:
    params = player()
    p = {
        "world": {
            "seed": "legacy",
            "generatorVersion": generator_version,
            "cells": [
                {"q": 0, "r": 0, "type": "grass", "height": 1.45, "state": "visible"},
                {"q": 0, "r": 1, "type": "water", "height": 0.04, "state": "visible"},
            ],
        },
        "saplings": [],
        "excavationSites": [{"id": "dig:1:0", "q": 1, "r": 0}],
        "activeExcavation": {"siteId": "dig:1:0", "depth": 3, "paid": True},
        "collection": [],
        "completedShifts": 0,
    }
    response = client.post(
        "/game/session",
        params=params,
        json={"initialSave": json.dumps({"schemaVersion": 1, "savedAt": 100, **p})},
    )
    assert response.status_code == 200
    imported = StoredSave.model_validate_json(response.text)
    assert decode_save(imported.data).progress.model_dump(mode="json") == p
    assert imported.fogPreview == []

    reveal = action(
        client, params, imported, {"type": "reveal", "position": {"q": 1, "r": 0}}
    )
    assert reveal.status_code == 400
    assert str(generator_version) in reveal.json()["detail"]
    state = client.get("/game/match3/state", params=params)
    assert state.status_code == 200
    assert state.json()["excavation"]["terrain"] == "fog"
    assert state.json()["excavation"]["depth"] == 3
    dig = next(shift for shift in state.json()["shifts"] if shift["shiftId"] == "dig")
    assert not dig["available"]
    assert [reason["code"] for reason in dig["reasons"]] == ["unsupported_generator"]

    assert client.get("/game/progress", params=params).json() == imported.model_dump(
        mode="json"
    )


def test_invalid_input_and_save_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = player()
    saved = open_world(client, params)
    assert (
        action(
            client, params, saved, {"type": "reveal", "position": {"q": 4.5, "r": 0}}
        ).status_code
        == 422
    )
    assert (
        action(
            client, params, saved, {"type": "reveal", "position": {"q": 999, "r": 0}}
        ).status_code
        == 400
    )
    monkeypatch.setattr(get_settings(), "max_save_bytes", 10)
    assert (
        client.put(
            "/game/progress",
            params=params,
            json={"data": saved.data, "expectedServerSavedAt": saved.serverSavedAt},
        ).status_code
        == 413
    )


def test_noop_and_removing_opened_cells(client: TestClient) -> None:
    params = player()
    saved = open_world(client, params)
    assert client.put(
        "/game/progress",
        params=params,
        json={"data": saved.data, "expectedServerSavedAt": saved.serverSavedAt},
    ).json() == saved.model_dump(mode="json")
    changed = decode_save(saved.data)
    changed.progress.world.cells.pop()
    assert (
        client.put(
            "/game/progress",
            params=params,
            json={
                "data": encode_save(changed),
                "expectedServerSavedAt": saved.serverSavedAt,
            },
        ).status_code
        == 400
    )


def test_cookie_session_works_without_registration_or_jwt(client: TestClient) -> None:
    response = client.post("/game/session", json={"seed": "anonymous"})
    assert response.status_code == 200
    saved = StoredSave.model_validate_json(response.text)
    assert "game_player_id=" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/game/progress").json() == saved.model_dump(mode="json")
    assert (
        client.get(
            "/game/progress", headers={"Authorization": "Bearer invalid"}
        ).status_code
        == 200
    )
    with Session(get_engine()) as db:
        row = db.get(WorldSave, saved.playerId)
        assert row is not None and row.user_id is None
    with TestClient(client.app) as second:
        other = StoredSave.model_validate_json(
            second.post("/game/session", json={}).text
        )
        assert other.playerId != saved.playerId
        assert client.get(
            "/game/progress", params={"player_id": str(other.playerId)}
        ).json() == other.model_dump(mode="json")
    updated = client.post(
        "/game/actions",
        json={
            "action": {"type": "completeShift"},
            "expectedServerSavedAt": saved.serverSavedAt,
        },
    )
    assert updated.status_code == 200
    assert (
        client.put(
            "/game/progress",
            json={
                "data": updated.json()["data"],
                "expectedServerSavedAt": updated.json()["serverSavedAt"],
            },
        ).status_code
        == 200
    )


def test_game_openapi_has_no_security_requirement(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    for path, methods in paths.items():
        if path.startswith("/game/"):
            for operation in methods.values():
                assert not operation.get("security")
    assert paths["/me"]["get"]["security"]
    assert client.get("/me").status_code == 401
    assert (
        client.get("/game/progress", params={"player_id": "invalid"}).status_code == 422
    )


def test_sea_is_neither_revealed_nor_added_to_snapshot(client: TestClient) -> None:
    params = player()
    stored = open_world(client, params)
    saved = decode_save(stored.data)
    shore = next(cell for cell in saved.progress.world.cells if cell.r == 3)
    south = {"q": shore.q, "r": 4}
    reveal = action(client, params, stored, {"type": "reveal", "position": south})
    assert reveal.status_code == 400 and "за пределами острова" in reveal.text
    data = saved.model_dump(mode="json")
    data["progress"]["world"]["cells"].append(
        {**south, "type": "grass", "height": 1.45, "state": "visible"}
    )
    replaced = client.put(
        "/game/progress",
        params=params,
        json={"data": json.dumps(data), "expectedServerSavedAt": stored.serverSavedAt},
    )
    assert replaced.status_code == 400
    assert client.get("/game/progress", params=params).json() == stored.model_dump(
        mode="json"
    )
