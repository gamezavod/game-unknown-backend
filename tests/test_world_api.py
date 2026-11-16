import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine
from app.models import WorldSave
from app.world.progress import new_progress
from app.world.schemas import StoredSave
from app.world.state import decode_save, encode_save


def account(client: TestClient, email: str = "world@example.com") -> dict[str, str]:
    data = {"email": email, "password": "a-long-world-test-password"}
    assert client.post("/auth/register", json=data).status_code == 201
    response = client.post("/auth/login", json=data)
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def open_world(
    client: TestClient, headers: dict[str, str], seed: str = "api"
) -> StoredSave:
    response = client.post("/game/session", headers=headers, json={"seed": seed})
    assert response.status_code == 200, response.text
    return StoredSave.model_validate(response.json())


def action(
    client: TestClient,
    headers: dict[str, str],
    stored: StoredSave,
    body: dict[str, object],
) -> httpx.Response:
    response: httpx.Response = client.post(
        "/game/actions",
        headers=headers,
        json={"action": body, "expectedServerSavedAt": stored.serverSavedAt},
    )

    return response


def test_authentication_and_user_isolation(client: TestClient) -> None:
    assert client.get("/game/progress").status_code == 401
    first, second = account(client), account(client, "other@example.com")
    a, b = open_world(client, first, "one"), open_world(client, second, "two")
    assert a != b
    assert (
        decode_save(
            client.get("/game/progress?user_id=other", headers=first).json()["data"]
        ).progress.world.seed
        == "one"
    )
    assert (
        decode_save(
            client.get("/game/progress", headers=second).json()["data"]
        ).progress.world.seed
        == "two"
    )
    assert open_world(client, first, "replacement") == a
    assert (
        client.put(
            "/game/progress",
            headers=first,
            json={"data": b.data, "expectedServerSavedAt": a.serverSavedAt},
        ).status_code
        == 400
    )


def test_landscape_and_revealed_cells_persist(client: TestClient) -> None:
    headers = account(client)
    stored = open_world(client, headers, "landscape")
    forest = next(
        c for c in decode_save(stored.data).progress.world.cells if c.type == "forest"
    )
    response = action(
        client,
        headers,
        stored,
        {"type": "chopForest", "position": {"q": forest.q, "r": forest.r}},
    )
    assert response.status_code == 200
    chopped = StoredSave.model_validate(response.json())
    response = action(
        client, headers, chopped, {"type": "reveal", "position": {"q": 4, "r": 0}}
    )
    assert response.status_code == 200
    saved = decode_save(client.get("/game/progress", headers=headers).json()["data"])
    assert len(saved.progress.world.cells) == 38
    assert all(c.state == "visible" for c in saved.progress.world.cells)
    assert (
        next(c for c in saved.progress.world.cells if c.key == forest.key).type
        == "grass"
    )
    with Session(get_engine()) as db:
        assert db.scalars(select(WorldSave)).one().data == saved.model_dump()


def test_paid_excavation_and_client_clock(client: TestClient) -> None:
    headers = account(client)
    stored = open_world(client, headers)
    saved = decode_save(stored.data)
    data = saved.model_dump()
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
        headers=headers,
        json={
            "data": json.dumps(data),
            "expectedServerSavedAt": stored.serverSavedAt,
        },
    )
    assert response.status_code == 200
    result = StoredSave.model_validate(response.json())
    assert stored.serverSavedAt < result.serverSavedAt < 999999999999999
    loaded = decode_save(client.get("/game/progress", headers=headers).json()["data"])
    assert loaded.progress.activeExcavation is not None
    assert loaded.progress.activeExcavation.model_dump() == p["activeExcavation"]
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
    headers = account(client)
    device2 = {
        "Authorization": "Bearer "
        + client.post(
            "/auth/login",
            json={
                "email": "world@example.com",
                "password": "a-long-world-test-password",
            },
        ).json()["access_token"]
    }
    original = open_world(client, headers)
    first = action(
        client, headers, original, {"type": "reveal", "position": {"q": 4, "r": 0}}
    )
    assert first.status_code == 200
    second = action(
        client, device2, original, {"type": "reveal", "position": {"q": -4, "r": 0}}
    )
    assert second.status_code == 409
    assert second.json()["current"] == first.json()
    assert client.get("/game/progress", headers=device2).json() == first.json()


def test_concurrent_writes_are_atomic(client: TestClient) -> None:
    headers = account(client)
    saved = open_world(client, headers)

    def write(_: int) -> int:
        return action(client, headers, saved, {"type": "completeShift"}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2))) == [200, 409]
    assert (
        decode_save(
            client.get("/game/progress", headers=headers).json()["data"]
        ).progress.completedShifts
        == 1
    )


def test_growth_uses_server_time_and_matures_on_login(client: TestClient) -> None:
    headers = account(client)
    saved = open_world(client, headers)
    grass = next(
        c
        for c in decode_save(saved.data).progress.world.cells
        if c.type == "grass" and not c.building
    )
    response = action(
        client,
        headers,
        saved,
        {
            "type": "plantForest",
            "position": {"q": grass.q, "r": grass.r},
            "now": 99999999999999,
            "growthDurationMs": 1,
        },
    )
    assert response.status_code == 200
    planted = StoredSave.model_validate(response.json())
    p = decode_save(planted.data)
    assert p.progress.saplings[0].plantedAt < 99999999999999
    assert p.progress.saplings[0].growthDurationMs == get_settings().sapling_growth_ms
    assert (
        action(
            client, headers, planted, {"type": "advanceGrowth", "now": 99999999999999}
        ).json()
        == planted.model_dump()
    )
    with Session(get_engine()) as db:
        row = db.scalars(select(WorldSave)).one()
        data = p.model_dump()
        data["progress"]["saplings"][0]["plantedAt"] = 0
        row.data = data
        db.commit()
    restored = open_world(client, headers)
    grown = decode_save(restored.data).progress
    assert not grown.saplings
    assert next(c for c in grown.world.cells if c.key == grass.key).type == "forest"
    assert restored.serverSavedAt > planted.serverSavedAt


def test_corrupt_and_unknown_saves_do_not_overwrite(client: TestClient) -> None:
    headers = account(client)
    saved = open_world(client, headers)
    for raw in ["broken", '{"schemaVersion":999}']:
        assert (
            client.put(
                "/game/progress",
                headers=headers,
                json={"data": raw, "expectedServerSavedAt": saved.serverSavedAt},
            ).status_code
            == 422
        )
    assert client.get("/game/progress", headers=headers).json() == saved.model_dump()
    with Session(get_engine()) as db:
        row = db.scalars(select(WorldSave)).one()
        row.data = {"schemaVersion": 999}
        db.commit()
    assert action(client, headers, saved, {"type": "completeShift"}).status_code == 422
    with Session(get_engine()) as db:
        assert db.scalars(select(WorldSave)).one().data == {"schemaVersion": 999}


def test_import_migration_and_unknown_generator(client: TestClient) -> None:
    headers = account(client)
    p = new_progress("legacy").model_dump()
    p["world"]["generatorVersion"] = 999
    response = client.post(
        "/game/session",
        headers=headers,
        json={"initialSave": json.dumps({"schemaVersion": 1, "savedAt": 100, **p})},
    )
    assert response.status_code == 200
    imported = StoredSave.model_validate(response.json())
    assert decode_save(imported.data).progress.model_dump() == p
    assert (
        action(
            client, headers, imported, {"type": "reveal", "position": {"q": 4, "r": 0}}
        ).status_code
        == 400
    )
    assert client.get("/game/progress", headers=headers).json() == imported.model_dump()


def test_invalid_input_and_save_limit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = account(client)
    saved = open_world(client, headers)
    assert (
        action(
            client, headers, saved, {"type": "reveal", "position": {"q": 4.5, "r": 0}}
        ).status_code
        == 422
    )
    assert (
        action(
            client, headers, saved, {"type": "reveal", "position": {"q": 999, "r": 0}}
        ).status_code
        == 400
    )
    monkeypatch.setattr(get_settings(), "max_save_bytes", 10)
    assert (
        client.put(
            "/game/progress",
            headers=headers,
            json={"data": saved.data, "expectedServerSavedAt": saved.serverSavedAt},
        ).status_code
        == 413
    )


def test_noop_and_removing_opened_cells(client: TestClient) -> None:
    headers = account(client)
    saved = open_world(client, headers)
    assert (
        client.put(
            "/game/progress",
            headers=headers,
            json={"data": saved.data, "expectedServerSavedAt": saved.serverSavedAt},
        ).json()
        == saved.model_dump()
    )
    changed = decode_save(saved.data)
    changed.progress.world.cells.pop()
    assert (
        client.put(
            "/game/progress",
            headers=headers,
            json={
                "data": encode_save(changed),
                "expectedServerSavedAt": saved.serverSavedAt,
            },
        ).status_code
        == 400
    )
