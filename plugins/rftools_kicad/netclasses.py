"""Writing suggested widths and gaps to net classes, previewed, verified and reversible.

Design Decision 5 and the spec ("Writing to net classes SHALL be opt-in,
previewed, reversible and limited"):

* :func:`make_preview` reads the project's net classes afresh and lists, for
  each selected class, every field that would change: current and proposed
  track width, differential-pair width and differential-pair gap. Only those
  three fields are ever written.
* **Only a KiCad later than 10.0.6 is written to** (:data:`FIRST_SAFE_WRITE_AFTER`,
  :func:`write_blocked`). KiCad 10.0.6 and earlier corrupt a class written
  through the API, and a KiCad whose version cannot be read is treated the
  same. There the preview is still made, marked ``manual`` with the reason, so
  the user can enter the values in Board Setup by hand; nothing is sent, and
  Restore is unavailable too (the history stays readable).
* :func:`apply` writes nothing until ``confirm(preview)`` returns True. It then
  re-reads the classes and refuses if any listed value changed in KiCad since
  the preview, counts each class's nets, records the previous values in the
  history file, and sends each changed class **whole, as read**, with only the
  three fields changed, ``type = NCT_EXPLICIT`` and ``constituents`` cleared,
  merge mode ``MMM_MERGE``. ``NETCLASS::Deserialize`` refuses an implicit
  class: KiCad 10.0.6 drops one silently while ``SetNetClasses`` answers
  success (spike, 2026-09-25), and a KiCad with commit d622c37a answers
  AS_BAD_REQUEST.
* The write goes through ``Project.set_net_classes`` where kicad-python has it
  (0.9 and later), else through kicad-python 0.8's ``SetNetClasses`` message on
  the project's command channel. Neither available, or KiCad refusing the
  command, and the write is reported unavailable with the reason.
* Because KiCad has answered success while ignoring or corrupting a class,
  every write is verified by reading the classes and the nets again
  (:func:`verify`): the three fields must hold the proposed values, every
  other setting of every class must be unchanged, each written class must
  still list itself as its only constituent and hold as many nets as before.
  A class that is not intact is reported with "Do not save the board"; a
  write that changed nothing, or only landed other values in the three
  fields, is reported as not applied.
* ``<user config dir>/rftools-kicad/history.json`` keeps every write per
  project path. :func:`restore_preview` and :func:`restore` write the previous
  values of the latest write not yet restored back the same way, and record
  the restore as a write of its own.

Values are nanometres, KiCad's unit; results arrive in millimetres and are
converted with :func:`mm_to_nm`.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rftools_kicad.api import RECREATE_ENVIRONMENT
from rftools_kicad.settings import history_path as default_history_path
from rftools_kicad.settings import write_private_json
from rftools_kicad.stackup import NM_PER_MM

log = logging.getLogger(__name__)

TRACK_WIDTH = "track_width"
DIFF_PAIR_WIDTH = "diff_pair_track_width"
DIFF_PAIR_GAP = "diff_pair_gap"
#: The only fields a write changes, in the order they are listed.
FIELDS = (TRACK_WIDTH, DIFF_PAIR_WIDTH, DIFF_PAIR_GAP)
FIELD_LABELS = {
    TRACK_WIDTH: "Track width",
    DIFF_PAIR_WIDTH: "Diff pair width",
    DIFF_PAIR_GAP: "Diff pair gap",
}
#: The three fields as Board Setup › Net Classes names its columns, for entering them by hand.
BOARD_SETUP_COLUMNS = {
    TRACK_WIDTH: "Track Width",
    DIFF_PAIR_WIDTH: "DP Width",
    DIFF_PAIR_GAP: "DP Gap",
}

METHOD_SET_NET_CLASSES = "Project.set_net_classes"
METHOD_COMMAND = "SetNetClasses command"

ACTION_APPLY = "apply"
ACTION_RESTORE = "restore"

STATUS_SENDING = "sending"
STATUS_APPLIED = "applied"
STATUS_NOT_APPLIED = "not applied"  # KiCad answered, and nothing changed
STATUS_MISMATCH = "mismatch"  # only the three fields hold other values than sent
STATUS_REFUSED = "refused"  # KiCad refused the command, and nothing changed
#: A class read back is not intact (it lost its constituent or nets, another of
#: its settings changed, a class appeared or went), or the classes could not be
#: read back at all: the board must not be saved.
STATUS_DAMAGED = "damaged"

#: Writes whose previous values can be written back. A damaged write is not:
#: the user is told to close the board without saving, which discards it.
RESTORABLE = (STATUS_APPLIED, STATUS_MISMATCH)

NO_PROJECT_KEY = "(no project path)"

HISTORY_VERSION = 1

UPDATE_KICAD_PYTHON = f"Update kicad-python to 0.9 or later ({RECREATE_ENVIRONMENT})."

# ── Which KiCad can take a write ─────────────────────────────────────────────

#: Net classes are written only to a KiCad strictly later than this.
#:
#: In KiCad 10.0.6 (tagged 2026-08-28) and earlier, ``NETCLASS::Deserialize``
#: calls ``SetConstituentNetclasses({})``, so a class written through the API
#: stops listing itself as its constituent: ``GetNets`` filtered by the class
#: drops to no nets (26 to 0 in the spike), writing the old values back does
#: not repair it, and the next save hangs KiCad on Linux and crashes it on
#: Windows (spike, 2026-09-25). KiCad commit d622c37a ("API: Fix handling of
#: netclasses", 2026-09-22, cherry-picked to the 10.0 branch) keeps the class
#: as its own constituent and answers AS_BAD_REQUEST for a class it cannot
#: unpack. No release tag held it on 2026-09-25, so the first release with it
#: is a later 10.0.x or 11.x. A development build numbered above 10.0.6 but
#: built before the fix passes this gate; :func:`verify` catches that write.
FIRST_SAFE_WRITE_AFTER = (10, 0, 6)

#: A written class's type as a KiCad with d622c37a reports it
#: (``NETCLASS::Serialize``: NCT_EXPLICIT for at most one constituent). Checked
#: and logged, never failed on.
TYPE_AFTER_WRITE = "NCT_EXPLICIT"

WRITE_CORRUPTS = (
    "KiCad 10.0.6 and earlier corrupt a net class written through the API: the class "
    "loses its nets, and the next save can hang or crash KiCad. Enter the values in "
    "Board Setup › Net Classes instead; writing from the plugin turns on with the next "
    "KiCad release."
)

DO_NOT_SAVE = (
    "The write did not apply correctly. Do not save the board; close it without saving "
    "and reopen it."
)


class NetClassWriteError(Exception):
    """A write that cannot be made; the message is worded for the user."""


# ── Values ───────────────────────────────────────────────────────────────────


def mm_to_nm(mm: float) -> int:
    """Millimetres to whole nanometres (KiCad's unit)."""
    return int(round(mm * NM_PER_MM))


def nm_text(nm: int | None) -> str:
    return "not set" if nm is None else f"{nm / NM_PER_MM:g} mm"


def field_values(netclass: Any) -> dict:
    """``{field: nanometres or None}`` for the three fields of a class (proto or wrapper)."""
    proto = _proto(netclass)
    out = {}
    for name in FIELDS:
        if proto.HasField("board") and proto.board.HasField(name):
            out[name] = int(getattr(proto.board, name).value_nm)
        else:
            out[name] = None
    return out


# ── The preview ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldChange:
    netclass: str
    field: str
    current_nm: int | None
    proposed_nm: int | None

    @property
    def label(self) -> str:
        return FIELD_LABELS[self.field]

    def text(self) -> str:
        return (
            f"{self.netclass}: {self.label.lower()} {nm_text(self.current_nm)} → "
            f"{nm_text(self.proposed_nm)}"
        )


@dataclass(frozen=True)
class ClassPreview:
    """Every one of the three fields of one class: current, and proposed where it changes."""

    netclass: str
    current: dict
    proposed: dict

    def rows(self, labels: Mapping[str, str] = FIELD_LABELS) -> list:
        """``(field label, current text, proposed text, changes)`` for each of the three fields.

        Values are in mm, as Board Setup shows them; *labels* names the fields
        (:data:`BOARD_SETUP_COLUMNS` for Board Setup's own column names).
        """
        rows = []
        for name in FIELDS:
            current = self.current.get(name)
            if name in self.proposed and self.proposed[name] != current:
                rows.append((labels[name], nm_text(current), nm_text(self.proposed[name]), True))
            else:
                rows.append((labels[name], nm_text(current), "unchanged", False))
        return rows


@dataclass(frozen=True)
class Preview:
    """What a write would change. ``changes`` holds only fields whose value changes.

    ``manual`` is set, to the reason, when this KiCad cannot take the write:
    the preview is then only for entering the values by hand in Board Setup,
    and :func:`apply` refuses it.
    """

    changes: tuple
    classes: tuple = ()  # ClassPreview per selected class, in the project's order
    notes: tuple = ()
    action: str = ACTION_APPLY
    restores: str | None = None  # the history entry a restore writes back
    manual: str | None = None

    @property
    def empty(self) -> bool:
        return not self.changes

    @property
    def class_names(self) -> tuple:
        return tuple(dict.fromkeys(c.netclass for c in self.changes))

    def text(self) -> str:
        lines = [c.text() for c in self.changes] or ["Nothing would change."]
        return "\n".join(lines + list(self.notes))

    def manual_text(self) -> str:
        """Every class's three fields, current and to enter, as tab-separated text to copy."""
        if self.action == ACTION_RESTORE:
            what = "Previous net-class values recorded by the rftools.io plugin"
        else:
            what = "Net-class values proposed by the rftools.io plugin"
        lines = [
            f"{what}, to enter in KiCad's Board Setup › Net Classes:",
            "\t".join(("Net class", "Column", "Current", "Enter")),
        ]
        for netclass in self.classes:
            for column, current, enter, _ in netclass.rows(BOARD_SETUP_COLUMNS):
                lines.append("\t".join((netclass.netclass, column, current, enter)))
        if self.notes:
            lines += [""] + list(self.notes)
        return "\n".join(lines) + "\n"


def make_preview(
    project: Any,
    proposals: Mapping[str, Mapping[str, int | None]],
    notes: Iterable[str] = (),
    *,
    action: str = ACTION_APPLY,
    restores: str | None = None,
) -> Preview:
    """The changes *proposals* (``{class: {field: nm}}``) would make to the project now.

    Reads the classes from KiCad. A proposal for a class the project no longer
    has, or a value equal to the current one, is left out; a field outside the
    three is refused.
    """
    for fields in proposals.values():
        stray = [f for f in fields if f not in FIELDS]
        if stray:
            raise ValueError(f"only {', '.join(FIELDS)} can be written, not {', '.join(stray)}")
    classes = read_classes(project)
    by_name = {c.name: c for c in classes}
    notes = list(notes)
    changes = []
    previews = []
    for proto in classes:  # the project's order
        if proto.name not in proposals:
            continue
        current = field_values(proto)
        proposed = dict(proposals[proto.name])
        previews.append(ClassPreview(proto.name, current, proposed))
        for name in FIELDS:
            if name in proposed and proposed[name] != current[name]:
                changes.append(FieldChange(proto.name, name, current[name], proposed[name]))
    for name in proposals:
        if name not in by_name:
            notes.append(f"Net class {name} is no longer in the project; it is left out.")
    return Preview(tuple(changes), tuple(previews), tuple(notes), action, restores)


# ── Availability ─────────────────────────────────────────────────────────────


def kicad_version(kicad: Any) -> tuple | None:
    """``(major, minor, patch)`` of the running KiCad, or None when it cannot be read.

    kicad-python 0.8 has it as ``KiCad.get_version()``, a ``KiCadVersion``
    with ``major``, ``minor`` and ``patch`` (KiCad's ``GetVersion`` command).
    An empty reply (0.0.0) counts as unread.
    """
    getter = getattr(kicad, "get_version", None)
    if not callable(getter):
        return None
    try:
        reply = getter()
        version = (int(reply.major), int(reply.minor), int(reply.patch))
    except Exception as exc:  # no answer, or not a version
        log.warning("Could not read KiCad's version (%s).", type(exc).__name__)
        return None
    return None if version == (0, 0, 0) else version


def version_text(version: tuple) -> str:
    return ".".join(str(part) for part in version[:3])


def write_blocked(version: tuple | None) -> str | None:
    """Why this KiCad must not be sent a net-class write, or None when it may.

    Writes need a version strictly later than :data:`FIRST_SAFE_WRITE_AFTER`;
    one that cannot be read is treated as too old.
    """
    if version is None:
        return "KiCad's version could not be read, so net classes are not written. " + (
            WRITE_CORRUPTS
        )
    if tuple(version[:3]) <= FIRST_SAFE_WRITE_AFTER:
        return f"This is KiCad {version_text(version)}. {WRITE_CORRUPTS}"
    return None


def availability(project: Any, version: tuple | None = None) -> tuple:
    """``(method, None)`` when a write can be sent, else ``(None, reason)``.

    *version* is KiCad's ``(major, minor, patch)`` (:func:`kicad_version`);
    without it nothing is written (:func:`write_blocked`).
    """
    if project is None:
        return None, "No KiCad project is open, so net classes cannot be written."
    blocked = write_blocked(version)
    if blocked is not None:
        return None, blocked
    if callable(getattr(project, "set_net_classes", None)):
        return METHOD_SET_NET_CLASSES, None
    try:
        from kipy.proto.common.commands import project_commands_pb2
        from kipy.proto.common.types import MapMergeMode  # noqa: F401
    except Exception:  # no kicad-python, or one without these modules
        return None, (
            "The installed kicad-python library can not set net classes. " + UPDATE_KICAD_PYTHON
        )
    if not hasattr(project_commands_pb2, "SetNetClasses"):
        return None, (
            "The installed kicad-python library has neither Project.set_net_classes nor "
            "the SetNetClasses command. " + UPDATE_KICAD_PYTHON
        )
    client = getattr(project, "_kicad", None)
    if not callable(getattr(client, "send", None)):
        return None, (
            "The installed kicad-python library gives no way to send the SetNetClasses "
            "command. " + UPDATE_KICAD_PYTHON
        )
    return METHOD_COMMAND, None


# ── Writing ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WriteResult:
    applied: bool
    message: str
    status: str | None = None
    entry_id: str | None = None
    method: str | None = None
    cancelled: bool = False
    sent: bool = False
    problems: tuple = field(default=(), compare=False)


def apply(
    project: Any,
    preview: Preview,
    confirm: Callable[[Preview], bool],
    *,
    version: tuple | None = None,
    board: Any = None,
    history_file: Path | None = None,
    project_key: str | None = None,
    clock: Callable[[], float] = time.time,
) -> WriteResult:
    """Write *preview* once *confirm* approves it; verify it; record it.

    *version* is KiCad's ``(major, minor, patch)`` (:func:`kicad_version`) and
    gates the write; *board* is kicad-python's ``Board``, whose nets per
    written class are counted before and after (:func:`net_counts`).

    Nothing is sent when the write is unavailable (this KiCad included), the
    preview is empty or only for entering by hand, the user cancels, a listed
    value changed in KiCad since the preview, or the nets cannot be counted.
    """
    method, reason = availability(project, version)
    if method is None:
        return WriteResult(False, reason)
    if preview.manual:
        return WriteResult(False, preview.manual)
    if preview.empty:
        return WriteResult(False, "Nothing to write: every value is already as proposed.")
    if not confirm(preview):
        return WriteResult(False, "Cancelled; nothing was written.", cancelled=True)

    history = History(history_file or default_history_path())
    key = project_key or project_path(project)
    before = read_classes(project)
    by_name = {c.name: c for c in before}
    moved = []
    for change in preview.changes:
        proto = by_name.get(change.netclass)
        if proto is None:
            moved.append(f"net class {change.netclass} is no longer in the project")
        elif field_values(proto)[change.field] != change.current_nm:
            moved.append(
                f"{change.netclass}'s {change.label.lower()} is now "
                f"{nm_text(field_values(proto)[change.field])}"
            )
    if moved:
        return WriteResult(False, (
            "Nothing was written: the net classes changed in KiCad since the preview ("
            + "; ".join(moved) + "). Review the changes again."
        ))
    names = preview.class_names
    try:
        nets_before = net_counts(board, names)
    except NetClassWriteError as exc:
        return WriteResult(False, f"Nothing was written: {exc}.")

    wanted = _proposed(preview)
    to_send = [explicit_copy(by_name[name], wanted[name]) for name in names]
    try:
        entry = history.append(key, {
            "action": preview.action,
            "at": _iso(clock()),
            "status": STATUS_SENDING,
            "method": method,
            "restores": preview.restores,
            "changes": [
                {"netclass": c.netclass, "field": c.field,
                 "before": c.current_nm, "after": c.proposed_nm}
                for c in preview.changes
            ],
        })
    except (OSError, NetClassWriteError) as exc:
        # No record, no write: every write must stay restorable.
        detail = str(exc) if isinstance(exc, NetClassWriteError) else (
            f"the write history at {history.path} cannot be written ({type(exc).__name__})"
        )
        return WriteResult(False, f"Nothing was written: {detail}")
    try:
        send(project, method, to_send)
    except Exception as exc:
        log.warning("KiCad refused the net-class write: %s", type(exc).__name__)
        refused = f"KiCad refused the write ({type(exc).__name__}: {exc})"
        # KiCad unpacks the classes one by one and changes each in place, so a
        # refusal part-way can leave the classes before it written: read back.
        status, problems = _read_back(project, board, before, {}, nets_before)
        if status == STATUS_APPLIED:  # every class exactly as before
            history.update_quietly(key, entry["id"], status=STATUS_REFUSED)
            return WriteResult(
                False, f"{refused}. Nothing was changed.",
                status=STATUS_REFUSED, entry_id=entry["id"], method=method, sent=True,
            )
        history.update_quietly(key, entry["id"], status=STATUS_DAMAGED, problems=problems)
        return WriteResult(
            False, f"{refused}. {_damaged(problems)}",
            status=STATUS_DAMAGED, entry_id=entry["id"], method=method, sent=True,
            problems=tuple(problems),
        )

    status, problems = _read_back(project, board, before, wanted, nets_before)
    if problems:
        history.update_quietly(key, entry["id"], status=status, problems=problems)
    else:
        history.update_quietly(key, entry["id"], status=status)
    if status == STATUS_APPLIED:
        if preview.action == ACTION_RESTORE and preview.restores:
            history.update_quietly(key, preview.restores, restoredBy=entry["id"])
        what = "Restored" if preview.action == ACTION_RESTORE else "Wrote"
        count = len(preview.class_names)
        return WriteResult(
            True,
            f"{what} {len(preview.changes)} value{'s' if len(preview.changes) != 1 else ''} "
            f"in {count} net class{'es' if count != 1 else ''}. "
            "The previous values are recorded; Restore writes them back.",
            status=status, entry_id=entry["id"], method=method, sent=True,
        )
    if status == STATUS_DAMAGED:
        message = _damaged(problems)
    elif status == STATUS_NOT_APPLIED:
        message = (
            "KiCad answered, but the net classes are unchanged: the write was not applied. "
            "This KiCad may not accept net-class changes through its API."
        )
    else:
        message = (
            "KiCad answered, but the net classes are not as sent, so the write is reported "
            "as not applied: " + "; ".join(problems) + ". Check the net classes in "
            "Board Setup; Restore writes the recorded values back."
        )
    return WriteResult(
        False, message, status=status, entry_id=entry["id"], method=method, sent=True,
        problems=tuple(problems),
    )


def send(project: Any, method: str, classes: list) -> None:
    """Send whole classes with merge mode ``MMM_MERGE``."""
    from kipy.proto.common.types import MapMergeMode

    if method == METHOD_SET_NET_CLASSES:
        from kipy.project_types import NetClass

        project.set_net_classes([NetClass(c) for c in classes], merge_mode=MapMergeMode.MMM_MERGE)
        return
    from google.protobuf.empty_pb2 import Empty
    from kipy.proto.common.commands import project_commands_pb2

    command = project_commands_pb2.SetNetClasses()
    command.merge_mode = MapMergeMode.MMM_MERGE
    command.net_classes.extend(classes)
    project._kicad.send(command, Empty)


def explicit_copy(netclass: Any, values: Mapping[str, int | None]) -> Any:
    """The class whole, as read, with *values* set, ``NCT_EXPLICIT`` and no constituents."""
    from kipy.proto.common.types import project_settings_pb2

    message = project_settings_pb2.NetClass()
    message.CopyFrom(_proto(netclass))
    for name, nm in values.items():
        if name not in FIELDS:
            raise ValueError(f"{name} is not a field the plugin writes")
        if nm is None:
            message.board.ClearField(name)
        else:
            getattr(message.board, name).value_nm = int(nm)
    message.type = project_settings_pb2.NetClassType.NCT_EXPLICIT
    message.ClearField("constituents")
    return message


def verify(
    before: list,
    after: list,
    wanted: Mapping[str, Mapping[str, int | None]],
    nets_before: Mapping[str, int] | None = None,
    nets_after: Mapping[str, int] | None = None,
) -> tuple:
    """``(status, problems)``: did the re-read classes become exactly what was sent?

    Every class not written must be unchanged. A written class must hold the
    wanted values in the three fields, be unchanged in every other setting,
    and still list itself as its only constituent (KiCad 10.0.6 left it with
    none). Every class in *nets_before* must hold as many nets in *nets_after*.
    A written class's type is only logged when it is not
    :data:`TYPE_AFTER_WRITE`.

    :data:`STATUS_APPLIED` when all of that holds (with *wanted* empty: when
    nothing changed); :data:`STATUS_NOT_APPLIED` when nothing changed at all;
    :data:`STATUS_MISMATCH` when only the three fields hold other values;
    :data:`STATUS_DAMAGED` for anything else.
    """
    from kipy.proto.common.types import project_settings_pb2

    types = project_settings_pb2.NetClassType
    old = {c.name: c for c in before}
    new = {c.name: c for c in after}
    damage = []  # a class not intact: the board must not be saved
    values = []  # the three fields hold other values than sent
    for name in sorted(set(old) - set(new)):
        damage.append(f"net class {name} disappeared")
    for name in sorted(set(new) - set(old)):
        damage.append(f"net class {name} appeared")
    unchanged = total = 0
    for name, proto in old.items():
        if name not in new:
            continue
        got = new[name]
        if name not in wanted:
            if not _same(proto, got):
                damage.append(f"net class {name} changed although it was not written")
            continue
        constituents = list(got.constituents)
        if constituents != [name]:
            damage.append(
                f"net class {name} no longer lists itself as its only constituent "
                f"(it lists {', '.join(constituents) or 'none'})"
            )
        if types.Name(got.type) != TYPE_AFTER_WRITE:
            log.warning(
                "Net class %s reads back as %s after the write, where a KiCad with the fix "
                "reports %s; not treated as a failure.", name, types.Name(got.type),
                TYPE_AFTER_WRITE,
            )
        if not _same(_other_settings(proto), _other_settings(got)):
            damage.append(f"other settings of net class {name} changed")
        seen = field_values(got)
        previous = field_values(proto)
        for key, value in wanted[name].items():
            total += 1
            if seen[key] != value:
                if seen[key] == previous[key]:
                    unchanged += 1
                values.append(
                    f"{name}'s {FIELD_LABELS[key].lower()} is {nm_text(seen[key])}, "
                    f"not {nm_text(value)}"
                )
    for name, count in (nets_before or {}).items():
        now = (nets_after or {}).get(name)
        if now != count:
            damage.append(f"net class {name} holds {'?' if now is None else now} nets, not {count}")
    if damage:
        return STATUS_DAMAGED, damage + values
    if not values:
        return STATUS_APPLIED, []
    if unchanged == total:
        return STATUS_NOT_APPLIED, values
    return STATUS_MISMATCH, values


def net_counts(board: Any, names: Iterable[str]) -> dict:
    """``{class: number of nets}`` for each of *names*.

    kicad-python 0.8's ``Board.get_nets(netclass_filter=name)``: KiCad's
    ``GetNets`` keeps a net when its class lists *name* among its constituents,
    which is what KiCad 10.0.6's write broke. Raises :class:`NetClassWriteError`
    when the nets cannot be read.
    """
    getter = getattr(board, "get_nets", None)
    if not callable(getter):
        raise NetClassWriteError(
            "the board's nets cannot be read, so the write could not be checked"
        )
    counts = {}
    for name in names:
        try:
            counts[name] = len(list(getter(netclass_filter=name)))
        except Exception as exc:  # KiCad did not answer
            raise NetClassWriteError(
                f"the nets of net class {name} could not be read ({type(exc).__name__}: {exc}), "
                "so the write could not be checked"
            ) from exc
    return counts


def read_classes(project: Any) -> list:
    """The project's net classes as independent ``project_settings_pb2.NetClass`` copies."""
    from kipy.proto.common.types import project_settings_pb2

    out = []
    for netclass in project.get_net_classes():
        message = project_settings_pb2.NetClass()
        message.CopyFrom(_proto(netclass))
        out.append(message)
    return out


def project_path(project: Any) -> str:
    """The key the history keeps a project's writes under."""
    path = getattr(project, "path", None) or ""
    return str(path) or str(getattr(project, "name", "") or NO_PROJECT_KEY)


# ── Restore ──────────────────────────────────────────────────────────────────


def restore_preview(
    project: Any,
    *,
    history_file: Path | None = None,
    project_key: str | None = None,
) -> tuple:
    """``(preview, None)`` for writing back the latest write not yet restored, else
    ``(None, reason)``."""
    key = project_key or project_path(project)
    entry = History(history_file or default_history_path()).last_restorable(key)
    if entry is None:
        return None, "There is no write by this plugin to restore for this project."
    proposals: dict = {}
    wrote: dict = {}
    for change in entry.get("changes") or ():
        proposals.setdefault(change["netclass"], {})[change["field"]] = change.get("before")
        wrote[(change["netclass"], change["field"])] = change.get("after")
    preview = make_preview(
        project, proposals, action=ACTION_RESTORE, restores=entry["id"],
        notes=[f"Restores the values recorded before the write of {entry.get('at', '?')}."],
    )
    notes = list(preview.notes)
    for change in preview.changes:
        written = wrote.get((change.netclass, change.field))
        if change.current_nm != written:
            notes.append(
                f"{change.netclass}'s {change.label.lower()} was changed after the plugin "
                f"wrote {nm_text(written)}; it is now {nm_text(change.current_nm)}."
            )
    return Preview(preview.changes, preview.classes, tuple(notes), ACTION_RESTORE,
                   entry["id"]), None


def restore(
    project: Any,
    confirm: Callable[[Preview], bool],
    *,
    version: tuple | None = None,
    board: Any = None,
    history_file: Path | None = None,
    project_key: str | None = None,
    clock: Callable[[], float] = time.time,
) -> WriteResult:
    """Write back the previous values of the latest write, and record the restore.

    Gated and verified as :func:`apply` is: on a KiCad that cannot take a
    write nothing is sent, though :func:`restore_preview` still reads the history.
    """
    method, reason = availability(project, version)
    if method is None:
        return WriteResult(False, reason)
    preview, reason = restore_preview(
        project, history_file=history_file, project_key=project_key
    )
    if preview is None:
        return WriteResult(False, reason)
    return apply(project, preview, confirm, version=version, board=board,
                 history_file=history_file, project_key=project_key, clock=clock)


# ── The history file ─────────────────────────────────────────────────────────


class History:
    """``history.json``: every write, per project path, oldest first.

    ``{"version": 1, "projects": {"<project path>": [entry, ...]}}``, where an
    entry is ``{id, action, at, status, method, restores, restoredBy?,
    problems?, changes: [{netclass, field, before, after}]}``, ``before``/``after``
    are nanometres (``null``: the field was not set) and ``problems`` is what
    the read-back found when the write is not ``applied``. The file is written
    owner-only, as the settings are.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": HISTORY_VERSION, "projects": {}}
        except (OSError, ValueError) as exc:
            raise NetClassWriteError(
                f"The write history at {self.path} cannot be read ({type(exc).__name__}); "
                "nothing is written until it can, so every write stays restorable."
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
            raise NetClassWriteError(
                f"The write history at {self.path} is not in the expected form; nothing is "
                "written until it is."
            )
        return data

    def entries(self, project_key: str) -> list:
        entries = self.load()["projects"].get(project_key) or []
        return [e for e in entries if isinstance(e, dict)]

    def append(self, project_key: str, entry: dict) -> dict:
        data = self.load()
        entry = dict(entry)
        entries = data["projects"].setdefault(project_key, [])
        entry["id"] = f"{len(entries) + 1}-{entry.get('at', 'write')}"
        entries.append(entry)
        write_private_json(self.path, data)
        return entry

    def update(self, project_key: str, entry_id: str, **fields: Any) -> None:
        data = self.load()
        for entry in data["projects"].get(project_key) or []:
            if isinstance(entry, dict) and entry.get("id") == entry_id:
                entry.update(fields)
        write_private_json(self.path, data)

    def update_quietly(self, project_key: str, entry_id: str, **fields: Any) -> None:
        """:meth:`update`, logging rather than raising: the write it records already happened."""
        try:
            self.update(project_key, entry_id, **fields)
        except (OSError, NetClassWriteError) as exc:
            log.warning("Could not update the write history at %s: %s", self.path, exc)

    def last_restorable(self, project_key: str) -> dict | None:
        """The latest write (not itself a restore) that took effect and is not yet restored."""
        for entry in reversed(self.entries(project_key)):
            if (
                entry.get("action") == ACTION_APPLY
                and entry.get("status") in RESTORABLE
                and not entry.get("restoredBy")
            ):
                return entry
        return None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _proto(netclass: Any) -> Any:
    return getattr(netclass, "proto", netclass)


def _proposed(preview: Preview) -> dict:
    wanted: dict = {}
    for change in preview.changes:
        wanted.setdefault(change.netclass, {})[change.field] = change.proposed_nm
    return wanted


def _other_settings(proto: Any) -> Any:
    """The class without the three fields, its type and its constituents, which
    :func:`verify` checks one by one."""
    copy = type(proto)()
    copy.CopyFrom(proto)
    for name in FIELDS:
        copy.board.ClearField(name)
    copy.ClearField("type")
    copy.ClearField("constituents")
    return copy


def _read_back(
    project: Any, board: Any, before: list, wanted: Mapping, nets_before: Mapping
) -> tuple:
    """:func:`verify` on the classes and nets read again; damaged when they cannot be read."""
    try:
        after = read_classes(project)
        nets_after = net_counts(board, nets_before)
    except Exception as exc:  # the connection lost, for one
        detail = str(exc) if isinstance(exc, NetClassWriteError) else (
            f"the net classes could not be read back ({type(exc).__name__}: {exc})"
        )
        return STATUS_DAMAGED, [detail]
    return verify(before, after, wanted, nets_before, nets_after)


def _damaged(problems: Iterable[str]) -> str:
    return f"{DO_NOT_SAVE} Found after the write: {'; '.join(problems)}."


def _same(a: Any, b: Any) -> bool:
    return a.SerializeToString(deterministic=True) == b.SerializeToString(deterministic=True)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
