import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_match3_engine import winning_setup

from app.db import get_engine
from app.match3 import engine
from app.match3.config import RULES
from app.match3.state import CellPosition, Profile, Run, ShiftId
from app.models import Match3ProfileRow, Match3ReceiptRow, Match3RunRow
from app.world.generator import CONFIG, neighbors
from app.world.state import decode_save

PREFIX = "/game/match3"


def player(client: TestClient) -> dict[str, str]:
    params = {"player_id": str(uuid4())}
    response = client.post("/game/session", params=params, json={"seed": "match3"})
    assert response.status_code == 200
    assert client.get(PREFIX + "/state", params=params).status_code == 200
    return params


def start(
    client: TestClient,
    params: dict[str, str],
    shift: ShiftId = "sawmill",
    key: str | None = None,
) -> dict[str, Any]:
    response = client.post(
        PREFIX + "/runs",
        params=params,
        json={"shiftId": shift},
        headers={"Idempotency-Key": key} if key else {},
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


def hint_move(
    client: TestClient,
    params: dict[str, str],
    run: dict[str, Any],
    key: str | None = None,
) -> httpx.Response:
    run_id = str(run["runId"])
    hint = client.get(f"{PREFIX}/runs/{run_id}/hint", params=params).json()
    assert hint["available"]
    response: httpx.Response = client.post(
        f"{PREFIX}/runs/{run_id}/moves",
        params=params,
        json={
            "from": hint["from"],
            "to": hint["to"],
            "expectedRevision": hint["revision"],
        },
        headers={"Idempotency-Key": key} if key else {},
    )
    return response


def patch_run(run_id: object, update: Callable[[Run], None]) -> Run:
    with Session(get_engine()) as db:
        row = db.get(Match3RunRow, UUID(str(run_id)))
        assert row is not None
        run = Run.model_validate_json(json.dumps(row.data))
        update(run)
        row.data = run.model_dump(mode="json")
        db.commit()
        return run


def patch_profile(params: dict[str, str], update: Callable[[Profile], None]) -> Profile:
    with Session(get_engine()) as db:
        row = db.get(Match3ProfileRow, UUID(params["player_id"]))
        assert row is not None
        profile = Profile.model_validate_json(json.dumps(row.data))
        update(profile)
        row.data = profile.model_dump(mode="json")
        db.commit()
        return profile


def assign_workers(count: int) -> Callable[[Profile], None]:
    def apply(profile: Profile) -> None:
        profile.workers["sawmill"] = count

    return apply


def test_match3_routes_are_public(client: TestClient) -> None:
    assert client.get(PREFIX + "/state").status_code == 404
    client.post("/game/session", json={})
    state = client.get(PREFIX + "/state").json()
    assert state["warehouse"] == {
        "wood": {"amount": 120, "limit": 200},
        "food": {"amount": 80, "limit": 200},
        "stone": {"amount": 60, "limit": 200},
        "iron": {"amount": 0, "limit": 200},
    }
    shifts = {shift["shiftId"]: shift for shift in state["shifts"]}
    assert set(shifts) == {"sawmill", "farm", "quarry", "mine", "dig"}
    assert [
        shifts[s]["moves"] for s in ("sawmill", "farm", "quarry", "mine", "dig")
    ] == [24, 22, 22, 17, 20]
    assert {s: shift["moveBreakdown"] for s, shift in shifts.items()} == {
        "sawmill": {"base": 20, "forest": 2, "happiness": 2},
        "farm": {"base": 20, "forest": 0, "happiness": 2},
        "quarry": {"base": 20, "forest": 0, "happiness": 2},
        "mine": {"base": 15, "forest": 0, "happiness": 2},
        "dig": {"base": 20, "forest": 0, "happiness": 0},
    }
    assert all(shift["available"] for shift in shifts.values())
    assert state["activeRun"] is None and state["lastResult"] is None
    paths = client.get("/openapi.json").json()["paths"]
    operations = [
        operation
        for path, methods in paths.items()
        if path.startswith(PREFIX)
        for operation in methods.values()
    ]
    assert len(operations) == 8
    assert all(not operation.get("security") for operation in operations)
    assert client.get("/me").status_code == 401


@pytest.mark.parametrize("shift", ["sawmill", "farm", "quarry", "mine", "dig"])
def test_each_shift_starts_with_correct_board_and_rules(
    client: TestClient, shift: ShiftId
) -> None:
    params = player(client)
    run = start(client, params, shift)
    assert len(run["board"]) == 8
    assert all(len(row) == 8 for row in run["board"])
    assert run["shiftId"] == shift
    assert run["chargesLeft"] == (1 if shift == "sawmill" else 0)
    assert run["status"] == "active" and run["revision"] == 0
    if shift == "dig":
        assert run["amphora"] == {"row": 1, "column": 3}
        saved = decode_save(client.get("/game/progress", params=params).json()["data"])
        assert saved.progress.activeExcavation is not None
        assert saved.progress.activeExcavation.paid
    else:
        assert run["amphora"] is None
    current = client.get(PREFIX + "/state", params=params).json()
    assert current["activeRun"] == run
    assert all(
        any(reason["code"] == "active_run" for reason in option["reasons"])
        for option in current["shifts"]
    )
    assert (
        client.post(
            PREFIX + "/runs", params=params, json={"shiftId": shift}
        ).status_code
        == 409
    )


def test_hint_is_read_only_and_accepted_move_has_animation_steps(
    client: TestClient,
) -> None:
    params = player(client)
    run = start(client, params)
    with Session(get_engine()) as db:
        snapshot = db.get(Match3RunRow, UUID(str(run["runId"])))
        assert snapshot is not None
        before = dict(snapshot.data)
    state = client.get(PREFIX + "/state", params=params).json()
    hint1 = client.get(f"{PREFIX}/runs/{run['runId']}/hint", params=params).json()
    hint2 = client.get(f"{PREFIX}/runs/{run['runId']}/hint", params=params).json()
    assert hint1 == hint2
    assert client.get(PREFIX + "/state", params=params).json() == state
    with Session(get_engine()) as db:
        snapshot = db.get(Match3RunRow, UUID(str(run["runId"])))
        assert snapshot is not None and snapshot.data == before
    response = hint_move(client, params, run)
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved["accepted"] and moved["steps"]
    assert moved["movesLeft"] == int(str(run["movesLeft"])) - 1
    assert moved["steps"][-1]["after"] == moved["board"]
    assert moved["revision"] == 1


def test_invalid_swap_spends_nothing(client: TestClient) -> None:
    params = player(client)
    run = start(client, params)
    response = client.post(
        f"{PREFIX}/runs/{run['runId']}/moves",
        params=params,
        json={"from": {"row": 0, "column": 0}, "to": {"row": 7, "column": 7}},
    )
    assert response.status_code == 200
    rejected = response.json()
    assert not rejected["accepted"] and not rejected["steps"]
    for field in (
        "board",
        "movesLeft",
        "chargesLeft",
        "collected",
        "revision",
        "serverSavedAt",
    ):
        assert rejected[field] == run[field]
    assert (
        client.post(
            f"{PREFIX}/runs/{run['runId']}/moves",
            params=params,
            json={"from": {"row": -1, "column": 0}, "to": {"row": 0, "column": 0}},
        ).status_code
        == 422
    )


def test_lightning_uses_charge_and_finish_is_idempotent(client: TestClient) -> None:
    params = player(client)
    run = start(client, params)
    url = f"{PREFIX}/runs/{run['runId']}"
    response = client.post(
        url + "/lightning", params=params, json={"row": 3, "expectedRevision": 0}
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["cleared"]) == 8
    assert result["movesLeft"] == run["movesLeft"] and result["chargesLeft"] == 0
    assert (
        client.post(url + "/lightning", params=params, json={"row": 3}).status_code
        == 409
    )
    first = client.post(url + "/finish", params=params)
    assert first.status_code == 200
    second = client.post(url + "/finish", params=params)
    assert first.json() == second.json()
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["activeRun"] is None
    assert state["warehouse"]["wood"]["amount"] == 120 + first.json()["credited"]
    assert state["lastResult"]["runId"] == run["runId"]
    assert (
        client.post(
            url + "/moves",
            params=params,
            json={"from": {"row": 0, "column": 0}, "to": {"row": 0, "column": 1}},
        ).status_code
        == 409
    )


def test_goal_bonus_and_capacity_are_credited_once(client: TestClient) -> None:
    params = player(client)
    patch_profile(params, lambda p: p.stock.put("wood", 195))
    run = start(client, params)

    def prepare(value: Run) -> None:
        value.board = winning_setup().board
        value.collected = 29

    patch_run(run["runId"], prepare)
    response = client.post(
        f"{PREFIX}/runs/{run['runId']}/moves",
        params=params,
        json={"from": {"row": 1, "column": 2}, "to": {"row": 0, "column": 2}},
    )
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["status"] == "finished"
    result = completed["result"]
    assert result["goalReached"] and result["bonus"] == 10
    assert result["credited"] == 5 and result["overflow"] == result["total"] - 5
    assert result["warehouse"]["wood"]["amount"] == 200
    finished = client.post(f"{PREFIX}/runs/{run['runId']}/finish", params=params).json()
    assert finished["credited"] == 5
    unavailable = client.post(
        PREFIX + "/runs", params=params, json={"shiftId": "sawmill"}
    )
    assert (
        unavailable.status_code == 409
        and unavailable.json()["code"] == "warehouse_full"
    )


def test_failed_goal_keeps_collected_and_moves_expiry_finishes(
    client: TestClient,
) -> None:
    params = player(client)
    run = start(client, params)
    patch_run(run["runId"], lambda r: setattr(r, "movesLeft", 1))
    response = hint_move(client, params, run)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["reason"] == "moves_exhausted"
    assert result["bonus"] == 0 and result["credited"] == result["collected"]
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["activeRun"] is None
    assert state["warehouse"]["wood"]["amount"] == 120 + result["credited"]


def test_unavailable_workers_energy_and_concept_move_formula(
    client: TestClient,
) -> None:
    params = player(client)
    patch_profile(params, lambda p: p.workers.update({"sawmill": 0}))
    state = client.get(PREFIX + "/state", params=params).json()
    sawmill = next(shift for shift in state["shifts"] if shift["shiftId"] == "sawmill")
    assert not sawmill["available"] and sawmill["reasons"][0]["code"] == "no_workers"
    assert sawmill["moves"] == 0
    assert sawmill["moveBreakdown"] == {"base": 0, "forest": 0, "happiness": 0}
    assert (
        client.post(
            PREFIX + "/runs", params=params, json={"shiftId": "sawmill"}
        ).status_code
        == 409
    )

    def configure(p: Profile) -> None:
        p.workers["sawmill"] = 1
        p.happinessMoves = p.forestMoves = 0
        p.energy["sawmill"] = False

    patch_profile(params, configure)
    for workers, expected in [(1, 15), (2, 20), (3, 25)]:
        patch_profile(params, assign_workers(workers))
        state = client.get(PREFIX + "/state", params=params).json()
        sawmill = next(s for s in state["shifts"] if s["shiftId"] == "sawmill")
        assert sawmill["moveBreakdown"] == {
            "base": expected,
            "forest": 0,
            "happiness": 0,
        }
        run = start(client, params)
        assert run["movesLeft"] == sawmill["moves"] == expected
        assert run["chargesLeft"] == 0
        denied = client.post(
            f"{PREFIX}/runs/{run['runId']}/lightning", params=params, json={"row": 0}
        )
        assert denied.status_code == 409 and denied.json()["code"] == "no_energy"
        assert (
            client.post(
                f"{PREFIX}/runs/{run['runId']}/finish", params=params
            ).status_code
            == 200
        )


def test_active_run_survives_new_client_and_foreign_run_is_hidden(
    client: TestClient,
) -> None:
    params = player(client)
    run = start(client, params)
    moved = hint_move(client, params, run)
    assert moved.status_code == 200
    with TestClient(client.app) as reopened:
        current = reopened.get(PREFIX + "/state", params=params).json()["activeRun"]
        for field in ("board", "movesLeft", "collected", "chargesLeft", "revision"):
            assert current[field] == moved.json()[field]
        assert (
            reopened.get(
                f"{PREFIX}/runs/{run['runId']}/hint", params=params
            ).status_code
            == 200
        )
    other = player(client)
    url = f"{PREFIX}/runs/{run['runId']}"
    assert client.get(url + "/hint", params=other).status_code == 404
    assert client.post(url + "/finish", params=other).status_code == 404
    assert (
        client.post(url + "/lightning", params=other, json={"row": 0}).status_code
        == 404
    )


def test_idempotency_replays_start_move_and_finish_without_extra_credit(
    client: TestClient,
) -> None:
    params = player(client)
    run = start(client, params, key="start-1")
    assert start(client, params, key="start-1") == run
    conflict = client.post(
        PREFIX + "/runs",
        params=params,
        headers={"Idempotency-Key": "start-1"},
        json={"shiftId": "farm"},
    )
    assert (
        conflict.status_code == 409
        and conflict.json()["code"] == "idempotency_conflict"
    )
    hint = client.get(f"{PREFIX}/runs/{run['runId']}/hint", params=params).json()
    body = {
        "from": hint["from"],
        "to": hint["to"],
        "expectedRevision": hint["revision"],
    }
    url = f"{PREFIX}/runs/{run['runId']}"
    first = client.post(
        url + "/moves", params=params, headers={"Idempotency-Key": "move-1"}, json=body
    )
    assert first.status_code == 200
    second = client.post(
        url + "/moves", params=params, headers={"Idempotency-Key": "move-1"}, json=body
    )
    assert first.json() == second.json()
    assert client.post(url + "/moves", params=params, json=body).status_code == 409
    end = client.post(
        url + "/finish", params=params, headers={"Idempotency-Key": "finish-1"}
    )
    assert end.status_code == 200
    assert (
        client.post(
            url + "/finish", params=params, headers={"Idempotency-Key": "finish-1"}
        ).json()
        == end.json()
    )
    with Session(get_engine()) as db:
        assert len(db.scalars(select(Match3ReceiptRow)).all()) == 3
    # Обычный повтор finish также идемпотентен, даже после другой смены.
    another = start(client, params, "farm")
    assert (
        client.post(
            f"{PREFIX}/runs/{another['runId']}/finish", params=params
        ).status_code
        == 200
    )
    assert client.post(url + "/finish", params=params).json() == end.json()
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["lastResult"]["runId"] == another["runId"]


def test_concurrent_start_and_finish_are_serialized(client: TestClient) -> None:
    params = player(client)

    def begin(_: int) -> httpx.Response:
        response: httpx.Response = client.post(
            PREFIX + "/runs", params=params, json={"shiftId": "sawmill"}
        )
        return response

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(begin, range(2)))
    assert sorted(response.status_code for response in responses) == [201, 409]
    run = next(response.json() for response in responses if response.status_code == 201)
    patch_run(run["runId"], lambda value: setattr(value, "collected", 7))

    def finish(_: int) -> httpx.Response:
        response: httpx.Response = client.post(
            f"{PREFIX}/runs/{run['runId']}/finish", params=params
        )
        return response

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, range(2)))
    assert all(response.status_code == 200 for response in results)
    assert results[0].json() == results[1].json()
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["warehouse"]["wood"]["amount"] == 127
    saved = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert saved.progress.completedShifts == 1


def test_concurrent_moves_reject_stale_board_revision(client: TestClient) -> None:
    params = player(client)
    run = start(client, params)
    hint = client.get(f"{PREFIX}/runs/{run['runId']}/hint", params=params).json()

    def send(_: int) -> int:
        return int(
            client.post(
                f"{PREFIX}/runs/{run['runId']}/moves",
                params=params,
                json={"from": hint["from"], "to": hint["to"], "expectedRevision": 0},
            ).status_code
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(send, range(2))) == [200, 409]
    with Session(get_engine()) as db:
        row = db.get(Match3RunRow, UUID(str(run["runId"])))
        assert row is not None
        saved = Run.model_validate_json(json.dumps(row.data))
        assert saved.revision == 1 and saved.movesLeft == 23


def dig_board() -> list[list[Any]]:
    from test_match3_engine import stable_board

    equivalents = {
        "log": "rock",
        "tree": "brick",
        "leaf": "sprout",
        "acorn": "autumn",
        "mushroom": "rock",
    }
    return [[equivalents[chip] for chip in row] for row in stable_board()]


def test_dig_resume_keeps_payment_and_finding_opens_exact_selected_target(
    client: TestClient,
) -> None:
    params = player(client)
    response = client.post(
        PREFIX + "/runs",
        params=params,
        json={"shiftId": "dig", "target": {"q": 4, "r": 0}},
    )
    assert response.status_code == 201, response.text
    run = response.json()
    patch_run(run["runId"], lambda value: setattr(value, "digDepth", 3))
    result = client.post(f"{PREFIX}/runs/{run['runId']}/finish", params=params)
    assert result.status_code == 200
    assert result.json()["dig"]["savedDepth"] == 3
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["excavation"] == {
        "siteId": "dig:4:0",
        "position": {"q": 4, "r": 0},
        "terrain": state["excavation"]["terrain"],
        "depth": 3,
        "targetDepth": 6,
        "paid": True,
    }
    assert state["warehouse"]["food"]["amount"] == 80
    saved = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert len(saved.progress.world.cells) == 37
    assert (
        saved.progress.activeExcavation is not None
        and saved.progress.activeExcavation.paid
    )
    assert (
        client.post(
            PREFIX + "/runs",
            params=params,
            json={"shiftId": "dig", "target": {"q": -4, "r": 0}},
        ).status_code
        == 409
    )
    resumed = start(client, params, "dig")
    assert resumed["digDepth"] == 3 and resumed["amphora"] == {"row": 4, "column": 3}

    def prepare(value: Run) -> None:
        value.digDepth = 5
        value.board = dig_board()
        value.board[7][1] = value.board[7][2] = value.board[7][4] = "rock"
        value.board[7][3] = "brick"

    patch_run(resumed["runId"], prepare)
    found = client.post(
        f"{PREFIX}/runs/{resumed['runId']}/moves",
        params=params,
        json={"from": {"row": 7, "column": 3}, "to": {"row": 7, "column": 4}},
    )
    assert found.status_code == 200, found.text
    assert found.json()["status"] == "finished"
    discovery = found.json()["result"]["dig"]
    assert discovery["finding"]["siteId"] == "dig:4:0"
    assert discovery["revealedCell"] == {"q": 4, "r": 0}
    world = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert len(world.progress.world.cells) == 38
    assert (4, 0) in {cell.key for cell in world.progress.world.cells}
    assert (
        world.progress.activeExcavation is None and len(world.progress.collection) == 1
    )
    assert (
        client.post(
            f"{PREFIX}/runs/{resumed['runId']}/finish", params=params
        ).status_code
        == 200
    )
    unchanged = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert (
        len(unchanged.progress.world.cells) == 38
        and len(unchanged.progress.collection) == 1
    )
    assert (
        client.get(PREFIX + "/state", params=params).json()["warehouse"]["food"][
            "amount"
        ]
        == 80
    )


def test_dig_expiry_saves_depth_and_invalid_target_is_rejected(
    client: TestClient,
) -> None:
    params = player(client)
    for target in [{"q": 0, "r": 0}, {"q": 100, "r": 0}]:
        assert (
            client.post(
                PREFIX + "/runs",
                params=params,
                json={"shiftId": "dig", "target": target},
            ).status_code
            == 400
        )
    assert (
        client.post(
            PREFIX + "/runs",
            params=params,
            json={"shiftId": "farm", "target": {"q": 4, "r": 0}},
        ).status_code
        == 422
    )
    run = start(client, params, "dig")

    def prepare(value: Run) -> None:
        value.movesLeft, value.digDepth, value.rngState = 1, 5, 17
        value.board = dig_board()
        value.board[0][0] = value.board[0][1] = value.board[1][2] = "rock"

    patch_run(run["runId"], prepare)
    response = client.post(
        f"{PREFIX}/runs/{run['runId']}/moves",
        params=params,
        json={"from": {"row": 1, "column": 2}, "to": {"row": 0, "column": 2}},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["reason"] == "moves_exhausted" and result["dig"]["savedDepth"] == 5
    saved = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert saved.progress.activeExcavation is not None
    assert (
        saved.progress.activeExcavation.depth == 5
        and saved.progress.activeExcavation.paid
    )
    assert not saved.progress.collection


def test_corrupt_run_and_profile_are_not_reset(client: TestClient) -> None:
    params = player(client)
    run = start(client, params)
    with Session(get_engine()) as db:
        profile = db.get(Match3ProfileRow, UUID(params["player_id"]))
        assert profile is not None
        original = profile.data.copy()
        profile.data = {"schemaVersion": 999}
        db.commit()
    response = client.get(PREFIX + "/state", params=params)
    assert response.status_code == 422 and response.json()["code"] == "profile_corrupt"
    with Session(get_engine()) as db:
        profile = db.get(Match3ProfileRow, UUID(params["player_id"]))
        assert profile is not None and profile.data == {"schemaVersion": 999}
        profile.data = original
        row = db.get(Match3RunRow, UUID(str(run["runId"])))
        assert row is not None
        row.data = {"schemaVersion": 999}
        db.commit()
    response = client.get(f"{PREFIX}/runs/{run['runId']}/hint", params=params)
    assert response.status_code == 422 and response.json()["code"] == "run_corrupt"
    with Session(get_engine()) as db:
        row = db.get(Match3RunRow, UUID(str(run["runId"])))
        assert row is not None and row.data == {"schemaVersion": 999}


def test_client_cannot_submit_board_reward_or_negative_coordinates(
    client: TestClient,
) -> None:
    params = player(client)
    assert (
        client.post(
            PREFIX + "/runs", params=params, json={"shiftId": "sawmill", "stock": 999}
        ).status_code
        == 422
    )
    run = start(client, params)
    path = f"{PREFIX}/runs/{run['runId']}"
    assert (
        client.post(
            path + "/moves",
            params=params,
            json={
                "from": {"row": 0, "column": 0},
                "to": {"row": 0, "column": 1},
                "collected": 100,
            },
        ).status_code
        == 422
    )
    assert (
        client.post(path + "/lightning", params=params, json={"row": 8}).status_code
        == 422
    )
    assert (
        client.post(path + "/lightning", params=params, json={"row": "3"}).status_code
        == 422
    )
    assert client.get(PREFIX + "/state", params=params).json()["activeRun"] == run


def test_general_snapshot_cannot_erase_active_excavation(client: TestClient) -> None:
    params = player(client)
    run = start(client, params, "dig")
    original = client.get("/game/progress", params=params).json()
    saved = decode_save(original["data"])
    saved.progress.activeExcavation = None
    saved.progress.excavationSites.clear()
    response = client.put(
        "/game/progress",
        params=params,
        json={
            "data": saved.model_dump_json(),
            "expectedServerSavedAt": original["serverSavedAt"],
        },
    )
    assert response.status_code == 409
    assert client.get("/game/progress", params=params).json() == original
    assert client.get(PREFIX + "/state", params=params).json()["activeRun"] == run


def test_legacy_dig_load_normalizes_stones_and_can_play_mixed_match(
    client: TestClient,
) -> None:
    params = player(client)
    run = start(client, params, "dig")
    with Session(get_engine()) as db:
        row = db.get(Match3RunRow, UUID(run["runId"]))
        assert row is not None
        raw: dict[str, Any] = json.loads(json.dumps(row.data))
        raw["rules"]["mainChip"] = "digRock"
        raw["rules"]["chips"] = ["digRock", "brick", "sprout", "autumn", "rock"]
        chips = ["rock", "brick", "sprout", "autumn"]
        raw["board"] = [[chips[(r + c) % 4] for c in range(8)] for r in range(8)]
        raw["board"][0][:3] = ["rock", "digRock", "brick"]
        raw["board"][1][2] = "rock"
        row.data = raw
        db.commit()

    response = client.get(PREFIX + "/state", params=params)
    assert response.status_code == 200
    current = response.json()["activeRun"]
    assert current["board"][0][:3] == ["rock", "rock", "brick"]
    assert "digRock" not in response.text
    for field in (
        "runId",
        "movesLeft",
        "chargesLeft",
        "digDepth",
        "revision",
        "amphora",
    ):
        assert current[field] == run[field]
    assert current["serverSavedAt"] == run["serverSavedAt"]
    with Session(get_engine()) as db:
        row = db.get(Match3RunRow, UUID(run["runId"]))
        assert row is not None
        assert "digRock" not in json.dumps(row.data)
        rules = row.data["rules"]
        assert isinstance(rules, dict) and rules["chips"] == chips
        assert row.data["rngState"] == raw["rngState"]

    moved = client.post(
        f"{PREFIX}/runs/{run['runId']}/moves",
        params=params,
        json={
            "from": {"row": 1, "column": 2},
            "to": {"row": 0, "column": 2},
            "expectedRevision": current["revision"],
        },
    )
    assert moved.status_code == 200, moved.text
    result = moved.json()
    assert result["accepted"] and result["movesLeft"] == run["movesLeft"] - 1
    assert {(0, 0), (0, 1), (0, 2)} <= {
        (p["row"], p["column"]) for p in result["steps"][0]["cleared"]
    }
    assert "digRock" not in moved.text


@pytest.mark.parametrize("operation", ["start", "move"])
def test_legacy_idempotency_response_normalizes_all_board_frames(
    client: TestClient, operation: str
) -> None:
    params = player(client)
    key = "legacy-response"
    run = start(client, params, "dig", key=key if operation == "start" else None)
    url = PREFIX + "/runs"
    body = {"shiftId": "dig"}
    original = run
    if operation == "move":
        hint = client.get(f"{url}/{run['runId']}/hint", params=params).json()
        url += f"/{run['runId']}/moves"
        body = {
            "from": hint["from"],
            "to": hint["to"],
            "expectedRevision": hint["revision"],
        }
        response = client.post(
            url, params=params, json=body, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 200
        original = response.json()
        assert original["steps"]

    with Session(get_engine()) as db:
        receipt = db.get(Match3ReceiptRow, (UUID(params["player_id"]), key))
        assert receipt is not None
        encoded = json.dumps(receipt.response)
        assert '"rock"' in encoded
        receipt.response = json.loads(encoded.replace('"rock"', '"digRock"'))
        db.commit()

    replay = client.post(
        url, params=params, json=body, headers={"Idempotency-Key": key}
    )
    assert replay.status_code == (201 if operation == "start" else 200)
    assert replay.json() == original
    assert "digRock" not in replay.text
    with Session(get_engine()) as db:
        receipt = db.get(Match3ReceiptRow, (UUID(params["player_id"]), key))
        assert receipt is not None and receipt.response == original


def test_client_result_is_idempotent_and_does_not_store_a_board(
    client: TestClient,
) -> None:
    params = player(client)
    endpoint = PREFIX + "/results"
    run_id = uuid4()
    payload = {
        "runId": run_id.hex,
        "shiftId": "sawmill",
        "movesLeft": 18,
        "chargesLeft": 1,
        "collected": 32,
    }
    response = client.post(endpoint, params=params, json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["bonus"], result["credited"]) == (10, 42)
    assert result["warehouse"]["wood"]["amount"] == 162
    assert client.post(endpoint, params=params, json=payload).json() == result
    conflict = client.post(endpoint, params=params, json={**payload, "collected": 34})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    snapshot = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert snapshot.progress.completedShifts == 1
    with Session(get_engine()) as db:
        assert db.get(Match3RunRow, run_id) is None


def test_client_dig_preserves_depth_and_opens_random_allowed_cell(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = player(client)
    endpoint = PREFIX + "/results"
    initial = decode_save(client.get("/game/progress", params=params).json()["data"])
    known = {cell.key for cell in initial.progress.world.cells}
    eligible = {
        p
        for cell in known
        for p in neighbors(cell)
        if p not in known and p[1] <= CONFIG.start_radius
    }
    chosen = sorted(eligible)[-1]
    draws: list[set[tuple[int, int]]] = []

    def choose(candidates: list[tuple[int, int]]) -> tuple[int, int]:
        draws.append(set(candidates))
        return chosen

    monkeypatch.setattr("app.match3.service.secrets.choice", choose)
    partial = client.post(
        endpoint,
        params=params,
        json={
            "runId": uuid4().hex,
            "shiftId": "dig",
            "movesLeft": 0,
            "chargesLeft": 0,
            "collected": 0,
            "digDepth": 2,
        },
    )
    assert partial.status_code == 200, partial.text
    assert partial.json()["dig"]["savedDepth"] == 2
    assert partial.json()["dig"]["revealedCell"] is None
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["digDepth"] == 2 and state["activeRun"] is None

    payload = {
        "runId": uuid4().hex,
        "shiftId": "dig",
        "movesLeft": 8,
        "chargesLeft": 0,
        "collected": 0,
        "digDepth": 6,
        "digStartDepth": 2,
        "excavationSiteId": state["excavation"]["siteId"],
    }
    response = client.post(endpoint, params=params, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["dig"]["revealedCell"] == {"q": chosen[0], "r": chosen[1]}
    assert draws == [eligible, eligible]
    assert client.post(endpoint, params=params, json=payload).json() == response.json()
    snapshot = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert {cell.key for cell in snapshot.progress.world.cells} == known | {chosen}
    assert snapshot.progress.activeExcavation is None
    assert len(snapshot.progress.collection) == 1


def test_client_can_complete_legacy_run_without_server_moves(
    client: TestClient,
) -> None:
    params = player(client)
    run = start(client, params)
    payload = {
        "runId": run["runId"],
        "shiftId": "sawmill",
        "movesLeft": 23,
        "chargesLeft": 1,
        "collected": 3,
        "legacyRevision": run["revision"],
    }
    response = client.post(PREFIX + "/results", params=params, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["credited"] == 3
    assert client.get(PREFIX + "/state", params=params).json()["activeRun"] is None
    legacy_finish = client.post(PREFIX + f"/runs/{run['runId']}/finish", params=params)
    assert legacy_finish.json() == response.json()


def test_client_pause_checkpoints_board_and_only_final_result_credits(
    client: TestClient,
) -> None:
    params = player(client)
    pause_url, result_url = PREFIX + "/pause", PREFIX + "/results"
    payload = {
        "runId": uuid4().hex,
        "shiftId": "sawmill",
        "movesLeft": 23,
        "chargesLeft": 1,
        "collected": 3,
    }
    board = engine.create_board(RULES["sawmill"].chips, engine.RandomStream(17).next)
    first = client.post(pause_url, params=params, json={**payload, "board": board})
    assert first.status_code == 200, first.text
    paused = first.json()
    assert paused["clientManaged"] and paused["board"] == board
    assert (paused["movesLeft"], paused["chargesLeft"], paused["collected"]) == (
        23,
        1,
        3,
    )
    assert (
        client.post(pause_url, params=params, json={**payload, "board": board}).json()
        == paused
    )
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["activeRun"] == paused
    assert state["warehouse"]["wood"]["amount"] == 120
    snapshot = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert snapshot.progress.completedShifts == 0
    old_finish = client.post(PREFIX + f"/runs/{paused['runId']}/finish", params=params)
    assert old_finish.json()["code"] == "client_managed"

    second = client.post(
        pause_url,
        params=params,
        json={
            **payload,
            "legacyRevision": paused["revision"],
            "board": board,
            "movesLeft": 22,
            "collected": 6,
        },
    )
    assert second.status_code == 200, second.text
    conflict = client.post(
        pause_url,
        params=params,
        json={
            **payload,
            "legacyRevision": paused["revision"],
            "board": board,
            "movesLeft": 21,
            "collected": 9,
        },
    )
    assert conflict.status_code == 409
    final_payload = {
        **payload,
        "legacyRevision": second.json()["revision"],
        "movesLeft": 22,
        "collected": 6,
    }
    result = client.post(result_url, params=params, json=final_payload)
    assert result.status_code == 200, result.text
    assert result.json()["credited"] == 6
    assert (
        client.post(result_url, params=params, json=final_payload).json()
        == result.json()
    )
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["activeRun"] is None
    assert state["warehouse"]["wood"]["amount"] == 126


def test_client_pause_dig_saves_depth_without_revealing_a_cell(
    client: TestClient,
) -> None:
    params = player(client)
    before = decode_save(client.get("/game/progress", params=params).json()["data"])
    payload = {
        "runId": uuid4().hex,
        "shiftId": "dig",
        "movesLeft": 18,
        "chargesLeft": 0,
        "collected": 0,
        "digDepth": 2,
        "board": engine.create_board(
            RULES["dig"].chips,
            engine.RandomStream(17).next,
            CellPosition(row=3, column=3),
        ),
    }
    pause = client.post(PREFIX + "/pause", params=params, json=payload)
    assert pause.status_code == 200, pause.text
    saved = pause.json()
    state = client.get(PREFIX + "/state", params=params).json()
    assert state["digDepth"] == 2
    assert state["excavation"]["siteId"] == saved["excavationSiteId"]
    assert state["activeRun"]["amphora"] == {"row": 3, "column": 3}
    after = decode_save(client.get("/game/progress", params=params).json()["data"])
    assert after.progress.world.cells == before.progress.world.cells
    assert after.progress.collection == before.progress.collection
    assert after.progress.completedShifts == before.progress.completedShifts
    payload.pop("board")
    payload.update(
        {
            "legacyRevision": saved["revision"],
            "excavationSiteId": saved["excavationSiteId"],
            "digStartDepth": 2,
            "digDepth": 6,
            "movesLeft": 13,
        }
    )
    finish = client.post(PREFIX + "/results", params=params, json=payload)
    assert finish.status_code == 200, finish.text
    assert finish.json()["dig"]["revealedCell"] is not None
