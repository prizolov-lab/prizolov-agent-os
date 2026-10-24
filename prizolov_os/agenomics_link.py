# Prizolov Agent OS 0.3.0 | Author: Dm.Andreyanov | Brand: Prizolov Lab | © 2026
# SPDX-FileCopyrightText: 2026 Dm.Andreyanov / Prizolov Lab
# SPDX-License-Identifier: Apache-2.0

"""Связь с Agenomics: реальные задачи агентов как доказательства и исходы.

Необязательный модуль. Включается переменной AGENOMICS_EVIDENCE=1 и только
если установлен пакет agenomics (>=0.9.6). Без них ничего не делает и ничего
не меняет в работе агентов; любая ошибка здесь логируется и не прерывает задачу.

Что записывается (отдельная база, по умолчанию agenomics_evidence.db рядом с
данными; путь AGENOMICS_EVIDENCE_DB):

1. Перед задачей: наблюдение с Trust Score ядра (только из прошлых задач) и
   две замороженные цели: runtime_failure и task_failure.
2. После задачи: runtime_failure (Q1) = задача не завершена или инструмент
   вернул ошибку; task_failure по критику (Q3, LLM-критик, если самопроверка
   включена): оценка ниже порога.
3. Когда человек оценивает ответ (Kernel.feedback): task_failure (Q4,
   независимая группа human_review). Текст замечания в базу НЕ пишется,
   только «нравится» или «не нравится».

Что здесь НЕ делается: подгонка весов и порогов. Калибровка идёт по
отдельному протоколу в Agenomics (docs/CALIBRATION_PROTOCOL.md) и только на
накопленных данных; этот модуль только собирает.
"""

import hashlib
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    from agenomics import (AgentGenome, EvidenceStore, ExternalVerification, TrustScorer,
                           record_external_outcome)
except ImportError:  # agenomics не установлен
    AgentGenome = EvidenceStore = ExternalVerification = TrustScorer = record_external_outcome = None

AGENT_ID = "prizolov-os-director"
ENVIRONMENT_ID = "agent-os/kernel"
SOURCE = "prizolov-agent-os/kernel"
CRITIC_THRESHOLD = 7
ROLLING = 200
MIN_FOR_CALIBRATION = 10

RUNTIME_DONOR = dict(donor_id="agent_os.runtime", donor_type="outcome", name="Ядро: завершение задачи и ошибки инструментов",
                     independence_group="runtime", provider="prizolov-agent-os")
CRITIC_DONOR = dict(donor_id="agent_os.critic", donor_type="outcome", name="LLM-критик ядра (самопроверка)",
                    independence_group="llm_critic", provider="prizolov-agent-os")
HUMAN_DONOR = dict(donor_id="agent_os.human_review", donor_type="outcome", name="Оценка ответа человеком",
                   independence_group="human_review", provider="owner")


def enabled() -> bool:
    return os.environ.get("AGENOMICS_EVIDENCE", "") == "1" and EvidenceStore is not None


def evidence_db_path() -> str:
    return os.environ.get("AGENOMICS_EVIDENCE_DB") or "agenomics_evidence.db"


class _HumanVerifier:
    donor_id = HUMAN_DONOR["donor_id"]

    def __init__(self, positive: bool, reference: str, verified_at: datetime) -> None:
        self.positive, self.reference, self.verified_at = positive, reference, verified_at

    def verify(self, prediction: Any) -> Any:
        return ExternalVerification(
            occurred=not self.positive, outcome_type="task_failure", source_type="human_review",
            source_reference=self.reference, verification_method="manual_review", verified_at=self.verified_at)


class AgenomicsLink:
    """Подписывается на события ядра и записывает доказательства. Потокобезопасна."""

    def __init__(self, events: Any, model: str = "", self_check: str = "off", db_path: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._db_path = db_path or evidence_db_path()
        self._model, self._self_check = model, self_check
        self._tool_errors = 0
        self._review_score: Optional[int] = None
        self._task_no = 0
        self._current: Dict[str, int] = {}      # цель -> id предсказания текущей задачи
        self._last: Dict[str, int] = {}         # то же для последней завершённой задачи
        self._last_ref = ""
        self._recent: List[bool] = []           # исходы runtime последних задач (True = сбой)
        if events is not None:
            events.subscribe(self._on_event)

    @classmethod
    def from_env(cls, events: Any, model: str = "", self_check: str = "off") -> Optional["AgenomicsLink"]:
        return cls(events, model, self_check) if enabled() else None

    # --- события ядра ----------------------------------------------------------
    def _on_event(self, event: Any) -> None:
        try:
            if event.type == "tool_result" and event.data.get("is_error"):
                self._tool_errors += 1
            elif event.type == "self_check":
                score = event.data.get("score")
                self._review_score = score if isinstance(score, int) else None
        except Exception:
            logger.exception("Agenomics: событие не обработано")

    # --- Trust Score только из прошлого ---------------------------------------
    def _trust(self) -> Any:
        rate = (sum(self._recent[-ROLLING:]) / len(self._recent[-ROLLING:])) if len(self._recent) >= MIN_FOR_CALIBRATION else None
        genome = AgentGenome(
            id=AGENT_ID, domain="business", autonomy="advisory", transparency=80.0,
            bias_control=70.0, data_safety=85.0,
            drift_rate=0.05 if rate is None else round(min(rate, 1.0), 4), has_ledger=False)
        return TrustScorer().score(genome)

    def _config_hash(self) -> str:
        return hashlib.sha256(f"{AGENT_ID}|{self._model}|{self._self_check}".encode()).hexdigest()[:16]

    # --- этапы задачи ---------------------------------------------------------
    def start(self, task_ref: str) -> None:
        """Перед задачей: наблюдение и замороженные цели. Ошибка не прерывает задачу."""
        try:
            with self._lock:
                self._tool_errors, self._review_score, self._current = 0, None, {}
                self._task_no += 1
                store = EvidenceStore(self._db_path)
                try:
                    now = datetime.now(timezone.utc)
                    trust = self._trust()
                    obs = store.record_observation(
                        AGENT_ID, trust.score, trust.label, genome_hash=self._config_hash(),
                        model_version=self._model or None, source=SOURCE, timestamp=now)
                    for target in ("runtime_failure", "task_failure"):
                        self._current[target] = store.record_prediction(
                            obs, target, horizon="current_run", frozen_at=now,
                            task_version=f"os:{task_ref}", environment_id=ENVIRONMENT_ID)
                finally:
                    store.close()
        except Exception:
            logger.exception("Agenomics: заморозка не удалась; задача продолжается")
            self._current = {}

    def finish(self, completed: bool) -> None:
        """После задачи: исходы runtime (Q1) и критика (Q3)."""
        try:
            with self._lock:
                if not self._current:
                    return
                store = EvidenceStore(self._db_path)
                try:
                    store.register_donor(**RUNTIME_DONOR)
                    store.register_donor(**CRITIC_DONOR)
                    ref = f"os-task:{self._task_no}"
                    failed = (not completed) or self._tool_errors > 0
                    store.record_outcome(self._current["runtime_failure"], RUNTIME_DONOR["donor_id"], "execution_error",
                                         occurred=failed, outcome_class="RUNTIME", quality_level="Q1", source_reference=ref,
                                         details=f"completed={completed}; tool_errors={self._tool_errors}")
                    self._recent.append(failed)
                    if self._review_score is not None:
                        store.record_outcome(self._current["task_failure"], CRITIC_DONOR["donor_id"], "task_failure",
                                             occurred=self._review_score < CRITIC_THRESHOLD, outcome_class="TASK",
                                             quality_level="Q3", source_reference=f"{ref}:critic",
                                             details=f"critic_score={self._review_score}")
                finally:
                    store.close()
                self._last, self._last_ref, self._current = dict(self._current), f"os-task:{self._task_no}", {}
        except Exception:
            logger.exception("Agenomics: исходы не записаны; задача не затронута")

    def human(self, positive: bool) -> None:
        """Оценка человека последнего ответа: Q4 для task_failure. Комментарий не сохраняется."""
        try:
            with self._lock:
                pred = self._last.get("task_failure")
                if pred is None:
                    return
                store = EvidenceStore(self._db_path)
                try:
                    store.register_donor(**HUMAN_DONOR)
                    record_external_outcome(store, pred, _HumanVerifier(
                        positive, f"{self._last_ref}:human", datetime.now(timezone.utc)))
                finally:
                    store.close()
        except Exception:
            logger.exception("Agenomics: оценка человека не записана")
