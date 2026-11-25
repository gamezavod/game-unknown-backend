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
    ClientResultRequest,
    DigResult,
    ExcavationView,
    FinishResponse,
    Goal,
    HintResponse,
    LightningResponse,
    MoveBreakdown,
    MoveRequest,
    MoveResponse,
    PauseRequest,
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
from app.world.generator import CONFIG, distance, generate_cell, neighbors
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
FinishReason = Literal["goal_reached", "moves_exhausted", "early_finish"]


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
            clientManaged=run.clientManaged,
            excavationSiteId=run.siteId,
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
                if p not in known and p[1] <= CONFIG.start_radius
            },
            key=lambda p: (distance(p), p),
        )

    @staticmethod
    def site_available(progress: GameProgress) -> bool:
        return bool(progress.activeExcavation or Match3Service.frontier(progress))

    @staticmethod
    def configured_rules(
        profile: Profile, shift_id: ShiftId
    ) -> tuple[Rules, MoveBreakdown]:
        rules = RULES[shift_id].model_copy(deep=True)
        workers = profile.workers.get(shift_id, 0)
        if rules.resourceId is None:
            moves = MoveBreakdown(base=rules.moves)
        elif workers:
            moves = MoveBreakdown(
                base=10 + 5 * workers,
                forest=profile.forestMoves if shift_id == "sawmill" else 0,
                happiness=profile.happinessMoves,
            )
        else:
            moves = MoveBreakdown(base=0)
        rules.moves = moves.total
        if not profile.energy.get(shift_id, False):
            rules.charges = 0
        return rules, moves

    def state(self, player: UUID) -> StateResponse:
        ctx = self.context(player)
        active_row = self.repo.active(player)
        active = self.read_run(player, active_row.id)[1] if active_row else None
        shifts = []
        for shift_id in RULES:
            rules, move_breakdown = self.configured_rules(ctx.profile, shift_id)
            reasons = self.reasons(ctx, rules, active_row)
            shifts.append(
                ShiftView(
                    shiftId=rules.shiftId,
                    title=rules.title,
                    resourceId=rules.resourceId,
                    goal=self.goal(rules),
                    moves=rules.moves,
                    moveBreakdown=move_breakdown,
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
        elif operation.startswith(("finish:", "client-result:")):
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

    def persist_world(self, ctx: Context) -> None:
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

    def persist(self, ctx: Context, row: Match3RunRow, run: Run) -> None:
        self.persist_world(ctx)
        if run.status == "finished" and run.settledAt is None:
            run.settledAt = ctx.saved.serverSavedAt
        row.data, row.status = run.model_dump(mode="json"), run.status
        self.repo.db.flush()

    @staticmethod
    def prepare_dig(
        progress: GameProgress, target: Position | None = None
    ) -> tuple[str, int]:
        if progress.activeExcavation is not None:
            current = progress.activeExcavation
            site = next(s for s in progress.excavationSites if s.id == current.siteId)
            if target is not None and target.key != site.key:
                raise Match3Error(
                    409, "unfinished_excavation", "Сначала завершите начатый участок"
                )
        else:
            frontier = Match3Service.frontier(progress)
            if not frontier:
                raise Match3Error(409, "no_excavation_site", "Нет доступного участка")
            destination = target.key if target is not None else secrets.choice(frontier)
            if destination not in frontier:
                raise Match3Error(
                    400, "invalid_target", "Выберите соседний участок тумана"
                )
            q, r = destination
            site_id = f"dig:{q}:{r}"
            if any(site.id == site_id for site in progress.excavationSites):
                site_id = f"dig:{uuid4()}"
            site = ExcavationSite(id=site_id, q=q, r=r)
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
        rules, _ = self.configured_rules(ctx.profile, shift_id)
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
    def check_server_mode(run: Run) -> None:
        if run.clientManaged:
            raise Match3Error(
                409, "client_managed", "Ходы этой смены считаются в браузере"
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
        self.check_server_mode(original)
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
        self.check_server_mode(run)
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
        self.check_server_mode(original)
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

    def dig_result(
        self, progress: GameProgress, site_id: str, depth: int, pick_random: bool
    ) -> DigResult:
        active = progress.activeExcavation
        assert active and active.siteId == site_id
        active.depth = max(active.depth, depth)
        finding: CollectionItem | None = None
        opened: Position | None = None
        if depth >= 6:
            finding = next(
                (item for item in progress.collection if item.siteId == site_id), None
            )
            if finding is None:
                site = next(
                    site for site in progress.excavationSites if site.id == site_id
                )
                known = {cell.key for cell in progress.world.cells}
                reserved = {
                    other.key
                    for other in progress.excavationSites
                    if other.id != site_id
                }
                candidates = [p for p in self.frontier(progress) if p not in reserved]
                if candidates:
                    q, r = (
                        secrets.choice(candidates)
                        if pick_random or site.key not in candidates
                        else site.key
                    )
                    if site.key not in known:
                        site.q, site.r = q, r
                    progress.world.cells.append(generate_cell(progress.world, (q, r)))
                    opened = Position(q=q, r=r)
                elif site.key not in known:
                    raise Match3Error(
                        409, "no_excavation_site", "Нет доступного участка"
                    )
                finding = CollectionItem(
                    id=f"amphora:{site_id}",
                    siteId=site_id,
                    foundAt=self.worlds.now_ms(),
                )
                progress.collection.append(finding)
            progress.activeExcavation = None
        return DigResult(
            siteId=site_id,
            depth=depth,
            savedDepth=0 if depth >= 6 else active.depth,
            paid=active.paid,
            finding=finding,
            revealedCell=opened,
        )

    def apply_result(
        self,
        ctx: Context,
        run_id: UUID,
        rules: Rules,
        collected: int,
        depth: int,
        site_id: str | None,
        reason: FinishReason,
        *,
        pick_random: bool = False,
    ) -> Result:
        reached = reason == "goal_reached"
        bonus = rules.bonus if rules.resourceId and reached else 0
        credited = overflow = 0
        dig: DigResult | None = None
        if rules.resourceId:
            amount = ctx.profile.stock.amount(rules.resourceId)
            credited = min(
                collected + bonus,
                max(0, ctx.profile.limits.amount(rules.resourceId) - amount),
            )
            overflow = collected + bonus - credited
            ctx.profile.stock.put(rules.resourceId, amount + credited)
        else:
            assert site_id is not None
            dig = self.dig_result(ctx.saved.progress, site_id, depth, pick_random)
            collected = int(reached)
        ctx.saved.progress.completedShifts += 1
        result = Result(
            runId=run_id,
            shiftId=rules.shiftId,
            reason=reason,
            resourceId=rules.resourceId,
            collected=collected,
            bonus=bonus,
            total=collected + bonus,
            credited=credited,
            overflow=overflow,
            goalReached=reached,
            warehouse=self.warehouse(ctx.profile),
            dig=dig,
        )
        ctx.profile.lastResult = result
        return result

    def finalize(
        self, ctx: Context, row: Match3RunRow, run: Run, reason: FinishReason
    ) -> None:
        if run.result is None:
            run.result = self.apply_result(
                ctx, row.id, run.rules, run.collected, run.digDepth, run.siteId, reason
            )
            run.status = "finished"

    def validate_client_update(
        self, ctx: Context, player: UUID, body: ClientResultRequest
    ) -> tuple[Rules, Match3RunRow | None, Run | None, str | None, bool]:
        progress = ctx.saved.progress
        active_row = self.repo.active(player)
        saved_run = None
        if body.legacyRevision is not None:
            if not active_row or active_row.id != body.runId:
                raise Match3Error(409, "stale_revision", "Состояние смены изменилось")
            _, saved_run = self.read_run(player, active_row.id)
            self.check_run(saved_run, body.legacyRevision)
            if body.shiftId != saved_run.rules.shiftId:
                raise Match3Error(422, "invalid_result", "Неверный тип смены")
            rules = saved_run.rules
        else:
            if self.repo.run(player, body.runId):
                raise Match3Error(409, "stale_revision", "Смена уже сохранена")
            rules, _ = self.configured_rules(ctx.profile, body.shiftId)
            reasons = self.reasons(ctx, rules, active_row)
            for unavailable in reasons:
                if unavailable.code != "warehouse_full":
                    raise Match3Error(409, unavailable.code, unavailable.message)

        moves = saved_run.movesLeft if saved_run else rules.moves
        charges = saved_run.chargesLeft if saved_run else rules.charges
        collected = saved_run.collected if saved_run else 0
        spent = moves - body.movesLeft + charges - body.chargesLeft
        if (
            body.movesLeft > moves
            or body.chargesLeft > charges
            or not collected <= body.collected <= collected + spent * 64 * 40
        ):
            raise Match3Error(422, "invalid_result", "Неверные ходы, заряды или добыча")

        site_id = None
        if body.shiftId == "dig":
            active = progress.activeExcavation
            current_id = active.siteId if active else None
            depth = min(6, active.depth) if active else 0
            if current_id != body.excavationSiteId or depth != body.digStartDepth:
                raise Match3Error(
                    409, "excavation_changed", "Участок раскопок изменился"
                )
            if (
                body.collected
                or body.digDepth < depth
                or (spent == 0 and body.digDepth != depth)
            ):
                raise Match3Error(422, "invalid_result", "Неверная глубина раскопок")
            site_id, _ = self.prepare_dig(progress)
            reached = body.digDepth >= 6
        else:
            if (
                body.digDepth
                or body.digStartDepth
                or body.excavationSiteId
                or body.collected >= rules.target + 64
            ):
                raise Match3Error(422, "invalid_result", "Неверная добыча смены")
            reached = body.collected >= rules.target

        return rules, active_row, saved_run, site_id, reached

    def pause_client_run(self, player: UUID, body: PauseRequest) -> RunView:
        ctx = self.context(player)
        operation = f"client-pause:{body.runId}:{body.legacyRevision}"
        data = body.model_dump(mode="json")
        cached = self.cached(player, operation, operation, data)
        if cached:
            assert isinstance(cached, RunView)
            return cached

        if self.repo.receipt(player, f"client-result:{body.runId}"):
            raise Match3Error(409, "run_finished", "Смена уже завершена")

        rules, active_row, saved_run, site_id, reached = self.validate_client_update(
            ctx, player, body
        )
        if reached or body.movesLeft == 0:
            raise Match3Error(
                422, "invalid_pause", "Завершённая смена сохраняется как итог"
            )
        if any(chip not in rules.chips for row in body.board for chip in row):
            raise Match3Error(422, "invalid_pause", "Фишка не относится к этой смене")

        run = Run(
            rules=rules,
            board=body.board,
            movesLeft=body.movesLeft,
            chargesLeft=body.chargesLeft,
            collected=body.collected,
            digDepth=body.digDepth,
            siteId=site_id,
            revision=saved_run.revision + 1 if saved_run else 1,
            clientManaged=True,
        )
        if active_row is None:
            active_row = Match3RunRow(
                id=body.runId, player_id=player, status="active", data={}
            )
            self.repo.db.add(active_row)
        self.update_depth(ctx, run)
        self.persist(ctx, active_row, run)
        view = self.view(ctx, active_row, run)
        self.commit_response(player, operation, operation, data, view)
        return view

    def save_client_result(
        self, player: UUID, body: ClientResultRequest
    ) -> FinishResponse:
        ctx = self.context(player)
        operation = f"client-result:{body.runId}"
        data = body.model_dump(mode="json")
        cached = self.cached(player, operation, operation, data)
        if cached:
            assert isinstance(cached, FinishResponse)
            return cached

        rules, active_row, saved_run, site_id, reached = self.validate_client_update(
            ctx, player, body
        )
        reason: FinishReason = (
            "goal_reached"
            if reached
            else "moves_exhausted"
            if body.movesLeft == 0
            else "early_finish"
        )
        result = self.apply_result(
            ctx,
            body.runId,
            rules,
            body.collected,
            body.digDepth,
            site_id,
            reason,
            pick_random=body.shiftId == "dig" and body.excavationSiteId is not None,
        )
        if active_row and saved_run:
            saved_run.revision += 1
            saved_run.movesLeft = body.movesLeft
            saved_run.chargesLeft = body.chargesLeft
            saved_run.collected = body.collected
            saved_run.digDepth = body.digDepth
            saved_run.result = result
            saved_run.status = "finished"
            self.persist(ctx, active_row, saved_run)
        else:
            self.persist_world(ctx)
        assert ctx.saved.serverSavedAt is not None
        response = FinishResponse(
            **result.model_dump(),
            playerId=player,
            serverSavedAt=ctx.saved.serverSavedAt,
        )
        self.commit_response(player, operation, operation, data, response)
        return response

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
            self.check_server_mode(run)
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
