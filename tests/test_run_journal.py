from datetime import UTC, datetime
from uuid import uuid4

from client.run_journal import RunJournal
from common.protocol import LogLine, RunState, SafetyState

NOW = datetime(2026, 11, 24, 12, tzinfo=UTC)


def test_full_and_important_journal_use_the_same_run():
    journal = RunJournal()
    run = RunState(
        id=uuid4(),
        script_id="test",
        status="running",
        pid=None,
        started_at=NOW,
        elapsed_seconds=0,
        exit_code=None,
    )
    journal.record_run(None, run)
    journal.record_run(run, run)
    for sequence, level, message in (
        (1, "INFO", "ordinary progress"),
        (2, "WARNING", "important warning"),
    ):
        journal.record_line(
            LogLine(sequence=sequence, timestamp=NOW, source="stdout", level=level, message=message)
        )
    journal.record_run(run, run.model_copy(update={"status": "succeeded", "exit_code": 0}))
    journal.record_safety(None, SafetyState(latched=True, reason="lost link"))
    assert [item.value.rsplit("  ", 1)[-1] for item in journal.full.controls] == [
        "ordinary progress",
        "important warning",
    ]
    summary = [row.controls[-1].value for row in journal.summary.controls]
    assert summary == [
        "Запуск начат.",
        "important warning",
        "Запуск завершён.",
        "Аварийная блокировка включена. lost link",
    ]
    journal.set_mode("full")
    assert journal.terminal.visible and not journal.timeline.visible
    journal.reset()
    assert not journal.full.controls and not journal.summary.controls
    assert journal.terminal.visible
