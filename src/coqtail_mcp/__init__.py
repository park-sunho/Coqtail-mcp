"""coqtail-mcp — an MCP server for Rocq/Coq proof sessions.

The server exposes a minimal surface to AI agents:

  * rocq_start / rocq_close        — session lifecycle
  * rocq_step_to                   — advance or rewind to a buffer position
  * rocq_goals                     — fetch the current proof goal + context
  * rocq_query                     — run a non-state-changing query

Implementation note
-------------------
The low-level XML plumbing (spawning ``coqidetop``/``coqtop``, framing XML,
dispatching per-version encoders, tracking ``state_id``s) is the hard part
and has already been solved by the Coqtail vim plugin. Rather than
re-implement it, this package vendors three files from Coqtail under
:mod:`coqtail_mcp.coqtail_lib` and adds a thin session/server layer on top.

Because Coqtail's modules use ``import coqtop as CT`` style (relative to
their own directory), we prepend that directory to :data:`sys.path` at
package import time so the vendored files keep working unmodified.
"""

from __future__ import annotations

import os
import sys

_COQTAIL_LIB_DIR = os.path.join(os.path.dirname(__file__), "coqtail_lib")
if _COQTAIL_LIB_DIR not in sys.path:
    sys.path.insert(0, _COQTAIL_LIB_DIR)

__all__ = ["__version__"]
__version__ = "0.1.0"
