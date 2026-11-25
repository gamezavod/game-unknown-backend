import json

import pytest

from app.world.generator import (
    MAX_ISLAND_WIDTH,
    disk,
    fog_preview,
    generate_cell,
    generate_world,
    island_layout,
    neighbors,
    start_world_errors,
    world_frontier,
)
from app.world.progress import apply_action, new_progress
from app.world.state import (
    Cell,
    SaveDataError,
    SaveGame,
    World,
    decode_save,
    encode_save,
)


def test_1000_valid_seeds_and_farm_preference() -> None:
    for seed in range(1000):
        world = generate_world(str(seed))
        assert start_world_errors(world.cells) == [], seed
        assert world.generatorVersion == 2
        assert len(world.cells) == 25
        by_key = {c.key: c for c in world.cells}
        assert set(by_key).issubset(disk(3))
        assert all(
            c.state == "visible" and c.type not in ("river", "water")
            for c in world.cells
        )
        layout = island_layout(world.seed)
        assert set(by_key) == {p for p in disk(3) if layout.contains(p)}
        assert sum(c.type == "forest" for c in world.cells) >= 4
        assert sum(c.type == "rock" for c in world.cells) >= 2
        assert sum(c.type == "coast" for c in world.cells) >= 3
        buildings = {c.building: c for c in world.cells if c.building}
        assert set(buildings) == {"townHall", "home", "lumbermill", "farm", "warehouse"}
        assert all(
            c.type == "grass" and c.buildingLevel == 1 for c in buildings.values()
        )
        assert buildings["townHall"].key == (0, 0)
        assert buildings["home"].key in neighbors((0, 0))
        assert buildings["warehouse"].key in neighbors((0, 0))
        assert (
            sum(
                by_key[p].type == "forest"
                for p in neighbors(buildings["lumbermill"].key)
                if p in by_key
            )
            >= 2
        )
        farm = next(c for c in world.cells if c.building == "farm")
        water_sites = [
            c
            for c in world.cells
            if c.type == "grass"
            and c.building in (None, "farm")
            and any(not layout.contains(p) for p in neighbors(c.key))
        ]
        if water_sites:
            assert any(not layout.contains(p) for p in neighbors(farm.key))


@pytest.mark.parametrize("seed", ["seed", "остров 🌲", "seed:with:separators"])
def test_v2_seed_reproduces_open_and_unopened_terrain(seed: str) -> None:
    world = generate_world(seed)
    repeated = generate_world(seed)
    assert world == repeated
    assert world != generate_world("other")
    assert fog_preview(world) == fog_preview(repeated)


def test_retry_limit() -> None:
    for attempts in [0, -1]:
        with pytest.raises(ValueError, match="Лимит"):
            generate_world("seed", attempts)
    with pytest.raises(ValueError, match="Seed"):
        generate_world("")
    rejected = 0
    for seed in range(100):
        try:
            generate_world(str(seed), 1)
        except ValueError as error:
            assert "за 1 попыток" in str(error)
            rejected += 1
            accepted = generate_world(str(seed))
            assert accepted.seed != str(seed)
            assert generate_world(accepted.seed, 1) == accepted
    assert rejected


def test_v1_preserves_opened_cells_without_generating_terrain() -> None:
    water = Cell(q=0, r=0, type="water", height=0.04, state="visible")
    world = World(seed="legacy", generatorVersion=1, cells=[water])
    assert generate_cell(world, water.key) == water
    assert world_frontier(world) == []
    assert fog_preview(world) == []
    with pytest.raises(ValueError, match="версия генератора: 1"):
        generate_cell(world, (1, 0))


def test_chopping_planting_and_elapsed_time_are_pure() -> None:
    p = new_progress("growth")
    forest = next(c for c in p.world.cells if c.type == "forest")
    action = {"type": "chopForest", "position": {"q": forest.q, "r": forest.r}}
    chopped = apply_action(p, action, 1000)
    assert next(c for c in p.world.cells if c.key == forest.key).type == "forest"
    assert next(c for c in chopped.world.cells if c.key == forest.key).type == "grass"
    planted = apply_action(
        chopped,
        {**action, "type": "plantForest", "now": 999999999999, "growthDurationMs": 1},
        2000,
        5000,
    )
    assert planted.saplings[0].plantedAt == 2000
    assert planted.saplings[0].growthDurationMs == 5000
    grown = apply_action(planted, {"type": "advanceGrowth"}, 10000)
    assert grown.saplings == []
    assert next(c for c in grown.world.cells if c.key == forest.key).type == "forest"
    assert len(planted.saplings) == 1


def test_migrations_corruption_and_unknown_versions() -> None:
    progress = new_progress("legacy").model_dump()
    raw = json.dumps({"schemaVersion": 1, "savedAt": 100, **progress})
    save = decode_save(raw)
    assert save.progress.model_dump() == progress
    assert decode_save(encode_save(save)) == save
    assert json.loads(raw)["schemaVersion"] == 1
    for bad in ["{", "null", '{"schemaVersion":999}', '{"schemaVersion":true}']:
        with pytest.raises(SaveDataError):
            decode_save(bad)
    save.progress.world.generatorVersion = 999
    assert decode_save(encode_save(save)).progress.world == save.progress.world


def test_paid_excavation_roundtrip() -> None:
    p = new_progress("dig").model_dump()
    p["excavationSites"] = [{"id": "dig", "q": 0, "r": 0}]
    p["activeExcavation"] = {"siteId": "dig", "depth": 7, "paid": True}
    p["collection"] = [{"id": "item", "siteId": "dig", "foundAt": 100}]
    save = SaveGame.model_validate(
        {
            "schemaVersion": 2,
            "savedAt": 100,
            "serverSavedAt": 101,
            "revision": 0,
            "cloudPending": False,
            "progress": p,
        }
    )
    assert decode_save(encode_save(save)) == save
    assert save.progress.activeExcavation is not None
    assert save.progress.activeExcavation.paid


def test_lazy_generation_is_order_independent_and_fog_is_implicit() -> None:
    world = generate_world("lazy")
    frontier = world_frontier(world)
    first, second = frontier[:2]
    previews = fog_preview(world)
    assert [cell.key for cell in previews] == frontier
    assert all(cell.state == "unexplored" and not cell.building for cell in previews)
    assert generate_cell(world, first) == previews[0].model_copy(
        update={"state": "visible"}
    )
    a = world.model_copy(deep=True)
    b = world.model_copy(deep=True)
    a.cells.append(generate_cell(a, first))
    a.cells.append(generate_cell(a, second))
    b.cells.append(generate_cell(b, second))
    b.cells.append(generate_cell(b, first))
    assert {c.key: c for c in a.cells} == {c.key: c for c in b.cells}
    assert len(world.cells) == 25
    assert first not in world_frontier(a)


def connected_from(
    start: tuple[int, int], cells: set[tuple[int, int]]
) -> set[tuple[int, int]]:
    seen, pending = {start}, [start]
    for cell in pending:
        for neighbor in neighbors(cell):
            if neighbor in cells and neighbor not in seen:
                seen.add(neighbor)
                pending.append(neighbor)
    return seen


def test_island_footprint_and_river_are_finite_and_connected() -> None:
    for seed in range(50):
        world = generate_world(str(seed))
        island = island_layout(world.seed)
        land = {
            (q, r)
            for r, (left, right) in island.rows.items()
            for q in range(left, right + 1)
        }
        columns = [q + r / 2 for q, r in land]
        assert max(columns) - min(columns) + 1 <= MAX_ISLAND_WIDTH
        assert max(r for _, r in land) == 3
        assert connected_from((0, 0), land) == land
        river = set(island.river)
        assert river <= land and all(r < -3 for _, r in river)
        assert connected_from(next(iter(river)), river) == river
        assert any(n not in land for p in river for n in neighbors(p))
        assert all(generate_cell(world, p).type == "river" for p in river)
        assert all(generate_cell(world, p).type != "water" for p in land)
        with pytest.raises(ValueError, match="за пределами острова"):
            generate_cell(world, (0, 4))
        world.cells.extend(
            generate_cell(world, p) for p in sorted(land - {c.key for c in world.cells})
        )
        assert world_frontier(world) == []
        assert fog_preview(world) == []
