"""``Worker`` behaviour: draining, the backend seam, and crash recovery.

``spool.Session``/``spool.Spool`` are real dataclasses but their I/O methods
(``transition``, ``save``, ``iter_sessions``, ...) are still stubs owned by the
spool workstream (contracts section 14) -- they raise ``NotImplementedError``
in this tree. So these tests use small, honest test doubles that implement the
*documented* behaviour of ``transition()`` and the spool's inbox bookkeeping,
in place of the real ones. This exercises the worker's own logic in full; it
does not exercise the real ``Spool``/``Session`` I/O, which is spool's to
verify once merged. See the task's return value for this noted as a limit, not
a gap silently papered over.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from munin.backends.base import BackendUnavailable, Transcript, TranscriptSegment
from munin.config import Config
from munin.pipeline.render import pensieve_filename
from munin.spool import Segment, Session, StateError
from munin.worker import STALE_TRANSCRIBING_REASON, Worker, _compute_gaps


class FakeSession(Session):
    """A ``Session`` whose ``transition`` actually works, for these tests only."""

    def transition(self, to: str, *, by: str, **fields: object) -> None:
        self.history.append(
            {"at": "2026-09-14T13:25:07+02:00", "from": self.state, "to": to, "by": by}
        )
        self.state = to
        for key, value in fields.items():
            setattr(self, key, value)


class FakeSpool:
    """Just enough of ``Spool`` for ``Worker`` to drive: a list and an inbox log."""

    def __init__(self, sessions: list[Session]) -> None:
        self.sessions = list(sessions)
        self.unlinked: list[str] = []

    def iter_sessions(self, *, limit: int | None = None) -> list[Session]:
        return list(self.sessions)

    def unlink_inbox(self, session: Session) -> None:
        self.unlinked.append(session.id)


class SucceedingBackend:
    name = "fake-ok"

    def available(self) -> bool:
        return True

    def transcribe(self, session) -> Transcript:
        return Transcript(
            segments=[
                TranscriptSegment(start=0.0, end=2.0, speaker="Ola Nordmann", text="Hei."),
            ],
            language="nb",
            backend=self.name,
            model="fake-model",
            words=[{"start": 0.0, "end": 0.5, "text": "Hei"}],
        )


class RaisingBackend:
    name = "fake-broken"

    def available(self) -> bool:
        return True

    def transcribe(self, session) -> Transcript:
        raise RuntimeError("boom: backend fell over")


def _make_session(
    tmp_path: Path,
    *,
    state: str,
    segments: list[Segment] | None = None,
    session_id: str = "2026-09-14T1325-weekly-quality-sync",
) -> FakeSession:
    directory = tmp_path / session_id
    directory.mkdir(parents=True, exist_ok=True)
    return FakeSession(
        id=session_id,
        directory=directory,
        state=state,
        title="Weekly quality sync",
        slug="weekly-quality-sync",
        source="adhoc",
        platform="linux",
        segments=segments or [],
    )


def _config(tmp_path: Path) -> Config:
    return Config(home=tmp_path / "home", pensieve_raw_dir=tmp_path / "pensieve")


# ---------------------------------------------------------------------------
# process(): the backend seam


def test_process_success_writes_transcript_and_transitions_to_done(tmp_path, monkeypatch):
    session = _make_session(tmp_path, state="pending")
    worker = Worker(_config(tmp_path))
    worker.spool = FakeSpool([session])
    monkeypatch.setattr("munin.worker.get_backend", lambda name: SucceedingBackend())

    worker.process(session)

    assert session.state == "done"
    assert session.history[-1]["to"] == "done"

    txt_path = session.directory / "transcript.txt"
    assert txt_path.read_text(encoding="utf-8") == "[00:00:00 - 00:00:02] Ola Nordmann: Hei.\n"

    json_path = session.directory / "transcript.json"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["backend"] == "fake-ok"
    assert payload["language"] == "nb"
    assert payload["segments"] == [
        {"start": 0.0, "end": 2.0, "speaker": "Ola Nordmann", "text": "Hei."}
    ]
    assert payload["words"] == [{"start": 0.0, "end": 0.5, "text": "Hei"}]
    assert payload["adjustments"] == []

    pensieve_path = tmp_path / "pensieve" / pensieve_filename("Weekly quality sync")
    assert pensieve_path.read_text(encoding="utf-8") == txt_path.read_text(encoding="utf-8")

    assert session.transcript == {
        "txt": str(txt_path),
        "json": str(json_path),
        "pensieve_copy": str(pensieve_path),
    }
    assert worker.spool.unlinked == [session.id]


def test_process_none_backend_leaves_pending_reason(tmp_path):
    """The PoC's actual configured backend (contracts section 10): the exact
    reason string is part of the contract, not just "some message"."""
    session = _make_session(tmp_path, state="pending")
    worker = Worker(_config(tmp_path))  # transcribe.backend defaults to "none"
    worker.spool = FakeSpool([session])

    worker.process(session)

    assert session.state == "pending"
    assert session.pending_reason == "no transcription backend configured"
    # Still queued: BackendUnavailable must not drop it from the inbox.
    assert worker.spool.unlinked == []
    assert not (session.directory / "transcript.txt").exists()


def test_process_unexpected_exception_marks_failed_and_keeps_audio(tmp_path, monkeypatch):
    session = _make_session(tmp_path, state="pending")
    worker = Worker(_config(tmp_path))
    worker.spool = FakeSpool([session])
    monkeypatch.setattr("munin.worker.get_backend", lambda name: RaisingBackend())

    worker.process(session)

    assert session.state == "failed"
    assert session.error["code"] == "internal"
    assert "boom" in session.error["message"]
    assert session.history[-1]["to"] == "failed"
    # The worker never deletes anything.
    assert session.directory.exists()
    assert worker.spool.unlinked == []


def test_process_raises_state_error_when_not_pending(tmp_path):
    session = _make_session(tmp_path, state="recording")
    worker = Worker(_config(tmp_path))
    worker.spool = FakeSpool([session])
    with pytest.raises(StateError):
        worker.process(session)


# ---------------------------------------------------------------------------
# claim()


def test_claim_moves_captured_to_pending(tmp_path):
    session = _make_session(tmp_path, state="captured")
    worker = Worker(_config(tmp_path))
    worker.claim(session)
    assert session.state == "pending"
    assert session.history[-1] == {
        "at": "2026-09-14T13:25:07+02:00",
        "from": "captured",
        "to": "pending",
        "by": "munin-work",
    }


def test_claim_raises_state_error_when_not_captured(tmp_path):
    session = _make_session(tmp_path, state="pending")
    worker = Worker(_config(tmp_path))
    with pytest.raises(StateError):
        worker.claim(session)


# ---------------------------------------------------------------------------
# drain()


def test_drain_claims_then_processes_in_one_sweep(tmp_path, monkeypatch):
    captured = _make_session(tmp_path, state="captured", session_id="s-captured")
    already_pending = _make_session(tmp_path, state="pending", session_id="s-pending")
    worker = Worker(_config(tmp_path))
    worker.spool = FakeSpool([captured, already_pending])
    monkeypatch.setattr("munin.worker.get_backend", lambda name: SucceedingBackend())

    touched = worker.drain()

    assert captured.state == "done"
    assert already_pending.state == "done"
    # Two sessions, not three transitions: the claimed one is counted once even
    # though this sweep both claimed and processed it.
    assert touched == 2
    assert set(worker.spool.unlinked) == {"s-captured", "s-pending"}


def test_drain_is_a_noop_on_an_empty_or_already_settled_spool(tmp_path):
    done = _make_session(tmp_path, state="done")
    worker = Worker(_config(tmp_path))
    worker.spool = FakeSpool([done])
    assert worker.drain() == 0
    assert done.state == "done"


# ---------------------------------------------------------------------------
# recover(): crash recovery at startup


def test_recover_resets_stale_transcribing_sessions(tmp_path):
    stale = _make_session(tmp_path, state="transcribing", session_id="s-stale")
    untouched = _make_session(tmp_path, state="pending", session_id="s-pending")
    worker = Worker(_config(tmp_path))
    worker.spool = FakeSpool([stale, untouched])

    count = worker.recover()

    assert count == 1
    assert stale.state == "pending"
    assert stale.pending_reason == STALE_TRANSCRIBING_REASON
    assert untouched.state == "pending"
    assert untouched.pending_reason is None


def test_recover_is_idempotent(tmp_path):
    worker = Worker(_config(tmp_path))
    worker.spool = FakeSpool([])
    assert worker.recover() == 0


# ---------------------------------------------------------------------------
# _compute_gaps(): the wall-clock-break-to-timeline-position mapping


def test_compute_gaps_single_segment_is_empty(tmp_path):
    seg = Segment(
        index=1,
        mic="mic.opus",
        app="app.opus",
        started_at=datetime(2026, 9, 14, 13, 25, tzinfo=timezone.utc),
        stopped_at=datetime(2026, 9, 14, 13, 27, tzinfo=timezone.utc),
        duration_seconds=120.0,
    )
    assert _compute_gaps([seg]) == []


def test_compute_gaps_two_segments_with_a_break():
    first_start = datetime(2026, 9, 14, 13, 25, tzinfo=timezone.utc)
    first = Segment(
        index=1,
        mic="mic.opus",
        app="app.opus",
        started_at=first_start,
        stopped_at=first_start + timedelta(seconds=10),
        duration_seconds=10.0,
    )
    second_start = first.stopped_at + timedelta(minutes=5)
    second = Segment(
        index=2,
        mic="mic.002.opus",
        app="app.002.opus",
        started_at=second_start,
        stopped_at=second_start + timedelta(seconds=6),
        duration_seconds=6.0,
    )
    gaps = _compute_gaps([first, second])
    assert gaps == [(10.0, 300.0)]


def test_compute_gaps_ignores_segments_out_of_order_input(tmp_path):
    """Segments are sorted by index before gaps are computed, regardless of
    the order the caller happens to hand them in."""
    first_start = datetime(2026, 9, 14, 13, 25, tzinfo=timezone.utc)
    first = Segment(
        index=1,
        mic="mic.opus",
        app="app.opus",
        started_at=first_start,
        stopped_at=first_start + timedelta(seconds=10),
        duration_seconds=10.0,
    )
    second_start = first.stopped_at + timedelta(minutes=2)
    second = Segment(
        index=2,
        mic="mic.002.opus",
        app="app.002.opus",
        started_at=second_start,
        stopped_at=second_start + timedelta(seconds=6),
        duration_seconds=6.0,
    )
    assert _compute_gaps([second, first]) == [(10.0, 120.0)]
