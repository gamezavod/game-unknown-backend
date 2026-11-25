import pytest

from app.match3 import engine
from app.match3.config import RULES
from app.match3.state import Board, CascadeStep, CellPosition, Run, ShiftId


def make_run(shift: ShiftId = "sawmill", seed: int = 17) -> Run:
    rules = RULES[shift].model_copy(deep=True)
    rng = engine.RandomStream(seed)
    blocked = CellPosition(row=1, column=3) if shift == "dig" else None
    board = engine.create_board(rules.chips, rng.next, blocked)
    return Run(
        rules=rules,
        board=board,
        rngState=rng.state,
        movesLeft=rules.moves,
        chargesLeft=rules.charges,
        siteId="test-site" if blocked else None,
    )


def stable_board() -> Board:
    return [
        ["tree", "log", "leaf", "mushroom", "acorn", "tree", "leaf", "acorn"],
        ["acorn", "leaf", "log", "tree", "mushroom", "acorn", "tree", "leaf"],
        ["log", "tree", "leaf", "acorn", "tree", "leaf", "mushroom", "acorn"],
        ["leaf", "mushroom", "acorn", "tree", "leaf", "acorn", "tree", "log"],
        ["tree", "acorn", "tree", "leaf", "mushroom", "tree", "leaf", "acorn"],
        ["mushroom", "tree", "leaf", "acorn", "tree", "leaf", "mushroom", "tree"],
        ["tree", "leaf", "mushroom", "tree", "leaf", "acorn", "tree", "leaf"],
        ["leaf", "acorn", "tree", "leaf", "mushroom", "tree", "leaf", "acorn"],
    ]


def winning_setup() -> Run:
    run = make_run()
    run.board = stable_board()
    run.board[0][0] = run.board[0][1] = run.board[1][2] = "log"
    return run


def test_all_themes_generate_stable_playable_boards() -> None:
    for shift in RULES:
        for seed in range(1, 101):
            run = make_run(shift, seed)
            assert len(run.board) == 8
            assert all(len(row) == 8 for row in run.board)
            assert not engine.matches(run.board, run.amphora)
            assert engine.possible_moves(run.board, run.amphora)
            assert all(c in RULES[shift].chips for row in run.board for c in row)


@pytest.mark.parametrize("vertical", [False, True])
def test_legacy_mixed_stones_load_as_one_matching_chip(vertical: bool) -> None:
    original = make_run("dig")
    original.digDepth, original.movesLeft, original.revision = 3, 7, 4
    raw = original.model_dump()
    raw["rules"]["mainChip"] = "digRock"
    raw["rules"]["chips"] = ["digRock", "brick", "sprout", "autumn", "rock"]
    positions = [(i, 0) if vertical else (0, i) for i in range(3)]
    for (row, column), chip in zip(positions, ["rock", "digRock", "rock"], strict=True):
        raw["board"][row][column] = chip

    loaded = Run.model_validate(raw)
    assert loaded.rules.mainChip == "rock"
    assert loaded.rules.chips == ["rock", "brick", "sprout", "autumn"]
    assert set(positions) <= {
        p.key for p in engine.matches(loaded.board, loaded.amphora)
    }
    assert "digRock" not in loaded.model_dump_json()
    for field in (
        "rngState",
        "movesLeft",
        "chargesLeft",
        "digDepth",
        "siteId",
        "revision",
    ):
        assert getattr(loaded, field) == getattr(original, field)
    # Чтение совместимого формата не исправляет входной объект на месте.
    assert raw["board"][positions[1][0]][positions[1][1]] == "digRock"


def test_dig_generation_and_refill_use_four_unique_chips() -> None:
    assert RULES["dig"].chips == ["rock", "brick", "sprout", "autumn"]
    run = make_run("dig")
    rng = engine.RandomStream(101)
    for _ in range(20):
        engine.clear_step(
            run, [CellPosition(row=7, column=c) for c in range(8)], rng.next
        )
        assert "digRock" not in run.model_dump_json()


def test_matches_merge_crossings_and_ignore_amphora() -> None:
    board = stable_board()
    board[0][0] = board[0][1] = board[0][2] = "log"
    board[1][1] = board[2][1] = "log"
    assert len(engine.matches(board)) == 5
    assert not engine.matches(board, CellPosition(row=0, column=1))


def test_invalid_swap_does_not_change_board_turns_or_rng() -> None:
    run = make_run()
    run.board = stable_board()
    before = run.model_dump()
    result = engine.move(
        run, CellPosition(row=0, column=0), CellPosition(row=0, column=1)
    )
    assert not result.accepted and result.rejection == "no_match"
    assert result.run.model_dump() == before == run.model_dump()
    result = engine.move(
        run, CellPosition(row=0, column=0), CellPosition(row=7, column=7)
    )
    assert not result.accepted and result.rejection == "not_adjacent"
    assert not result.steps


def test_move_cascades_have_continuous_frames_and_exact_loot() -> None:
    run = winning_setup()
    before = run.model_dump()
    a, b = CellPosition(row=1, column=2), CellPosition(row=0, column=2)
    result = engine.move(run, a, b)
    assert result.accepted
    assert result.run.movesLeft == run.movesLeft - 1
    assert result.steps[0].before == result.swapped
    assert result.steps[-1].after == result.run.board
    for previous, following in zip(result.steps, result.steps[1:], strict=False):
        assert previous.after == following.before
    assert result.run.collected == sum(
        step.before[p.row][p.column] == "log"
        for step in result.steps
        for p in step.cleared
    )
    assert result.run.collected >= 3
    assert run.model_dump() == before
    assert engine.move(run, a, b) == result
    if not result.run.goal_reached:
        assert not engine.matches(result.run.board)
        assert engine.possible_moves(result.run.board)


def test_gravity_preserves_surviving_chip_order() -> None:
    board = stable_board()
    before = [row[0] for row in board]
    result = engine.drop_refill(
        board,
        [CellPosition(row=7, column=0), CellPosition(row=6, column=0)],
        RULES["sawmill"].chips,
        lambda: 0.0,
    )
    assert [row[0] for row in result][2:] == before[:6]
    assert result[0][0] == result[1][0] == "log"
    assert [row[0] for row in board] == before


def test_hint_is_a_legal_move_and_does_not_change_rng() -> None:
    run = make_run()
    before = run.model_dump()
    a, b = engine.possible_moves(run.board)[0]
    assert engine.move(run, a, b).accepted
    assert run.model_dump() == before


def test_amphora_blocks_swaps_and_progress_requires_path_clear() -> None:
    run = make_run("dig")
    pot = run.amphora
    assert pot is not None
    assert not engine.move(run, pot, CellPosition(row=1, column=2)).accepted
    before = run.digDepth
    engine.clear_step(run, [CellPosition(row=2, column=2)], lambda: 0.3)
    assert run.digDepth == before
    engine.clear_step(run, [CellPosition(row=2, column=3)], lambda: 0.5)
    assert run.digDepth == before + 1
    run.digDepth = 5
    engine.clear_step(run, [CellPosition(row=7, column=3)], lambda: 0.7)
    assert run.digDepth == 6 and run.goal_reached
    assert run.amphora == CellPosition(row=7, column=3)


def test_lightning_uses_charge_but_not_move() -> None:
    run = make_run()
    result, cleared, steps = engine.lightning(run, 3)
    assert len(cleared) == 8
    assert result.movesLeft == run.movesLeft
    assert result.chargesLeft == 0
    assert steps[0].kind == "lightning"
    assert steps[-1].after == result.board
    assert run.chargesLeft == 1
    if not result.goal_reached:
        assert not engine.matches(result.board)
        assert engine.possible_moves(result.board)


def test_corrupt_run_shape_is_rejected() -> None:
    raw = make_run().model_dump()
    raw["board"] = [["log"]]
    with pytest.raises(ValueError):
        Run.model_validate(raw)


def test_dead_board_reshuffle_is_in_animation_steps() -> None:
    run = make_run()
    chips = run.rules.chips
    run.board = [[chips[(row + column) % 5] for column in range(8)] for row in range(8)]
    assert not engine.matches(run.board) and not engine.possible_moves(run.board)
    assert run.rngState is not None
    rng = engine.RandomStream(run.rngState)
    steps: list[CascadeStep] = []
    engine.settle(run, rng.next, steps)
    assert len(steps) == 1 and steps[0].kind == "reshuffle"
    assert steps[0].after == run.board
    assert engine.possible_moves(run.board) and not engine.matches(run.board)


def test_cascade_limit_prevents_endless_refill() -> None:
    run = make_run()
    run.rules.target = 99999
    run.board = [["log"] * 8 for _ in range(8)]
    steps: list[CascadeStep] = []
    engine.settle(run, lambda: 0.0, steps)
    assert len(steps) == 41 and steps[-1].kind == "reshuffle"
    assert not engine.matches(run.board) and engine.possible_moves(run.board)
