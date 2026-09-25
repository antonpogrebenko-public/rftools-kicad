"""rftools.io board calculations for KiCad (IPC API plugin).

The core modules turn the board KiCad reports into calculator requests, run
them through the rftools.io API with a local cache, and hand plain data to a
presentation layer:

    stackup   the board's stackup -> a neutral dict -> a LayerModel
    mapping   a LayerModel layer + net-class values -> calculator inputs
    api       the rftools.io SDK behind a cache, with refusals mapped by type
    cache     results.json in the user cache directory
    solve     the width or gap for a target through one solve call
    budget    the calls a run will use, before any is spent
    settings  the API key and options in the user config directory
    main      the entrypoint KiCad runs

Nothing here imports the SDK or kicad-python at module import, so a missing or
incompatible dependency becomes a readable message rather than a traceback in
KiCad's status bar. The plugin needs Python 3.12 or later; ``main`` checks that
before importing anything else.
"""
from __future__ import annotations

__version__ = "0.1.0"

#: The plugin's identifier: plugin.json, metadata.json and the key's client label.
IDENTIFIER = "io.rftools.kicad"

#: What the key link names as the client (api-metering Decision 3).
CLIENT_NAME = "kicad-plugin"
