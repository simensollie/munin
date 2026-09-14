"""Munin: a cross-platform digital meeting recorder and transcription pipeline.

Three separable processes (spec section 5):

- ``munin-rec``  (:mod:`munin.daemon`) captures, never transcribes.
- ``munin-work`` (:mod:`munin.worker`) drains the spool and runs the pipeline.
- ``munin``      (:mod:`munin.cli`)    is the command-line surface.

Nothing above ``munin.capture`` and ``munin.detect`` may reference a platform
(spec section 16.5).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
