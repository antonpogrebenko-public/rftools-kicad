"""The entrypoint KiCad runs for the plugin's action (plugin.json: rftools_kicad/main.py).

KiCad starts this file with the plugin environment's Python, made from the
interpreter set in Preferences › Plugins (``api.interpreter_path`` in
``kicad_common.json``). The plugin runs on KiCad's own Python — 3.9 on macOS,
3.11 on Windows, the system ``python3`` on Linux — and needs 3.9 or later
(design Decision 7a). So this file must parse and start on anything a user
might have set: it checks the version before importing anything else and, when
the interpreter is too old, says so and how to fix it (a wx dialog when wx
imports; otherwise the console, which KiCad 10.0.1+ shows in its status-bar
messages, and a small HTML page opened in the browser).
tests/test_main.py parses this file with a 3.9 grammar; keep it plain
(Optional[...] rather than ``X | None``, no ``match``) and import the plugin's
own modules inside functions, after the check.

Then: connect to KiCad (``kipy.KiCad()``), read the board's stackup and the
project's net classes, build the layer model, and hand plain data to the
presentation layer: ``present(model, netclasses, services)``.
"""
from __future__ import annotations

import html
import os
import sys
import tempfile
from typing import Any, Callable, List, Optional

REQUIRED_PYTHON = (3, 9)
TITLE = "rftools.io board calculations"
#: plugin.json's identifier, which names the plugin's environment folder.
PLUGIN_ID = "io.rftools.kicad"
#: How a user makes KiCad rebuild the plugin's environment (as api.RECREATE_ENVIRONMENT,
#: repeated here because this file imports nothing of the plugin's before the check).
RECREATE_ENVIRONMENT = (
    "right-click the plugin in Preferences › Plugins › Action Plugins and choose "
    "Recreate Plugin Environment"
)

API_NOT_ENABLED = (
    "Could not connect to KiCad. Enable the KiCad API in Preferences › Plugins "
    "(\"Enable KiCad API\"), then run this action again from the PCB editor."
)
NO_BOARD = (
    "KiCad did not return an open board. Open the board in the PCB editor and run "
    "this action from there."
)


class PluginStartError(Exception):
    """A reason the plugin cannot start, worded for the user."""


# ── Before anything else: the interpreter ────────────────────────────────────


def python_too_old_message(
    version_info: Optional[Any] = None, executable: Optional[str] = None
) -> Optional[str]:
    """The message for an interpreter older than 3.9, or None when it is new enough."""
    version = tuple(version_info or sys.version_info)
    if version[:2] >= REQUIRED_PYTHON:
        return None
    found = ".".join(str(part) for part in version[:3])
    where = executable or sys.executable or "an unknown interpreter"
    return (
        "The rftools.io plugin needs Python 3.9 or later. KiCad is running it with "
        "Python {found} ({where}).\n\n"
        "The plugin runs on KiCad's own Python and needs nothing else installed. Open "
        "Preferences › Plugins in KiCad and set Python Interpreter back to KiCad's own "
        "Python (on Linux, the system python3). KiCad 10.0.6 does not rebuild the "
        "plugin's environment on its own: {recreate}, or delete the plugin's environment "
        "folder ({env} under KiCad's python-environments cache), then restart KiCad."
    ).format(found=found, where=where, recreate=RECREATE_ENVIRONMENT, env=PLUGIN_ID)


def notify(
    text: str,
    error: bool = True,
    browser_fallback: bool = False,
    opener: Optional[Callable[[str], Any]] = None,
) -> str:
    """Show *text* to the user; returns how: "wx", "browser" or "console".

    The text always goes to stderr too: KiCad 10.0.1 and later report a plugin
    action's console output in the editor's status-bar messages.
    """
    say(text, sys.stderr)
    try:
        import wx
    except Exception:
        wx = None
    if wx is not None:
        try:
            app = wx.App(False)
            style = wx.OK | (wx.ICON_ERROR if error else wx.ICON_INFORMATION)
            wx.MessageBox(text, TITLE, style)
            del app
            return "wx"
        except Exception:
            pass
    if browser_fallback:
        path = write_message_page(text)
        if opener is None:
            from rftools_kicad.browser import open_url

            opener = open_url
        from pathlib import Path

        opener(Path(path).as_uri())
        return "browser"
    return "console"


def say(text: str, stream: Any) -> None:
    """Print *text*, replacing what the stream's encoding cannot carry.

    A plugin's output is a pipe to KiCad, and on Windows a pipe's encoding is
    the ANSI code page, which has no εr or ›: an unencodable character must
    not turn a message into a traceback.
    """
    try:
        print(text, file=stream)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding), file=stream)


def write_message_page(text: str, directory: Optional[str] = None) -> str:
    """A small HTML page stating *text*; returns its path."""
    paragraphs = "".join(
        "<p>{}</p>".format(html.escape(part)) for part in text.split("\n\n") if part.strip()
    )
    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<title>{title}</title><style>body{{font:16px/1.5 system-ui,sans-serif;"
        "max-width:40em;margin:3em auto;padding:0 1em}}</style></head>"
        "<body><h1>{title}</h1>{body}</body></html>\n"
    ).format(title=html.escape(TITLE), body=paragraphs)
    directory = directory or tempfile.gettempdir()
    fd, path = tempfile.mkstemp(prefix="rftools-kicad-", suffix=".html", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(page)
    return path


# ── Then the plugin ──────────────────────────────────────────────────────────


def _plugin_dir_on_path() -> None:
    """KiCad runs this file as a script; make the package importable from it."""
    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)


def connect(factory: Optional[Callable[[], Any]] = None) -> Any:
    """``(kicad, board, project)`` from the running KiCad, or PluginStartError."""
    try:
        import kipy
        from kipy.errors import ApiError
        from kipy.errors import ConnectionError as KiCadConnectionError
    except Exception as exc:
        raise PluginStartError(
            "The kicad-python library could not be loaded ({}: {}). To reinstall it, "
            "{}.".format(type(exc).__name__, exc, RECREATE_ENVIRONMENT)
        ) from exc
    try:
        kicad = (factory or kipy.KiCad)()
        board = kicad.get_board()
        project = board.get_project()
    except KiCadConnectionError as exc:
        raise PluginStartError(API_NOT_ENABLED) from exc
    except ApiError as exc:
        raise PluginStartError("{} (KiCad said: {})".format(NO_BOARD, exc)) from exc
    return kicad, board, project


class Services:
    """What the presentation layer gets besides the model and the net classes.

    ``api()`` builds the API client with the configured key (raising
    PluginStartError when there is none); ``save_key(key)`` checks a pasted
    key with a usage request and saves it.
    ``kicad``, ``board`` and ``project`` are kicad-python handles, for the
    net-class writer; the core modules never touch them.
    """

    def __init__(
        self,
        settings: Any,
        options: Any,
        cache: Any,
        kicad: Any = None,
        board: Any = None,
        project: Any = None,
    ) -> None:
        from rftools_kicad import __version__
        from rftools_kicad.settings import KEY_URL, history_path
        from rftools_kicad.solve import DEFAULT_GRID_MM

        self.version = __version__
        self.settings = settings
        self.options = options
        #: The manufacturing grid for target solves, mm (settings "grid").
        self.grid = _positive_number(settings.options.get("grid"), DEFAULT_GRID_MM)
        self.cache = cache
        self.key_url = KEY_URL
        self.history_path = history_path()
        self.kicad = kicad
        self.board = board
        self.project = project

    @property
    def has_key(self) -> bool:
        return bool(self.settings.api_key)

    def api(self) -> Any:
        from rftools_kicad.api import Api, make_client

        key = self.settings.api_key
        if not key:
            raise PluginStartError("No API key is set. Get a free key at " + self.key_url)
        return Api(make_client(key), self.cache, key_id=self.settings.key_id)

    def save_key(self, key: str) -> dict:
        from rftools_kicad.api import make_client
        from rftools_kicad.settings import load_settings, save_key

        usage = save_key(key, make_client, path=self.settings.path)
        self.settings = load_settings(self.settings.path)
        return usage


def present(model: Any, netclasses: List[Any], services: Services) -> int:
    """The presentation layer's interface: show the model, run what the user selects.

    The real presentation layers are ``dialog.present`` (wxPython) and, where
    wx cannot load, ``report.present`` (an HTML report with no write control);
    see :func:`presenter`. Both receive plain data: a ``stackup.LayerModel``, a
    list of ``mapping.NetClassValues`` and :class:`Services`, and both work
    through ``dialog_logic.DialogState``. This last resort, used only when
    neither module imports, prints the proposed model and spends no API call.
    """
    lines = ["{} {}".format(TITLE, services.version)]
    planes = ", ".join(model.planes) or "none"
    lines.append("Reference planes ({}): {}".format(model.plane_source, planes))
    for structure in model.structures:
        lines.append("  " + structure.describe())
    for problem in model.problems:
        lines.append("  ! " + problem.message)
    for nc in netclasses:
        lines.append("  Net class {}: track {} mm, via {} / {} mm, clearance {} mm".format(
            nc.name,
            _mm(nc.track_width_nm), _mm(nc.via_diameter_nm), _mm(nc.via_drill_nm),
            _mm(nc.clearance_nm),
        ))
    lines.append("The results dialog could not be loaded; no API call was made.")
    say("\n".join(lines), sys.stdout)
    return 0


def presenter() -> Callable[..., int]:
    """The first presentation layer that loads: the dialog, the HTML report, the stub."""
    import importlib

    for name in ("rftools_kicad.dialog", "rftools_kicad.report"):
        try:
            module = importlib.import_module(name)
        except Exception:  # wx missing, or failing to load its libraries
            continue
        fn = getattr(module, "present", None)
        if callable(fn):
            return fn
    return present


def run(
    kicad_factory: Optional[Callable[[], Any]] = None,
    present_fn: Optional[Callable[..., int]] = None,
    settings: Any = None,
) -> int:
    """Read the board, build the model, hand off. Returns the process exit code."""
    _plugin_dir_on_path()
    from rftools_kicad.cache import ResultCache
    from rftools_kicad.mapping import NetClassValues, Options
    from rftools_kicad.settings import load_settings, results_path
    from rftools_kicad.stackup import layer_model

    settings = settings or load_settings()
    configure_logging(settings)
    try:
        kicad, board, project = connect(kicad_factory)
        stackup, kicad_netclasses = read_board(board, project)
    except PluginStartError as exc:
        notify(str(exc))
        return 1
    model = layer_model(stackup)
    netclasses = [NetClassValues.from_kipy(nc) for nc in kicad_netclasses]
    options = _options(Options, settings.options)
    services = Services(settings, options, ResultCache(results_path()), kicad, board, project)
    return (present_fn or presenter())(model, netclasses, services)


def read_board(board: Any, project: Any) -> Any:
    """``(neutral stackup dict, kicad-python net classes)``, or PluginStartError."""
    from kipy.errors import ApiError
    from kipy.errors import ConnectionError as KiCadConnectionError

    from rftools_kicad.stackup import from_kipy

    try:
        return from_kipy(board.get_stackup()), list(project.get_net_classes())
    except KiCadConnectionError as exc:
        raise PluginStartError(API_NOT_ENABLED) from exc
    except ApiError as exc:
        raise PluginStartError(
            "KiCad did not return the board's stackup and net classes "
            "(KiCad said: {}).".format(exc)
        ) from exc


def configure_logging(settings: Any) -> None:
    """Warnings and worse to stderr, every key-shaped string cut to its public id."""
    import logging

    from rftools_kicad.settings import RedactingFilter

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RedactingFilter(keys=(settings.api_key,)))
    handler.setFormatter(logging.Formatter("rftools.io plugin: %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_rftools", False)]
    handler._rftools = True  # type: ignore[attr-defined]
    handler.setLevel(logging.WARNING)
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # KiCad keeps a plugin's output only when it fails, so the plugin also keeps
    # a small log of its own for support: <cache>/rftools-kicad/plugin.log
    # (RFTOOLS_KICAD_LOG_DIR moves it; the test suite points it at a temp folder).
    try:
        import os as _os
        from logging.handlers import RotatingFileHandler
        from pathlib import Path as _Path

        from rftools_kicad.settings import cache_dir

        directory = _Path(_os.environ.get("RFTOOLS_KICAD_LOG_DIR") or cache_dir())
        directory.mkdir(parents=True, exist_ok=True)
        to_file = RotatingFileHandler(str(directory / "plugin.log"), maxBytes=256_000,
                                      backupCount=1, encoding="utf-8")
        to_file.addFilter(RedactingFilter(keys=(settings.api_key,)))
        to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        to_file.setLevel(logging.INFO)
        to_file._rftools = True  # type: ignore[attr-defined]
        root.addHandler(to_file)
    except Exception:  # noqa: BLE001 — a log file is never worth failing the plugin for
        pass


def main(argv: Optional[List[str]] = None, version_info: Optional[Any] = None) -> int:
    message = python_too_old_message(version_info)
    if message is not None:
        notify(message, browser_fallback=True)
        return 2
    try:
        return run()
    except Exception as exc:  # never a bare traceback in KiCad's status bar
        _plugin_dir_on_path()
        from rftools_kicad.settings import redact

        notify(redact("The plugin stopped: {}: {}".format(type(exc).__name__, exc)))
        return 1


def _options(options_type: Any, saved: dict) -> Any:
    known = {k: v for k, v in (saved or {}).items() if k in options_type.__dataclass_fields__}
    try:
        return options_type(**known)
    except TypeError:
        return options_type()


def _positive_number(value: Any, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return default


def _mm(nm: Optional[int]) -> str:
    return "?" if nm is None else "{:g}".format(nm / 1e6)


if __name__ == "__main__":
    sys.exit(main())
