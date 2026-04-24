"""Vendored subset of the Coqtail (https://github.com/whonore/Coqtail) plugin.

The files in this directory — ``xmlInterface.py``, ``coqtop.py``, ``coqtail.py`` —
are unmodified copies used for their XML-protocol client and sentence-parsing
helpers. See the top-level LICENSE for attribution.

They expect to be imported as top-level modules (they do ``import coqtop as CT``
rather than ``from . import coqtop``). The parent package adds this directory
to :data:`sys.path` at import time so these modules remain byte-for-byte
identical to the upstream.
"""
