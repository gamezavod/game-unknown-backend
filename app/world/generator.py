"""Серверный генератор островов v2.

Открытые клетки читаются из сохранения. Версия генератора определяет
территорию, которую игрок ещё не открыл.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

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
GENERATOR_V2 = 2
MAX_ISLAND_WIDTH = 500


@dataclass(frozen=True)
class GenerationConfig:
    start_radius: int = 3
    start_land: int = 25
    max_attempts: int = 128
    region_size: int = 7
    mountain_level: float = 0.69
    forest_threshold: float = 0.57


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
        height=1.12 if terrain == "river" else 1.45,
        rotation=(n % 6) * 60,
        variant=n % 3,
    )


def start_world_errors(cells: list[Cell]) -> list[str]:
    errors = []
    by_key = {cell.key: cell for cell in cells}
    if (
        len(cells) != CONFIG.start_land
        or len(by_key) != CONFIG.start_land
        or any(
            distance(c.key) > CONFIG.start_radius or c.state != "visible" for c in cells
        )
    ):
        errors.append("Неверная стартовая область")

    def count(terrain: Terrain) -> int:
        return sum(c.type == terrain for c in cells)

    if any(c.type in ("water", "river", "fog") for c in cells):
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
            if neighbor in by_key and neighbor not in reached:
                reached.add(neighbor)
                queue.append(neighbor)
    if len(reached) != len(cells):
        errors.append("Несвязная суша")
    return errors


def populate_start(
    cells: list[Cell],
    random: Callable[[], float],
    is_sea: Callable[[tuple[int, int]], bool],
) -> None:
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
    coast = [c for c in available if any(is_sea(p) for p in neighbors(c.key))]
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
            and sum(by_key[p].type == "forest" for p in neighbors(c.key) if p in by_key)
            >= 2
        ),
        None,
    )
    if mill:
        place(mill, "lumbermill")
    farm_sites = [c for c in available if c.type == "grass" and not c.building]
    farm = next(
        (c for c in farm_sites if any(is_sea(p) for p in neighbors(c.key))),
        farm_sites[0] if farm_sites else None,
    )
    if farm:
        place(farm, "farm")


def noise(seed: str, p: tuple[int, int], channel: str) -> float:
    q, r = p[0] / CONFIG.region_size, p[1] / CONFIG.region_size
    x, y = math.floor(q), math.floor(r)

    def smooth(v: float) -> float:
        return v * v * (3 - 2 * v)

    u, v = smooth(q - x), smooth(r - y)

    def at(a: int, b: int) -> float:
        return hash32(f"{seed}:v2:{channel}:{a},{b}") / 4294967296

    return (at(x, y) * (1 - u) + at(x + 1, y) * u) * (1 - v) + (
        at(x, y + 1) * (1 - u) + at(x + 1, y + 1) * u
    ) * v


@dataclass(frozen=True)
class Island:
    rows: dict[int, tuple[int, int]]
    river: frozenset[tuple[int, int]]

    def contains(self, p: tuple[int, int]) -> bool:
        q, r = p
        row = self.rows.get(r)
        return row is not None and row[0] <= q <= row[1]


@lru_cache(maxsize=128)
def island_layout(seed: str) -> Island:
    random = random_for(f"{seed}:v2:shape")
    depth = 24 + int(random() * 13)
    half_width = 9 + int(random() * 7)
    bend = random() * math.tau
    rows: dict[int, tuple[int, int]] = {}

    # Каждый ряд — единый отрезок суши. Центр задаётся по горизонтали
    # сцены (q + r/2), чтобы осевое смещение не увеличивало ширину острова.
    limit = (MAX_ISLAND_WIDTH - 1) / 2
    for r in range(-depth, -3):
        t = (-r - 4) / (depth - 4)
        center = math.sin(t * 4 + bend) * 3 * math.sin(t * math.pi)
        width = max(4, half_width * math.sin(math.pi * t) ** 0.75)
        left = min(center - width + math.sin(t * 23 + bend) * t, center - 2)
        right = max(center + width + math.sin(t * 19 - bend) * t, center + 2)
        rows[r] = (
            math.ceil(max(-limit, left) - r / 2),
            math.floor(min(limit, right) - r / 2),
        )

    # В стартовом диске 25 клеток суши. Профиль задаёт
    # широкий южный берег и неровный стык с северной частью.
    coast_profiles = ((4, 4, 3, 3), (5, 3, 3, 3), (4, 3, 4, 3), (4, 3, 3, 4))
    profile = (3, 4, 4) + coast_profiles[int(random() * len(coast_profiles))]
    for r, size in enumerate(profile, start=-3):
        disk_left = max(-3, -3 - r)
        disk_right = min(3, 3 - r)
        left = math.ceil(-(size - 1 + r) / 2) + int(random() * 3) - 1
        left = min(disk_right - size + 1, max(disk_left, left))
        if r == -1:
            left = min(0, max(2 - size, left))
        elif r == 0:
            left = min(-1, max(2 - size, left))
        elif r == 1:
            left = min(-1, max(1 - size, left))
        rows[r] = (left, left + size - 1)

    # Связное русло начинается в глубине острова и выходит к одному
    # из боковых берегов. Оно целиком севернее стартовой области.
    source = -depth + 4
    mouth = -8 - int(random() * 4)
    east = random() < 0.5
    river: set[tuple[int, int]] = set()
    q = sum(rows[source]) // 2
    for r in range(source, mouth + 1):
        left, right = rows[r]
        t = (r - source) / (mouth - source)
        middle = (left + right) / 2 + math.sin(t * 7 + bend) * (1 - t)
        edge = right if east else left
        target = round(middle + (edge - middle) * t**3)
        target = min(right, max(left, target))
        bridge = min(right + 1, max(left, q))
        if r > source:
            river.update(
                (column, r - 1) for column in range(min(q, bridge), max(q, bridge) + 1)
            )
        entry = min(bridge, right)
        river.update(
            (column, r) for column in range(min(entry, target), max(entry, target) + 1)
        )
        q = target
    return Island(rows, frozenset(river))


def starter_cells(seed: str, island: Island) -> list[Cell]:
    cells = [
        cell_at(p, "grass", seed)
        for p in disk(CONFIG.start_radius)
        if island.contains(p)
    ]
    populate_start(
        cells, random_for(f"{seed}:v2:start"), lambda p: not island.contains(p)
    )
    return cells


def generate_world(seed: str, max_attempts: int = CONFIG.max_attempts) -> World:
    if not isinstance(seed, str) or not seed:
        raise ValueError("Seed не должен быть пустым")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("Лимит попыток должен быть положительным целым числом")
    for attempt in range(max_attempts):
        candidate = seed if attempt == 0 else f"{seed}:v2:{attempt}"
        cells = starter_cells(candidate, island_layout(candidate))
        if not start_world_errors(cells):
            return World(seed=candidate, generatorVersion=GENERATOR_V2, cells=cells)
    raise ValueError(
        f"Не удалось создать подходящий остров за {max_attempts} попыток (seed: {seed})"
    )


def can_generate_cell(world: World, p: tuple[int, int]) -> bool:
    return world.generatorVersion == GENERATOR_V2 and island_layout(
        world.seed
    ).contains(p)


def world_frontier(world: World) -> list[tuple[int, int]]:
    if world.generatorVersion != GENERATOR_V2:
        return []
    known = {cell.key for cell in world.cells}
    return sorted(
        {
            p
            for cell in world.cells
            for p in neighbors(cell.key)
            if p not in known
            and p[1] <= CONFIG.start_radius
            and can_generate_cell(world, p)
        },
        key=lambda p: (distance(p), p),
    )


def generate_cell(world: World, p: tuple[int, int]) -> Cell:
    if any(type(n) is not int or abs(n) > 2**53 - 1 for n in p):
        raise ValueError("Неверные координаты")
    opened = next((c for c in world.cells if c.key == p), None)
    if opened:
        return opened.model_copy(deep=True)
    return generate_unopened_cell(world, p)


def fog_preview(world: World) -> list[Cell]:
    return [
        generate_unopened_cell(world, p).model_copy(update={"state": "unexplored"})
        for p in world_frontier(world)
    ]


def generate_unopened_cell(world: World, p: tuple[int, int]) -> Cell:
    if world.generatorVersion != GENERATOR_V2:
        raise ValueError(
            f"Не поддерживается версия генератора: {world.generatorVersion}"
        )
    island = island_layout(world.seed)
    if not island.contains(p):
        raise ValueError("Клетка находится за пределами острова")
    if distance(p) <= CONFIG.start_radius:
        return next(c for c in starter_cells(world.seed, island) if c.key == p)
    if p in island.river:
        return cell_at(p, "river", world.seed)
    if any(not island.contains(n) for n in neighbors(p)):
        terrain: Terrain = "coast"
    elif noise(world.seed, p, "height") > CONFIG.mountain_level:
        terrain = "rock"
    elif noise(world.seed, p, "moisture") > CONFIG.forest_threshold:
        terrain = "forest"
    else:
        terrain = "grass"
    return cell_at(p, terrain, world.seed)
