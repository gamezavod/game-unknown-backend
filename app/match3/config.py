"""Значения из frontend 1c43202, src/match3.ts и match3-ui.ts.

Это согласованные настройки прототипа, а не симуляция экономики острова.
Новых платежей за раскопки нет: прототип считает провизию уже внесённой.
"""

from app.match3.state import Profile, Resources, Rules, ShiftId

RULES: dict[ShiftId, Rules] = {
    "sawmill": Rules(
        shiftId="sawmill",
        title="Лесопилка",
        resourceId="wood",
        mainChip="log",
        chips=["log", "tree", "leaf", "acorn", "mushroom"],
        target=30,
        bonus=10,
        moves=24,
        charges=1,
        requiredWorkers=1,
    ),
    "farm": Rules(
        shiftId="farm",
        title="Ферма",
        resourceId="food",
        mainChip="wheat",
        chips=["wheat", "carrot", "apple", "sunflower", "pumpkin"],
        target=30,
        bonus=10,
        moves=22,
        charges=0,
        requiredWorkers=1,
    ),
    "quarry": Rules(
        shiftId="quarry",
        title="Каменоломня",
        resourceId="stone",
        mainChip="rock",
        chips=["rock", "sand", "brick", "crystal", "clay"],
        target=30,
        bonus=10,
        moves=22,
        charges=0,
        requiredWorkers=1,
    ),
    "mine": Rules(
        shiftId="mine",
        title="Рудник",
        resourceId="iron",
        mainChip="ore",
        chips=["ore", "gem", "lantern", "pickaxe", "gear"],
        target=30,
        bonus=10,
        moves=17,
        charges=0,
        requiredWorkers=1,
    ),
    "dig": Rules(
        shiftId="dig",
        title="Раскопки",
        resourceId=None,
        mainChip="rock",
        chips=["rock", "brick", "sprout", "autumn"],
        target=1,
        bonus=0,
        moves=20,
        charges=0,
        requiredWorkers=0,
    ),
}


def new_profile() -> Profile:
    return Profile(
        stock=Resources(wood=120, food=80, stone=60, iron=0),
        limits=Resources(wood=200, food=200, stone=200, iron=200),
        workers={"sawmill": 2, "farm": 2, "quarry": 2, "mine": 1, "dig": 0},
        energy={key: key == "sawmill" for key in RULES},
    )
