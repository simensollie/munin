"""The transcription pipeline.

The PoC ships only :mod:`munin.pipeline.render` and :func:`run`, the single
entry point ``munin-work`` calls. Language routing, ASR, diarization,
attribution, voice matching and glossary application (spec 7.1-7.4.1) are
separate stages that will compose *inside* :func:`run` once a real backend
exists; each has a stub module in this package that raises
``NotImplementedError`` pointing at its spec section, so the seam is visible
before it is filled in.

DEVIATION from docs/superpowers/specs/2026-09-14-poc-contracts.md section 14:
that document lists this file as frozen with an empty ``__all__``. The task
brief for the worker workstream explicitly asks for ``run(session, backend)``
here as "the single entry the worker calls". Implemented as asked; flagged
because it edits a file the contracts doc names as frozen. The change is
additive (one function, no signature elsewhere altered) and low-risk to other
workstreams, none of which import this module.
"""

from __future__ import annotations

from munin.backends.base import Backend, Transcript
from munin.capture.base import SessionRef

__all__ = ["run"]


def run(session: SessionRef, backend: Backend) -> Transcript:
    """Transcribe one session through the given backend.

    For the PoC this is a thin pass-through to ``backend.transcribe(session)``:
    no language routing, ASR, diarization, attribution, voice matching or
    glossary application happens yet (see the stub modules in this package).
    ``BackendUnavailable`` is not caught here -- the worker decides what a
    failed or unavailable backend means for the session's state.
    """
    return backend.transcribe(session)
