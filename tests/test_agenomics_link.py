# Prizolov Agent OS 0.3.0 | Author: Dm.Andreyanov | Brand: Prizolov Lab | © 2026
# SPDX-FileCopyrightText: 2026 Dm.Andreyanov / Prizolov Lab
# SPDX-License-Identifier: Apache-2.0

"""Связь с Agenomics: заморозка до задачи, исходы после, оценка человека."""

import pytest

from prizolov_os import agenomics_link as al
from prizolov_os.events import EventBus

pytestmark = pytest.mark.skipif(al.EvidenceStore is None, reason="нужен agenomics >= 0.9.6")


def _link(tmp_path):
    bus = EventBus()
    return bus, al.AgenomicsLink(
        bus, model="m", self_check="always", db_path=str(tmp_path / "e.db")
    )


def _preds(tmp_path):
    store = al.EvidenceStore(str(tmp_path / "e.db"))
    try:
        return {p.target: p for p in store.get_predictions(al.AGENT_ID)}
    finally:
        store.close()


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv("AGENOMICS_EVIDENCE", raising=False)
    assert al.AgenomicsLink.from_env(EventBus()) is None


def test_task_lifecycle_runtime_critic_and_human(tmp_path):
    bus, link = _link(tmp_path)
    link.start("s:1")
    bus.emit("tool_result", "analyst", is_error=True, tool="x", output="boom")
    bus.emit("self_check", "critic", score=4, issues=[])
    link.finish(True)
    link.human(positive=False)
    preds = _preds(tmp_path)
    runtime, task = preds["runtime_failure"], preds["task_failure"]
    assert [(o.quality_level, o.occurred) for o in runtime.outcomes] == [("Q1", True)]
    assert sorted((o.quality_level, o.occurred) for o in task.outcomes) == [
        ("Q3", True),
        ("Q4", True),
    ]


def test_good_task_and_positive_feedback(tmp_path):
    bus, link = _link(tmp_path)
    link.start("s:2")
    bus.emit("self_check", "critic", score=9, issues=[])
    link.finish(True)
    link.human(positive=True)
    preds = _preds(tmp_path)
    assert [o.occurred for o in preds["runtime_failure"].outcomes] == [False]
    assert sorted((o.quality_level, o.occurred) for o in preds["task_failure"].outcomes) == [
        ("Q3", False),
        ("Q4", False),
    ]


def test_human_feedback_without_task_is_ignored(tmp_path):
    _, link = _link(tmp_path)
    link.human(positive=True)  # нет завершённой задачи: ничего не пишется и не падает
