"""Чистый серверный генератор v1. Совместим по seed с прежней TS-версией.

Параметры меняются только вместе с новой версией генератора. Открытые клетки
всегда читаются из сохранения; неизвестная версия запрещает только новое открытие.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass

from .state import Building, Cell, Terrain, World

DIRECTIONS = ((1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1), (1, -1))
BUILDINGS: tuple[Building, ...] = (
    "townHall",
    "home",
    "lumbermill",
    "farm",
    "warehouse",
)
MASK = 0xFFFFFFFF


@dataclass(frozen=True)
class GenerationConfig:
    start_radius: int = 3
    start_land: int = 25
    max_attempts: int = 128
    region_size: int = 7
    sea_level: float = 0.38
    mountain_level: float = 0.69
    forest_threshold: float = 0.57
    river_level: float = 0.5
    river_width: float = 0.025


CONFIG = GenerationConfig()


def distance(p: tuple[int, int]) -> int:
    q, r = p
    return max(abs(q), abs(r), abs(q + r))


def neighbors(p: tuple[int, int]) -> list[tuple[int, int]]:
    return [(p[0] + q, p[1] + r) for q, r in DIRECTIONS]


def disk(radius: int) -> list[tuple[int, int]]:
    return [
        (q, r)
        for q in range(-radius, radius + 1)
        for r in range(max(-radius, -q - radius), min(radius, -q + radius) + 1)
    ]


def hash32(value: str) -> int:
    # JS charCodeAt обрабатывает UTF-16, включая пары суррогатов для emoji.
    encoded = value.encode("utf-16-le", errors="surrogatepass")
    n = 2166136261
    for i in range(0, len(encoded), 2):
        unit = encoded[i] | (encoded[i + 1] << 8)
        n = ((n ^ unit) * 16777619) & MASK
    n ^= n >> 16
    n = (n * 0x7FEB352D) & MASK
    n ^= n >> 15
    n = (n * 0x846CA68B) & MASK
    return (n ^ (n >> 16)) & MASK


def random_for(seed: str) -> Callable[[], float]:
    n = hash32(seed)

    def next_value() -> float:
        nonlocal n
        n = (n + 0x6D2B79F5) & MASK
        t = ((n ^ (n >> 15)) * (1 | n)) & MASK
        t ^= (t + (((t ^ (t >> 7)) * (61 | t)) & MASK)) & MASK
        return ((t ^ (t >> 14)) & MASK) / 4294967296

    return next_value


def shuffled[T](items: list[T], random: Callable[[], float]) -> list[T]:
    result = items.copy()
    for i in range(len(result) - 1, 0, -1):
        j = math.floor(random() * (i + 1))
        result[i], result[j] = result[j], result[i]
    return result


def cell_at(position: tuple[int, int], terrain: Terrain, seed: str) -> Cell:
    q, r = position
    n = hash32(f"{seed}:{q},{r}")
    return Cell(
        q=q,
        r=r,
        type=terrain,
        state="visible",
        height=0.04 if terrain in ("water", "river") else 1.45,
        rotation=(n % 6) * 60,
        variant=n % 3,
    )


def start_world_errors(cells: list[Cell]) -> list[str]:
    errors = []
    by_key = {cell.key: cell for cell in cells}
    if (
        len(cells) != 37
        or len(by_key) != 37
        or any(distance(c.key) > 3 or c.state != "visible" for c in cells)
    ):
        errors.append("Неверная стартовая область")

    def count(terrain: Terrain) -> int:
        return sum(c.type == terrain for c in cells)

    if len(cells) - count("water") != 25 or count("river") or count("fog"):
        errors.append("Неверный состав суши")
    if count("forest") < 4 or count("rock") < 2 or count("coast") < 3:
        errors.append("Недостаточно природных клеток")
    for kind in BUILDINGS:
        buildings = [cell for cell in cells if cell.building == kind]
        if len(buildings) != 1 or buildings[0].type != "grass":
            errors.append(f"Недопустимое здание: {kind}")
            continue
        cell = buildings[0]
        if kind == "townHall" and distance(cell.key) != 0:
            errors.append("Ратуша вне центра")
        if kind in ("home", "warehouse") and distance(cell.key) != 1:
            errors.append("Здание далеко от ратуши")
        if (
            kind == "lumbermill"
            and sum(
                by_key[p].type == "forest" for p in neighbors(cell.key) if p in by_key
            )
            < 2
        ):
            errors.append("Лесопилка без леса")
    if sum(bool(cell.building) for cell in cells) != 5:
        errors.append("Неверное число зданий")
    reached, queue = {(0, 0)}, [(0, 0)]
    for p in queue:
        for neighbor in neighbors(p):
            neighbor_cell = by_key.get(neighbor)
            if (
                neighbor_cell
                and neighbor_cell.type != "water"
                and neighbor not in reached
            ):
                reached.add(neighbor)
                queue.append(neighbor)
    if len(reached) != len(cells) - count("water"):
        errors.append("Несвязная суша")
    return errors


def generate_world(seed: str, max_attempts: int = CONFIG.max_attempts) -> World:
    if not isinstance(seed, str) or not seed:
        raise ValueError("Seed не должен быть пустым")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("Лимит попыток должен быть положительным целым числом")
    for attempt in range(max_attempts):
        random = random_for(f"{seed}:start:v1:{attempt}")
        positions = disk(3)
        land = set(disk(1))
        while len(land) < CONFIG.start_land:
            frontier = [
                p
                for p in positions
                if p not in land and any(n in land for n in neighbors(p))
            ]
            land.add(frontier[math.floor(random() * len(frontier))])
        cells = [cell_at(p, "grass" if p in land else "water", seed) for p in positions]
        by_key = {cell.key: cell for cell in cells}

        def place(cell: Cell, kind: Building) -> None:
            cell.building, cell.buildingLevel = kind, 1

        place(by_key[(0, 0)], "townHall")
        adjacent = shuffled(neighbors((0, 0)), random)
        place(by_key[adjacent[0]], "home")
        place(by_key[adjacent[1]], "warehouse")
        available = shuffled(
            [c for c in cells if c.type == "grass" and not c.building], random
        )
        coast = [
            c
            for c in available
            if any(
                by_key.get(p) and by_key[p].type == "water" for p in neighbors(c.key)
            )
        ]
        for cell in coast[: 3 + math.floor(random() * 3)]:
            cell.type = "coast"
        inland = [c for c in available if c.type == "grass"]
        for cell in inland[: 4 + math.floor(random() * 4)]:
            cell.type = "forest"
        for cell in [c for c in inland if c.type == "grass"][:2]:
            cell.type = "rock"
        mill = next(
            (
                c
                for c in available
                if c.type == "grass"
                and sum(
                    by_key[p].type == "forest" for p in neighbors(c.key) if p in by_key
                )
                >= 2
            ),
            None,
        )
        if mill:
            place(mill, "lumbermill")
        farm_sites = [c for c in available if c.type == "grass" and not c.building]
        farm = next(
            (
                c
                for c in farm_sites
                if any(
                    by_key.get(p) and by_key[p].type == "water"
                    for p in neighbors(c.key)
                )
            ),
            farm_sites[0] if farm_sites else None,
        )
        if farm:
            place(farm, "farm")
        if not start_world_errors(cells):
            return World(seed=seed, generatorVersion=1, cells=cells)
    raise ValueError(
        f"Не удалось создать подходящий остров за {max_attempts} попыток (seed: {seed})"
    )


def noise(seed: str, p: tuple[int, int], channel: str) -> float:
    q, r = p[0] / CONFIG.region_size, p[1] / CONFIG.region_size
    x, y = math.floor(q), math.floor(r)

    def smooth(v: float) -> float:
        return v * v * (3 - 2 * v)

    u, v = smooth(q - x), smooth(r - y)

    def at(a: int, b: int) -> float:
        return hash32(f"{seed}:v1:{channel}:{a},{b}") / 4294967296

    return (at(x, y) * (1 - u) + at(x + 1, y) * u) * (1 - v) + (
        at(x, y + 1) * (1 - u) + at(x + 1, y + 1) * u
    ) * v


def outside_terrain(world: World, p: tuple[int, int]) -> Terrain:
    elevation, wet = noise(world.seed, p, "height"), noise(world.seed, p, "moisture")
    start = (
        {c.key: c for c in generate_world(world.seed).cells}
        if distance(p) == 4
        else None
    )

    def water(n: tuple[int, int]) -> bool:
        if distance(n) <= 3 and start:
            return start[n].type == "water"
        return noise(world.seed, n, "height") < CONFIG.sea_level

    if water(p):
        return "water"
    if any(water(n) for n in neighbors(p)):
        return "coast"
    if elevation > CONFIG.mountain_level:
        return "rock"
    if abs(wet - CONFIG.river_level) < CONFIG.river_width:
        return "river"
    return "forest" if wet > CONFIG.forest_threshold else "grass"


def generate_cell(world: World, p: tuple[int, int]) -> Cell:
    if any(type(n) is not int or abs(n) > 2**53 - 1 for n in p):
        raise ValueError("Неверные координаты")
    opened = next((c for c in world.cells if c.key == p), None)
    if opened:
        return opened.model_copy(deep=True)
    if world.generatorVersion != 1:
        raise ValueError(f"Неизвестная версия генератора: {world.generatorVersion}")
    if distance(p) <= 3:
        return next(c for c in generate_world(world.seed).cells if c.key == p)
    return cell_at(p, outside_terrain(world, p), world.seed)
