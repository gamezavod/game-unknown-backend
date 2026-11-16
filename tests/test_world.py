import json
from pathlib import Path

import pytest

from app.world.generator import (
    generate_cell,
    generate_world,
    neighbors,
    start_world_errors,
)
from app.world.progress import apply_action, new_progress
from app.world.state import SaveDataError, SaveGame, decode_save, encode_save


def test_1000_valid_seeds_and_farm_preference() -> None:
    for seed in range(1000):
        world = generate_world(str(seed))
        assert start_world_errors(world.cells) == [], seed
        assert len(world.cells) == 37
        assert sum(c.type != "water" for c in world.cells) == 25
        by_key = {c.key: c for c in world.cells}
        farm = next(c for c in world.cells if c.building == "farm")
        water_sites = [
            c
            for c in world.cells
            if c.type == "grass"
            and c.building in (None, "farm")
            and any(
                by_key.get(p) and by_key[p].type == "water" for p in neighbors(c.key)
            )
        ]
        if water_sites:
            assert any(
                by_key.get(p) and by_key[p].type == "water" for p in neighbors(farm.key)
            )


def test_seed_compatibility_including_unicode_and_unopened_regions() -> None:
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures/generator-v1.json").read_text()
    )
    for fixture in fixtures:
        world = generate_world(fixture["world"]["seed"])
        assert world.model_dump() == fixture["world"]
        for cell in fixture["unopened"]:
            assert generate_cell(world, (cell["q"], cell["r"])).model_dump() == cell
    assert generate_world("seed") == generate_world("seed")
    assert generate_world("seed") != generate_world("other")


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
    assert rejected


def test_unknown_version_preserves_opened_cells_and_no_radius_cap() -> None:
    world = generate_world("versions")
    world.cells[0].type = "grass"
    world.generatorVersion = 999
    assert generate_cell(world, world.cells[0].key) == world.cells[0]
    with pytest.raises(ValueError, match="версия генератора"):
        generate_cell(world, (4, 0))
    world.generatorVersion = 1
    assert generate_cell(world, (100000, -100000)) == generate_cell(
        world, (100000, -100000)
    )
    assert len(world.cells) == 37


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
