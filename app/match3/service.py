import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from app.config import Settings
from app.match3 import engine
from app.match3.config import RULES
from app.match3.repository import Match3Repository
from app.match3.state import (
    CellPosition,
    DigResult,
    ExcavationView,
    FinishResponse,
    Goal,
    HintResponse,
    LightningResponse,
    MoveRequest,
    MoveResponse,
    Profile,
    Reason,
    Resources,
    Result,
    Rules,
    Run,
    RunView,
    ShiftId,
    ShiftView,
    StateResponse,
    Stock,
    Warehouse,
)
from app.models import Match3ProfileRow, Match3ReceiptRow, Match3RunRow, WorldSave
from app.world.generator import distance, generate_cell, neighbors
from app.world.repository import WorldRepository
from app.world.service import WorldService
from app.world.state import (
    ActiveExcavation,
    CollectionItem,
    ExcavationSite,
    GameProgress,
    Position,
    SaveGame,
)


class Match3Error(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status, self.code, self.detail = status, code, detail


@dataclass
class Context:
    world_row: WorldSave
    saved: SaveGame
    profile_row: Match3ProfileRow
    profile: Profile


Response = RunView | MoveResponse | LightningResponse | FinishResponse


class Match3Service:
    def __init__(self, repo: Match3Repository, settings: Settings) -> None:
        self.repo, self.settings = repo, settings
        self.worlds = WorldRepository(repo.db)
        self.world_service = WorldService(self.worlds, settings)

    def context(self, player: UUID) -> Context:
        row = self.worlds.get(player, lock=True)
        if row is None:
            raise Match3Error(
                404, "world_not_found", "Сначала создайте мир через /game/session"
            )
        saved = self.world_service.read(row)
        profile = self.repo.profile(player)
        try:
            return Context(
                row,
                saved,
                profile,
                Profile.model_validate_json(json.dumps(profile.data)),
            )
        except (ValidationError, ValueError) as error:
            raise Match3Error(
                422,
                "profile_corrupt",
                "Состояние склада повреждено; данные не сброшены",
            ) from error

    def read_run(self, player: UUID, run_id: UUID) -> tuple[Match3RunRow, Run]:
        row = self.repo.run(player, run_id)
        if row is None:
            raise Match3Error(404, "run_not_found", "Смена не найдена у этого игрока")
        try:
            run = Run.model_validate_json(json.dumps(row.data))
            if row.status != run.status or run.result and run.result.runId != row.id:
                raise ValueError("Несогласованная смена")
            canonical = run.model_dump(mode="json")
            if row.data != canonical:
                # Совместимость фишек не расходует ход и не меняет RNG/revision.
                row.data = canonical
            return row, run
        except (ValidationError, ValueError) as error:
            raise Match3Error(
                422, "run_corrupt", "Сохранение смены повреждено; данные не сброшены"
            ) from error

    @staticmethod
    def warehouse(profile: Profile) -> Warehouse:
        return Warehouse(
            wood=Stock(amount=profile.stock.wood, limit=profile.limits.wood),
            food=Stock(amount=profile.stock.food, limit=profile.limits.food),
            stone=Stock(amount=profile.stock.stone, limit=profile.limits.stone),
            iron=Stock(amount=profile.stock.iron, limit=profile.limits.iron),
        )

    @staticmethod
    def goal(rules: Rules, run: Run | None = None) -> Goal:
        dig = rules.shiftId == "dig"
        return Goal(
            type="amphora" if dig else "collect",
            chipId=None if dig else rules.mainChip,
            target=rules.target,
            progress=(int(run.digDepth >= 6) if dig else run.collected) if run else 0,
            bonus=rules.bonus,
            targetDepth=6 if dig else None,
        )

    def view(self, ctx: Context, row: Match3RunRow, run: Run) -> RunView:
        loot = Resources()
        if run.rules.resourceId:
            loot.put(run.rules.resourceId, run.collected)
        assert ctx.saved.serverSavedAt is not None
        return RunView(
            playerId=row.player_id,
            serverSavedAt=ctx.saved.serverSavedAt,
            runId=row.id,
            shiftId=run.rules.shiftId,
            board=run.board,
            goal=self.goal(run.rules, run),
            movesLeft=run.movesLeft,
            chargesLeft=run.chargesLeft,
            collected=run.collected,
            loot=loot,
            digDepth=run.digDepth,
            amphora=run.amphora,
            revision=run.revision,
            status=run.status,
            result=run.result,
        )

    def reasons(
        self, ctx: Context, rules: Rules, active: Match3RunRow | None
    ) -> list[Reason]:
        reasons: list[Reason] = []
        if active:
            reasons.append(
                Reason(code="active_run", message="Сначала завершите активную смену")
            )
        if ctx.profile.workers.get(rules.shiftId, 0) < rules.requiredWorkers:
            reasons.append(
                Reason(
                    code="no_workers", message="Для смены нужен хотя бы один работник"
                )
            )
        resource = rules.resourceId
        if resource and ctx.profile.stock.amount(resource) >= ctx.profile.limits.amount(
            resource
        ):
            reasons.append(
                Reason(code="warehouse_full", message="Склад этого ресурса заполнен")
            )
        if rules.shiftId == "dig" and ctx.saved.progress.world.generatorVersion != 1:
            reasons.append(
                Reason(
                    code="unsupported_generator",
                    message="Версия генератора пока не поддерживает расширение",
                )
            )
        if rules.shiftId == "dig" and not self.site_available(ctx.saved.progress):
            reasons.append(
                Reason(
                    code="no_excavation_site", message="Нет доступного участка раскопок"
                )
            )
        return reasons

    @staticmethod
    def frontier(progress: GameProgress) -> list[tuple[int, int]]:
        known = {cell.key for cell in progress.world.cells}
        return sorted(
            {
                p
                for cell in progress.world.cells
                for p in neighbors(cell.key)
                if p not in known
            },
            key=lambda p: (distance(p), p),
        )

    @staticmethod
    def site_available(progress: GameProgress) -> bool:
        return bool(progress.activeExcavation or Match3Service.frontier(progress))

    @staticmethod
    def configured_rules(profile: Profile, shift_id: ShiftId) -> Rules:
        rules = RULES[shift_id].model_copy(deep=True)
        if rules.resourceId:
            workers = profile.workers.get(shift_id, 0)
            rules.moves = 10 + 5 * workers + profile.happinessMoves if workers else 0
            if shift_id == "sawmill" and workers:
                rules.moves += profile.forestMoves
        if not profile.energy.get(shift_id, False):
            rules.charges = 0
        return rules

    def state(self, player: UUID) -> StateResponse:
        ctx = self.context(player)
        active_row = self.repo.active(player)
        active = self.read_run(player, active_row.id)[1] if active_row else None
        shifts = []
        for shift_id in RULES:
            rules = self.configured_rules(ctx.profile, shift_id)
            reasons = self.reasons(ctx, rules, active_row)
            shifts.append(
                ShiftView(
                    shiftId=rules.shiftId,
                    title=rules.title,
                    resourceId=rules.resourceId,
                    goal=self.goal(rules),
                    moves=rules.moves,
                    charges=rules.charges
                    if ctx.profile.energy.get(rules.shiftId, False)
                    else 0,
                    requiredWorkers=rules.requiredWorkers,
                    assignedWorkers=ctx.profile.workers.get(rules.shiftId, 0),
                    available=not reasons,
                    reasons=reasons,
                )
            )
        dig = ctx.saved.progress.activeExcavation
        site = next(
            (
                s
                for s in ctx.saved.progress.excavationSites
                if dig and s.id == dig.siteId
            ),
            None,
        )
        assert ctx.saved.serverSavedAt is not None
        result = StateResponse(
            playerId=player,
            serverSavedAt=ctx.saved.serverSavedAt,
            warehouse=self.warehouse(ctx.profile),
            digDepth=dig.depth if dig else 0,
            excavation=ExcavationView(
                siteId=dig.siteId,
                position=Position(q=site.q, r=site.r),
                terrain=generate_cell(ctx.saved.progress.world, site.key).type,
                depth=dig.depth,
                paid=dig.paid,
            )
            if dig and site
            else None,
            shifts=shifts,
            activeRun=self.view(ctx, active_row, active)
            if active_row and active
            else None,
            lastResult=ctx.profile.lastResult,
        )
        self.repo.db.commit()
        return result

    def cached(
        self, player: UUID, key: str | None, operation: str, body: dict[str, object]
    ) -> Response | None:
        if key is None:
            return None
        fingerprint = self.fingerprint(operation, body)
        receipt = self.repo.receipt(player, key)
        if receipt is None:
            return None
        if receipt.fingerprint != fingerprint:
            raise Match3Error(
                409,
                "idempotency_conflict",
                "Idempotency-Key уже использован для другого запроса",
            )
        cls: type[RunView] | type[FinishResponse]
        if operation.startswith("move:"):
            cls = MoveResponse
        elif operation.startswith("lightning:"):
            cls = LightningResponse
        elif operation.startswith("finish:"):
            cls = FinishResponse
        else:
            cls = RunView
        try:
            result = cls.model_validate_json(json.dumps(receipt.response))
        except ValidationError as error:
            raise Match3Error(
                422, "receipt_corrupt", "Сохранённый ответ поврежден"
            ) from error
        canonical = result.model_dump(mode="json")
        if receipt.response != canonical:
            receipt.response = canonical
        self.repo.db.commit()
        return result

    @staticmethod
    def fingerprint(operation: str, body: dict[str, object]) -> str:
        return hashlib.sha256(
            (
                operation + json.dumps(body, sort_keys=True, separators=(",", ":"))
            ).encode()
        ).hexdigest()

    def commit_response(
        self,
        player: UUID,
        key: str | None,
        operation: str,
        body: dict[str, object],
        result: Response,
    ) -> Response:
        if key:
            self.repo.db.add(
                Match3ReceiptRow(
                    player_id=player,
                    key=key,
                    fingerprint=self.fingerprint(operation, body),
                    response=result.model_dump(mode="json"),
                )
            )
        self.repo.db.commit()
        return result

    def persist(self, ctx: Context, row: Match3RunRow, run: Run) -> None:
        progress = GameProgress.model_validate(ctx.saved.progress.model_dump())
        now = self.worlds.now_ms()
        assert ctx.saved.serverSavedAt is not None
        stamp = max(now, int(ctx.saved.serverSavedAt) + 1)
        ctx.saved = ctx.saved.model_copy(
            update={
                "savedAt": now,
                "serverSavedAt": stamp,
                "revision": ctx.saved.revision + 1,
                "cloudPending": False,
                "progress": progress,
            }
        )
        ctx.world_row.data = ctx.saved.model_dump(mode="json")
        ctx.world_row.server_saved_at = stamp
        ctx.profile_row.data = ctx.profile.model_dump(mode="json")
        if run.status == "finished" and run.settledAt is None:
            run.settledAt = stamp
        row.data, row.status = run.model_dump(mode="json"), run.status
        self.repo.db.flush()

    @staticmethod
    def prepare_dig(progress: GameProgress, target: Position | None) -> tuple[str, int]:
        if progress.activeExcavation is not None:
            current = progress.activeExcavation
            site = next(s for s in progress.excavationSites if s.id == current.siteId)
            if target is not None and target.key != site.key:
                raise Match3Error(
                    409, "unfinished_excavation", "Сначала завершите начатый участок"
                )
        else:
            frontier = Match3Service.frontier(progress)
            destination = target.key if target is not None else frontier[0]
            if destination not in frontier:
                raise Match3Error(
                    400, "invalid_target", "Выберите соседний участок тумана"
                )
            q, r = destination
            site = ExcavationSite(id=f"dig:{q}:{r}", q=q, r=r)
            progress.excavationSites.append(site)
            progress.activeExcavation = ActiveExcavation(
                siteId=site.id, depth=0, paid=True
            )
        # Прототип уже считает провизию внесённой; новой цены в концепции нет.
        progress.activeExcavation.paid = True
        return progress.activeExcavation.siteId, min(6, progress.activeExcavation.depth)

    def start(
        self,
        player: UUID,
        shift_id: ShiftId,
        key: str | None,
        target: Position | None = None,
    ) -> RunView:
        ctx = self.context(player)
        body: dict[str, object] = {
            "shiftId": shift_id,
            "target": target.model_dump() if target else None,
        }
        cached = self.cached(player, key, "start", body)
        if cached:
            assert isinstance(cached, RunView)
            return cached
        rules = self.configured_rules(ctx.profile, shift_id)
        reasons = self.reasons(ctx, rules, self.repo.active(player))
        if reasons:
            raise Match3Error(409, reasons[0].code, reasons[0].message)
        if not ctx.profile.energy.get(shift_id, False):
            rules.charges = 0
        site_id, depth = (
            self.prepare_dig(ctx.saved.progress, target)
            if shift_id == "dig"
            else (None, 0)
        )
        stream = engine.RandomStream(secrets.randbits(32) or 1)
        amphora = CellPosition(row=1 + depth, column=3) if shift_id == "dig" else None
        board = engine.create_board(rules.chips, stream.next, amphora)
        run = Run(
            rules=rules,
            board=board,
            rngState=stream.state,
            movesLeft=rules.moves,
            chargesLeft=rules.charges,
            siteId=site_id,
            digDepth=depth,
        )
        row = Match3RunRow(
            id=uuid4(),
            player_id=player,
            status="active",
            data=run.model_dump(mode="json"),
        )
        self.repo.db.add(row)
        if run.goal_reached:
            self.finalize(ctx, row, run, "goal_reached")
        self.persist(ctx, row, run)
        result = self.view(ctx, row, run)
        self.commit_response(player, key, "start", body, result)
        return result

    @staticmethod
    def check_run(run: Run, expected: int | None) -> None:
        if run.status != "active":
            raise Match3Error(409, "run_finished", "Смена уже завершена")
        if expected is not None and expected != run.revision:
            raise Match3Error(
                409, "stale_revision", "Поле уже изменилось; загрузите активную смену"
            )

    @staticmethod
    def update_depth(ctx: Context, run: Run) -> None:
        if run.rules.shiftId == "dig" and run.siteId:
            ctx.saved.progress.activeExcavation = ActiveExcavation(
                siteId=run.siteId, depth=run.digDepth, paid=True
            )

    def after_action(self, ctx: Context, row: Match3RunRow, run: Run) -> None:
        run.revision += 1
        self.update_depth(ctx, run)
        if run.goal_reached:
            self.finalize(ctx, row, run, "goal_reached")
        elif run.movesLeft == 0:
            self.finalize(ctx, row, run, "moves_exhausted")
        self.persist(ctx, row, run)

    def play(
        self, player: UUID, run_id: UUID, body: MoveRequest, key: str | None
    ) -> MoveResponse:
        ctx = self.context(player)
        data = body.model_dump(mode="json", by_alias=True)
        operation = f"move:{run_id}"
        cached = self.cached(player, key, operation, data)
        if cached:
            assert isinstance(cached, MoveResponse)
            return cached
        row, original = self.read_run(player, run_id)
        self.check_run(original, body.expectedRevision)
        moved = engine.move(original, body.from_, body.to)
        if moved.accepted:
            self.after_action(ctx, row, moved.run)
        result = MoveResponse(
            **self.view(ctx, row, moved.run).model_dump(),
            accepted=moved.accepted,
            rejectionReason=moved.rejection,
            swappedBoard=moved.swapped,
            steps=moved.steps,
        )
        self.commit_response(player, key, operation, data, result)
        return result

    def hint(self, player: UUID, run_id: UUID) -> HintResponse:
        self.context(player)
        _, run = self.read_run(player, run_id)
        options = (
            engine.possible_moves(run.board, run.amphora)
            if run.status == "active"
            else []
        )
        result = HintResponse(
            playerId=player,
            runId=run_id,
            revision=run.revision,
            available=bool(options),
            **{"from": options[0][0] if options else None},
            to=options[0][1] if options else None,
        )
        self.repo.db.commit()
        return result

    def strike(
        self,
        player: UUID,
        run_id: UUID,
        row_number: int,
        expected: int | None,
        key: str | None,
    ) -> LightningResponse:
        ctx = self.context(player)
        body: dict[str, object] = {"row": row_number, "expectedRevision": expected}
        operation = f"lightning:{run_id}"
        cached = self.cached(player, key, operation, body)
        if cached:
            assert isinstance(cached, LightningResponse)
            return cached
        row, original = self.read_run(player, run_id)
        self.check_run(original, expected)
        if original.chargesLeft < 1:
            code = "no_charges" if original.rules.charges > 0 else "no_energy"
            raise Match3Error(409, code, "Нет доступного заряда молнии")
        run, cleared, steps = engine.lightning(original, row_number)
        self.after_action(ctx, row, run)
        result = LightningResponse(
            **self.view(ctx, row, run).model_dump(), cleared=cleared, steps=steps
        )
        self.commit_response(player, key, operation, body, result)
        return result

    def finalize(
        self,
        ctx: Context,
        row: Match3RunRow,
        run: Run,
        reason: Literal["goal_reached", "moves_exhausted", "early_finish"],
    ) -> None:
        if run.result is not None:
            return
        rules = run.rules
        collected = run.collected
        bonus = rules.bonus if rules.resourceId and run.goal_reached else 0
        credited = overflow = 0
        dig_result: DigResult | None = None
        if rules.resourceId:
            amount = ctx.profile.stock.amount(rules.resourceId)
            credited = min(
                collected + bonus,
                max(0, ctx.profile.limits.amount(rules.resourceId) - amount),
            )
            overflow = collected + bonus - credited
            ctx.profile.stock.put(rules.resourceId, amount + credited)
        else:
            assert run.siteId is not None
            self.update_depth(ctx, run)
            finding: CollectionItem | None = None
            opened: Position | None = None
            if run.goal_reached:
                finding = next(
                    (
                        item
                        for item in ctx.saved.progress.collection
                        if item.siteId == run.siteId
                    ),
                    None,
                )
                if finding is None:
                    finding = CollectionItem(
                        id=f"amphora:{run.siteId}",
                        siteId=run.siteId,
                        foundAt=self.worlds.now_ms(),
                    )
                    ctx.saved.progress.collection.append(finding)
                    site = next(
                        site
                        for site in ctx.saved.progress.excavationSites
                        if site.id == run.siteId
                    )
                    known = {cell.key for cell in ctx.saved.progress.world.cells}
                    if site.key not in known:
                        q, r = site.key
                    else:
                        # Старые сохранения могли выбирать участок на открытой суше.
                        q, r = min(
                            self.frontier(ctx.saved.progress),
                            key=lambda p: (distance((p[0] - site.q, p[1] - site.r)), p),
                        )
                    ctx.saved.progress.world.cells.append(
                        generate_cell(ctx.saved.progress.world, (q, r))
                    )
                    opened = Position(q=q, r=r)
                ctx.saved.progress.activeExcavation = None
            collected = int(run.goal_reached)
            dig_result = DigResult(
                siteId=run.siteId,
                depth=run.digDepth,
                savedDepth=0 if run.goal_reached else run.digDepth,
                paid=True,
                finding=finding,
                revealedCell=opened,
            )
        ctx.saved.progress.completedShifts += 1
        run.status = "finished"
        run.result = Result(
            runId=row.id,
            shiftId=rules.shiftId,
            reason=reason,
            resourceId=rules.resourceId,
            collected=collected,
            bonus=bonus,
            total=collected + bonus,
            credited=credited,
            overflow=overflow,
            goalReached=run.goal_reached,
            warehouse=self.warehouse(ctx.profile),
            dig=dig_result,
        )
        ctx.profile.lastResult = run.result.model_copy(deep=True)

    def finish(
        self, player: UUID, run_id: UUID, expected: int | None, key: str | None
    ) -> FinishResponse:
        ctx = self.context(player)
        body: dict[str, object] = {"expectedRevision": expected}
        operation = f"finish:{run_id}"
        cached = self.cached(player, key, operation, body)
        if cached:
            assert isinstance(cached, FinishResponse)
            return cached
        row, run = self.read_run(player, run_id)
        if run.status == "active":
            self.check_run(run, expected)
            run.revision += 1
            self.finalize(ctx, row, run, "early_finish")
            self.persist(ctx, row, run)
        assert run.result is not None and ctx.saved.serverSavedAt is not None
        result = FinishResponse(
            **run.result.model_dump(),
            playerId=player,
            serverSavedAt=run.settledAt
            if run.settledAt is not None
            else ctx.saved.serverSavedAt,
        )
        self.commit_response(player, key, operation, body, result)
        return result
