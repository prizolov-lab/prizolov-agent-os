# Prizolov Agent OS 0.3.0 | Author: Dm.Andreyanov | Brand: Prizolov Lab | © 2026
# SPDX-FileCopyrightText: 2026 Dm.Andreyanov / Prizolov Lab
# SPDX-License-Identifier: Apache-2.0

"""Ядро системы: Директор, специалисты, память и самосовершенствование."""

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .. import events as ev
from ..agenomics_link import AgenomicsLink
from ..agent import Agent, AgentResult, add_usage
from ..agents import create_specialists
from ..budget import Budget, MeteredLLM
from ..config import settings
from ..events import EventBus
from ..forecasting import ASSET_CLASSES, Forecast, ForecastEngine, ForecastJournal
from ..forecasting.calibration import MIN_SAMPLES
from ..forecasting.journal import GIVE_UP_DAYS
from ..improvement import (
    Critic,
    LessonExtractor,
    PromptImprover,
    PromptProposal,
    Review,
    build_tool,
    format_lessons,
    propose_tool_tool,
    revision_request,
)
from ..knowledge import KnowledgeBase
from ..llm import LLMClient, create_client
from ..market import MarketData, MarketDataError
from ..memory import APPROVED, PENDING, REJECTED, CustomToolRecord, Lesson, PromptVersion, Store
from ..quality import QualityMonitor
from ..scheduler import ScheduleStore, report_payload
from ..tools import Approver, knowledge_tool, memory_tools, serialized
from ..tracing import Tracer
from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

SELF_CHECK_MODES = ("complex", "always", "off")
# Сколько новых уроков должно накопиться, чтобы предложить улучшить промпт.
PROMPT_IMPROVEMENT_THRESHOLD = 5
# Если в 80%-й интервал попадает меньше этой доли сверенных прогнозов, аналитик получает урок.
LOW_PASS_RATE = 0.6
# Факт ищем не дальше стольких дней до даты прогноза (выходные, праздники).
FACT_LOOKBACK_DAYS = 7


@dataclass
class VerificationReport:
    """Итог сверки прогнозов с фактом."""

    verified: List[Forecast] = field(default_factory=list)
    unverifiable: int = 0
    errors: List[str] = field(default_factory=list)


class Kernel:
    """Ядро Prizolov OS.

    Пример:
        kernel = Kernel.create(approver=ask_user)
        print(kernel.chat("Спрогнозируй курс доллара на месяц").text)
        kernel.feedback(positive=False, comment="Не указал источник данных")
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        store: Optional[Store] = None,
        llm: Optional[LLMClient] = None,
        self_check: str = "off",
        session_id: Optional[str] = None,
        market: Optional[MarketData] = None,
        budget: Optional[Budget] = None,
        tracer: Optional[Tracer] = None,
        knowledge: Optional[KnowledgeBase] = None,
        workspace_dir: Optional[Union[str, Path]] = None,
    ) -> None:
        if self_check not in SELF_CHECK_MODES:
            raise ValueError(f"self_check должен быть одним из {SELF_CHECK_MODES}")
        self.orchestrator: Orchestrator = orchestrator
        self.store: Store = store or Store()
        self.mode: str = "standard"
        self.self_check = self_check
        self.session_id: str = session_id or _new_session_id()
        self.last_review: Optional[Review] = None
        self.events = EventBus()
        self.orchestrator.events = self.events
        self.market = market or MarketData()
        self.forecasts = ForecastJournal(self.store)
        self.quality = QualityMonitor(self.store, self.events)
        self.budget = budget
        self.tracer = tracer
        self.knowledge = knowledge
        self.workspace_dir = Path(workspace_dir or settings.workspace_dir)
        self.schedules = ScheduleStore(self.store, settings.timezone)
        # Telegram-чат, из которого работает ядро (для доставки задач по расписанию).
        self.chat_id: Optional[int] = None
        self.created_at = time.time()
        self._spawn_args: Optional[Dict[str, Any]] = None
        if tracer is not None:
            tracer.attach(self.events)
        self._llm = llm
        self._last: Optional[Tuple[str, str]] = None
        # Необязательная связь с Agenomics (AGENOMICS_EVIDENCE=1); без неё None.
        self.agenomics = AgenomicsLink.from_env(self.events, getattr(settings, "model", "") or "", self_check)
        self._task_counter = 0
        # Исходные промпты из кода: к ним возвращает откат версий.
        self._default_prompts = {name: a.system_prompt for name, a in self.agents.items()}
        self._setup_agents()
        logger.info("Kernel initialized with orchestrator")

    @classmethod
    def create(
        cls,
        *,
        llm: Optional[LLMClient] = None,
        approver: Optional[Approver] = None,
        workspace_dir: Optional[Union[str, Path]] = None,
        market: Optional[MarketData] = None,
        store: Optional[Store] = None,
        self_check: Optional[str] = None,
        session_id: Optional[str] = None,
        trace: Optional[bool] = None,
    ) -> "Kernel":
        """Собирает ядро со всеми специалистами по настройкам.

        Все агенты получают один LLM-клиент с учётом стоимости и лимитами расходов.
        """
        store = store or Store(settings.db_path)
        market = market or MarketData()
        budget = Budget(store, settings.budget_task_usd, settings.budget_day_usd)
        metered = MeteredLLM(llm or create_client(), budget)
        approver = serialized(approver)
        engine = ForecastEngine(ForecastJournal(store))
        knowledge = KnowledgeBase(store, Path(workspace_dir or settings.workspace_dir))
        specialists = create_specialists(
            metered, workspace_dir, approver, market, engine, knowledge, store
        )
        trace = settings.trace if trace is None else trace
        kernel = cls(
            Orchestrator(specialists, llm=metered, approver=approver),
            store=store,
            llm=metered,
            self_check=self_check or settings.self_check,
            session_id=session_id,
            market=market,
            budget=budget,
            tracer=Tracer(settings.trace_dir, settings.trace_content) if trace else None,
            knowledge=knowledge,
            workspace_dir=workspace_dir,
        )
        kernel._spawn_args = dict(
            llm=llm, workspace_dir=workspace_dir, market=market, store=store,
            self_check=self_check, trace=trace,
        )
        if kernel.tracer is not None:
            kernel.tracer._session = lambda: kernel.session_id
        return kernel

    @property
    def agents(self) -> Dict[str, Agent]:
        """Все агенты: Директор и специалисты."""
        return {"director": self.orchestrator.director, **self.orchestrator.specialists}

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = create_client()
        return self._llm

    # --- Задачи и диалог -----------------------------------------------------

    def run(self, task: str) -> AgentResult:
        """Решает задачу с чистого листа (без сохранения диалога)."""
        logger.info(f"Starting task execution: {str(task)[:50]}")
        self._start_task()
        result = self.orchestrator.run(task)
        return self._after_answer(task, result)

    def chat(self, message: str) -> AgentResult:
        """Продолжает диалог и сохраняет его в памяти."""
        self._start_task()
        result = self.orchestrator.chat(message)
        result = self._after_answer(message, result)
        history = self.orchestrator.director.history
        if history:
            self.store.save_session(self.session_id, history, title=message[:80])
        return result

    def spawn(self) -> "Kernel":
        """Новое ядро с теми же настройками и чистым диалогом (для задач по расписанию).

        Без подтверждающего человека: запись файлов в таких задачах запрещена.
        """
        if self._spawn_args is None:
            raise RuntimeError("spawn() доступен только для ядра из Kernel.create()")
        return Kernel.create(**self._spawn_args)

    def _start_task(self) -> None:
        if self.budget is not None:
            self.budget.start_task()
        if self.agenomics is not None:
            self._task_counter += 1
            self.agenomics.start(f"{self.session_id}:{self._task_counter}")

    def reset(self) -> None:
        """Начинает новый диалог (старый остаётся в памяти)."""
        self.orchestrator.reset()
        self.session_id = _new_session_id()
        self._last = None

    def resume(self, session_id: str) -> int:
        """Продолжает сохранённый диалог. Возвращает число сообщений в нём."""
        messages = self.store.load_session(session_id)
        if messages is None:
            raise ValueError(f"Диалог '{session_id}' не найден")
        self.orchestrator.director.history = messages
        self.session_id = session_id
        return len(messages)

    def get_status(self) -> Dict[str, Any]:
        """Текущий статус ядра."""
        return {
            "mode": self.mode,
            "orchestrator_active": self.orchestrator is not None,
            "specialists": list(self.orchestrator.specialists),
            "delegations": len(self.orchestrator.execution_log),
            "session_id": self.session_id,
            "self_check": self.self_check,
            "facts": len(self.store.list_facts()),
            "lessons": len(self.store.list_lessons()),
            "pending_tools": len(self.store.list_custom_tools(PENDING)),
            "forecasts": self.forecasts.counts(),
            "knowledge": self.knowledge.stats() if self.knowledge else None,
        }

    # --- Самопроверка и уроки ------------------------------------------------

    def _after_answer(self, task: str, result: AgentResult) -> AgentResult:
        self.last_review = None
        if self._should_check(result):
            result = self._self_check(task, result)
        self._last = (task, result.text)
        if self.agenomics is not None:
            self.agenomics.finish(result.completed)
        return result

    def _should_check(self, result: AgentResult) -> bool:
        if not result.completed or self.self_check == "off":
            return False
        return self.self_check == "always" or bool(self.orchestrator.last_run_delegations)

    def _self_check(self, task: str, result: AgentResult) -> AgentResult:
        critic = Critic(self.llm)
        evidence = [
            f"[{d.agent}] Поручение: {d.task}\nРезультат: {d.result or d.error}"
            for d in self.orchestrator.last_run_delegations
        ]
        review = critic.review(task, result.text, evidence)
        self.last_review = review
        add_usage(result.usage, review.usage)
        logger.info(f"Self-check score: {review.score}")
        self.events.emit(ev.SELF_CHECK, "critic", score=review.score, issues=review.issues,
                         usage=review.usage)
        if not critic.needs_revision(review):
            return result
        self.events.emit(ev.REVISION, "director", issues=review.issues)

        if review.lesson:
            self.store.add_lesson("director", review.lesson, source="critic")
        revised = self.orchestrator.followup(result.messages, revision_request(review))
        add_usage(revised.usage, result.usage)
        revised.tool_results = result.tool_results + revised.tool_results
        revised.iterations += result.iterations
        return revised

    def feedback(self, positive: bool, comment: str = "") -> Optional[Lesson]:
        """Оценка последнего ответа. Из замечания формируется урок для агента."""
        if self._last is None:
            raise ValueError("Ещё нет ответа, который можно оценить")
        if self.agenomics is not None:
            self.agenomics.human(positive)
        if positive and not comment.strip():
            self.quality.record_feedback("director", True)
            return None
        task, answer = self._last
        extracted = LessonExtractor(self.llm).from_feedback(
            task, answer, positive, comment, list(self.agents)
        )
        if extracted is None:
            return None
        self.quality.record_feedback(extracted.agent, positive)
        source = "feedback+" if positive else "feedback-"
        lesson_id = self.store.add_lesson(extracted.agent, extracted.text, source)
        return next(item for item in self.store.list_lessons() if item.id == lesson_id)

    # --- Сверка прогнозов ----------------------------------------------------

    def verify_forecasts(self, today: Optional[date] = None) -> VerificationReport:
        """Сверяет наступившие рыночные прогнозы с фактическими ценами."""
        today = today or date.today()
        report = VerificationReport()
        due = [f for f in self.forecasts.due(today) if f.kind == "market"]
        by_asset: Dict[Tuple[str, str], List[Forecast]] = {}
        for forecast in due:
            if forecast.source == "csv":
                # Цены из файла пользователя сверить автоматически нельзя.
                if (today - forecast.target_date).days > GIVE_UP_DAYS:
                    self.forecasts.mark_unverifiable(forecast.id)
                    report.unverifiable += 1
                continue
            by_asset.setdefault((forecast.source, forecast.symbol), []).append(forecast)

        for (source, symbol), forecasts in by_asset.items():
            oldest = min(f.target_date for f in forecasts)
            days = max((today - oldest).days + FACT_LOOKBACK_DAYS + 3, 10)
            try:
                series = self.market.history(source, symbol, days)
            except MarketDataError as e:
                report.errors.append(f"{symbol} ({source}): {e}")
                continue
            prices = dict(zip(series.dates, series.closes))
            for forecast in forecasts:
                actual = _price_on(prices, forecast.target_date)
                if actual is not None:
                    report.verified.append(self.forecasts.mark_verified(forecast, actual))
                elif (today - forecast.target_date).days > GIVE_UP_DAYS:
                    self.forecasts.mark_unverifiable(forecast.id)
                    report.unverifiable += 1
        self._accuracy_lessons({(f.asset_class, f.bucket) for f in report.verified})
        return report

    def _accuracy_lessons(self, groups: Any) -> None:
        """Урок аналитику, если прогнозы по классу активов часто не сбываются."""
        for asset_class, bucket in groups:
            accuracy = self.forecasts.live_accuracy(asset_class, bucket)
            if accuracy.n < MIN_SAMPLES or (accuracy.pass_rate_80 or 0) >= LOW_PASS_RATE:
                continue
            name = ASSET_CLASSES.get(asset_class, asset_class)
            marker = f"Прогнозы ({name}, горизонт {bucket})"
            if any(lesson.text.startswith(marker) for lesson in
                   self.store.list_lessons("market_analyst")):
                continue
            self.store.add_lesson(
                "market_analyst",
                f"{marker} часто не сбываются: в 80%-й интервал попало только "
                f"{(accuracy.pass_rate_80 or 0) * 100:.0f}% из {accuracy.n}. Подчёркивай высокую "
                "неопределённость и не делай уверенных выводов по таким прогнозам.",
                "forecast_accuracy",
            )

    # --- Версии промптов -----------------------------------------------------

    def improvement_candidates(self) -> List[Tuple[str, int]]:
        """Агенты, у которых накопилось достаточно новых уроков для улучшения промпта."""
        counts = [(name, self.store.lessons_since_last_prompt(name)) for name in self.agents]
        return [(name, n) for name, n in counts if n >= PROMPT_IMPROVEMENT_THRESHOLD]

    def propose_prompt(self, agent: str) -> Tuple[PromptVersion, PromptProposal]:
        """Просит модель встроить уроки в промпт агента. Промпт меняется только после
        approve_prompt()."""
        target = self._agent(agent)
        proposal = PromptImprover(self.llm).propose(
            agent, target.system_prompt, self.store.list_lessons(agent)
        )
        if proposal is None:
            raise RuntimeError("Модель не смогла предложить новую версию промпта")
        note = "; ".join(proposal.changes)
        version_id = self.store.propose_prompt(agent, proposal.prompt, note)
        return self.store.get_prompt_version(version_id), proposal  # type: ignore[return-value]

    def approve_prompt(self, version_id: int) -> PromptVersion:
        version = self.store.activate_prompt(version_id)
        self._agent(version.agent).system_prompt = version.prompt
        return version

    def reject_prompt(self, version_id: int) -> None:
        self.store.reject_prompt(version_id)

    def rollback_prompt(self, agent: str) -> Optional[PromptVersion]:
        """Возвращает предыдущую версию промпта (None - исходный промпт из кода)."""
        previous = self.store.rollback_prompt(agent)
        self._agent(agent).system_prompt = (
            previous.prompt if previous else self._default_prompts[agent]
        )
        return previous

    # --- Инструменты, предложенные агентами ----------------------------------

    def pending_tools(self) -> List[CustomToolRecord]:
        return self.store.list_custom_tools(PENDING)

    def approve_tool(self, name: str) -> CustomToolRecord:
        """Подключает инструмент после того, как человек проверил его код."""
        record = self.store.get_custom_tool(name)
        if record is None or record.status != PENDING:
            raise ValueError(f"Нет инструмента '{name}', ожидающего проверки")
        director = self.orchestrator.director
        if name in director.tools:
            raise ValueError(f"Инструмент '{name}' уже есть")
        director.tools.add(build_tool(record))
        self.store.set_custom_tool_status(name, APPROVED)
        return record

    def reject_tool(self, name: str) -> None:
        self.store.set_custom_tool_status(name, REJECTED)

    # --- Настройка агентов ---------------------------------------------------

    def _setup_agents(self) -> None:
        director = self.orchestrator.director
        for tool in memory_tools(self.store):
            director.tools.add(tool)
        director.tools.add(propose_tool_tool(self.store, lambda: director.tools.names()))
        if self.knowledge is not None:
            director.tools.add(knowledge_tool(self.knowledge))
        director.tools.add(self._schedule_tool())
        for record in self.store.list_custom_tools(APPROVED):
            if record.name not in director.tools:
                director.tools.add(build_tool(record))

        director.compact_history = settings.compact_at > 0
        director.parallel_tools = {"delegate"}
        director.max_parallel = settings.parallel
        for name, agent in self.agents.items():
            active = self.store.active_prompt(name)
            if active:
                agent.system_prompt = active.prompt
            agent.context_provider = self._lessons_provider(name)

    def _schedule_tool(self) -> Any:
        from ..tools import Tool, make_schema

        def schedule_task(schedule: str, kind: str, task: str, symbol: str, horizons: str) -> str:
            if kind == "report":
                if not symbol.strip():
                    raise ValueError("Для отчёта нужен symbol (например GOLD или GC=F)")
                payload = report_payload(symbol, _parse_horizons(horizons or "1,7,15,30"))
            else:
                payload = task
            created = self.schedules.add(schedule, kind, payload, chat_id=self.chat_id)
            return (
                f"Задача #{created.id} создана: {created.title}; расписание: {created.schedule}; "
                f"следующий запуск {created.next_run:%d.%m.%Y %H:%M} UTC. "
                "Управление: /schedule."
            )

        return Tool(
            name="schedule_task",
            description=(
                "Создаёт регулярную задачу по расписанию, когда пользователь просит делать "
                "что-то регулярно («присылай каждое утро обзор рынка»). schedule - по-русски "
                "(«ежедневно 09:00», «по будням 9:30», «по понедельникам 10:00», «каждые 6 "
                "часов») или cron. kind=report - отчёт по активу без участия модели (бесплатно; "
                "заполни symbol и horizons); kind=task - поручение агентам (task - полная "
                "постановка; тратит бюджет при каждом запуске). Пользователь подтверждает "
                "создание."
            ),
            input_schema=make_schema({
                "schedule": {"type": "string"},
                "kind": {"type": "string", "enum": ["task", "report"]},
                "task": {"type": "string", "description": "Для task - что делать; иначе ''"},
                "symbol": {"type": "string", "description": "Для report - тикер; иначе ''"},
                "horizons": {"type": "string", "description": "Для report, например 1,7,15,30"},
            }),
            handler=schedule_task,
            requires_approval=True,
        )

    def _lessons_provider(self, agent: str) -> Callable[[str], str]:
        def provide(query: str) -> str:
            return format_lessons(self.store.relevant_lessons(agent, query))

        return provide

    def _agent(self, name: str) -> Agent:
        agent = self.agents.get(name)
        if agent is None:
            raise ValueError(f"Нет агента '{name}'. Доступны: {', '.join(self.agents)}")
        return agent


def _parse_horizons(text: str) -> List[int]:
    try:
        values = sorted({int(h) for h in text.replace(" ", "").split(",") if h})
    except ValueError:
        raise ValueError("horizons: числа через запятую, например 1,7,15,30") from None
    if not values or not all(1 <= h <= 365 for h in values):
        raise ValueError("Горизонты - от 1 до 365 дней")
    return values


def _price_on(prices: Dict[date, float], target: date) -> Optional[float]:
    """Цена на дату или ближайшую предыдущую (не раньше чем за неделю)."""
    for offset in range(FACT_LOOKBACK_DAYS + 1):
        day = target - timedelta(days=offset)
        if day in prices:
            return prices[day]
    return None


def _new_session_id() -> str:
    return uuid.uuid4().hex[:12]
