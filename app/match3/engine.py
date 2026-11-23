"""Чистое поле match-3: без HTTP, базы, часов и глобального random.

Амфора — отдельная позиция над полем, как в существующем UI. Её нельзя
переставлять или включать в совпадение. Глубина растёт только от очистки пути.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.match3.state import Board, CascadeStep, CellPosition, ChipId, Run

Random = Callable[[], float]


@dataclass
class RandomStream:
    state: int

    def next(self) -> float:
        value = self.state
        value ^= (value << 13) & 0xFFFFFFFF
        value ^= value >> 17
        value ^= (value << 5) & 0xFFFFFFFF
        self.state = value & 0xFFFFFFFF
        return self.state / 4294967296


def clone(board: Board) -> Board:
    return [row.copy() for row in board]


def matches(board: Board, blocked: CellPosition | None = None) -> list[CellPosition]:
    found: set[tuple[int, int]] = set()
    stop = blocked.key if blocked else None
    for vertical in (False, True):
        for fixed in range(8):
            group: list[tuple[int, int]] = []
            previous: ChipId | None = None
            for moving in range(9):
                row, column = (moving, fixed) if vertical else (fixed, moving)
                chip = (
                    board[row][column] if moving < 8 and (row, column) != stop else None
                )
                if chip != previous or chip is None:
                    if len(group) >= 3:
                        found.update(group)
                    group = []
                if chip is not None:
                    group.append((row, column))
                previous = chip
    return [CellPosition(row=r, column=c) for r, c in sorted(found)]


def swap(board: Board, a: CellPosition, b: CellPosition) -> Board:
    result = clone(board)
    result[a.row][a.column], result[b.row][b.column] = (
        result[b.row][b.column],
        result[a.row][a.column],
    )
    return result


def possible_moves(
    board: Board, blocked: CellPosition | None = None
) -> list[tuple[CellPosition, CellPosition]]:
    result: list[tuple[CellPosition, CellPosition]] = []
    for row in range(8):
        for column in range(8):
            a = CellPosition(row=row, column=column)
            if blocked and a == blocked:
                continue
            for r, c in ((row, column + 1), (row + 1, column)):
                if r >= 8 or c >= 8:
                    continue
                b = CellPosition(row=r, column=c)
                if blocked and b == blocked:
                    continue
                cleared = matches(swap(board, a, b), blocked)
                if any(p == a or p == b for p in cleared):
                    result.append((a, b))
    return result


def create_board(
    chips: list[ChipId], random: Random, blocked: CellPosition | None = None
) -> Board:
    for _ in range(500):
        board: Board = [[] for _ in range(8)]
        for row in range(8):
            for column in range(8):
                allowed = [
                    chip
                    for chip in chips
                    if not (
                        column >= 2
                        and board[row][column - 1] == board[row][column - 2] == chip
                        or row >= 2
                        and board[row - 1][column] == board[row - 2][column] == chip
                    )
                ]
                board[row].append(
                    allowed[min(int(random() * len(allowed)), len(allowed) - 1)]
                )
        if possible_moves(board, blocked):
            return board
    raise ValueError("Не удалось создать поле с допустимым ходом за 500 попыток")


def drop_refill(
    board: Board, cleared: list[CellPosition], chips: list[ChipId], random: Random
) -> Board:
    keys = {p.key for p in cleared}
    result = clone(board)
    for column in range(8):
        stack = [
            board[row][column] for row in range(7, -1, -1) if (row, column) not in keys
        ]
        for row in range(7, -1, -1):
            result[row][column] = (
                stack.pop(0)
                if stack
                else chips[min(int(random() * len(chips)), len(chips) - 1)]
            )
    return result


def clear_step(
    run: Run, cleared: list[CellPosition], random: Random, kind: str = "match"
) -> CascadeStep:
    before, amphora = clone(run.board), run.amphora
    earned = (
        0
        if run.rules.shiftId == "dig"
        else sum(before[p.row][p.column] == run.rules.mainChip for p in cleared)
    )
    run.collected += earned
    if (
        amphora
        and run.digDepth < 6
        and any(
            p.row == amphora.row + 1 and p.column == amphora.column for p in cleared
        )
    ):
        run.digDepth += 1
    run.board = drop_refill(before, cleared, run.rules.chips, random)
    return CascadeStep(
        kind="lightning" if kind == "lightning" else "match",
        before=before,
        cleared=cleared,
        after=clone(run.board),
        earned=earned,
        amphoraBefore=amphora,
        amphoraAfter=run.amphora,
    )


def settle(run: Run, random: Random, steps: list[CascadeStep]) -> None:
    for _ in range(40):
        if run.goal_reached:
            return
        cleared = matches(run.board, run.amphora)
        if not cleared:
            break
        steps.append(clear_step(run, cleared, random))
    if not run.goal_reached and (
        matches(run.board, run.amphora) or not possible_moves(run.board, run.amphora)
    ):
        before = clone(run.board)
        run.board = create_board(run.rules.chips, random, run.amphora)
        steps.append(
            CascadeStep(
                kind="reshuffle",
                before=before,
                cleared=[],
                after=clone(run.board),
                amphoraBefore=run.amphora,
                amphoraAfter=run.amphora,
            )
        )


@dataclass
class Move:
    run: Run
    accepted: bool
    swapped: Board
    steps: list[CascadeStep]
    rejection: str | None = None


def move(original: Run, a: CellPosition, b: CellPosition) -> Move:
    run = original.model_copy(deep=True)
    if abs(a.row - b.row) + abs(a.column - b.column) != 1:
        return Move(run, False, clone(run.board), [], "not_adjacent")
    if run.amphora and (a == run.amphora or b == run.amphora):
        return Move(run, False, clone(run.board), [], "amphora_blocked")
    swapped = swap(run.board, a, b)
    if not any(p == a or p == b for p in matches(swapped, run.amphora)):
        return Move(run, False, swapped, [], "no_match")
    run.board = swapped
    run.movesLeft -= 1
    stream = RandomStream(run.rngState)
    steps: list[CascadeStep] = []
    settle(run, stream.next, steps)
    run.rngState = stream.state
    return Move(run, True, swapped, steps)


def lightning(
    original: Run, row: int
) -> tuple[Run, list[CellPosition], list[CascadeStep]]:
    run = original.model_copy(deep=True)
    cleared = [
        CellPosition(row=row, column=c)
        for c in range(8)
        if not run.amphora or run.amphora.key != (row, c)
    ]
    run.chargesLeft -= 1
    stream = RandomStream(run.rngState)
    steps = [clear_step(run, cleared, stream.next, "lightning")]
    settle(run, stream.next, steps)
    run.rngState = stream.state
    return run, cleared, steps
