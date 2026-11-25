"""Изменения мира выполняются сервером. Экономика и механика раскопок не добавляются."""

from typing import Any

from .generator import generate_cell, generate_world, neighbors
from .state import GameProgress, Position, Sapling


def new_progress(seed: str) -> GameProgress:
    return GameProgress(
        world=generate_world(seed),
        saplings=[],
        excavationSites=[],
        activeExcavation=None,
        collection=[],
        completedShifts=0,
    )


def advance_growth(progress: GameProgress, now: int) -> GameProgress:
    ready = {
        s.key for s in progress.saplings if now >= s.plantedAt + s.growthDurationMs
    }
    if not ready:
        return progress
    result = progress.model_copy(deep=True)
    result.saplings = [s for s in result.saplings if s.key not in ready]
    for cell in result.world.cells:
        if cell.key in ready:
            cell.type = "forest"
    return result


def apply_action(
    progress: GameProgress,
    action: dict[str, Any],
    now: int,
    growth_duration_ms: int = 60_000,
) -> GameProgress:
    kind = action["type"]
    if kind == "advanceGrowth":
        return advance_growth(progress, now)
    if kind == "completeShift":
        result = advance_growth(progress, now).model_copy(deep=True)
        result.completedShifts += 1
        return result
    p = Position.model_validate(action["position"]).key
    cell = next((cell for cell in progress.world.cells if cell.key == p), None)
    if kind == "reveal":
        if cell:
            return progress
        known = {c.key for c in progress.world.cells}
        if not any(n in known for n in neighbors(p)):
            raise ValueError("Можно открыть только соседнюю клетку тумана")
        result = progress.model_copy(deep=True)
        result.world.cells.append(generate_cell(progress.world, p))
        return result
    if not cell or cell.building:
        raise ValueError("Участок недоступен для изменения леса")
    result = progress.model_copy(deep=True)
    if kind == "chopForest":
        if cell.type != "forest":
            raise ValueError("На участке нет леса")
        next(c for c in result.world.cells if c.key == p).type = "grass"
    elif kind == "plantForest":
        if cell.type != "grass" or any(s.key == p for s in progress.saplings):
            raise ValueError("Саженец здесь посадить нельзя")
        result.saplings.append(
            Sapling(q=p[0], r=p[1], plantedAt=now, growthDurationMs=growth_duration_ms)
        )
    else:
        raise ValueError("Неизвестное действие")
    return GameProgress.model_validate(result.model_dump())
