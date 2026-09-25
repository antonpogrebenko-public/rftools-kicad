"""Net-class writes: gated by KiCad's version, previewed, confirmed, whole-class explicit
MMM_MERGE, verified (fields, constituents, nets), restorable."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import stat
import sys

import pytest

from rftools_kicad import netclasses as N
from tests.support import (
    BROKEN_KICAD,
    FIXED_KICAD,
    NETS_PER_CLASS,
    FakeBoard,
    FakeKiCad,
    FakeProject,
    FakeProject09,
    sample_classes,
)

HIGH_TO_029 = {"HighSpeed": {N.TRACK_WIDTH: 290000}}


@pytest.fixture
def kicad():
    return FakeKiCad(sample_classes())


@pytest.fixture
def project(kicad):
    return FakeProject(kicad)


@pytest.fixture
def history(tmp_path):
    return tmp_path / "config" / "history.json"


def yes(preview):
    return True


def no(preview):
    return False


def apply(project, preview, confirm=yes, **kwargs):
    """``N.apply`` on a KiCad that takes writes (unless told otherwise), with its board."""
    kwargs.setdefault("version", FIXED_KICAD)
    kwargs.setdefault("board", FakeBoard(project._kicad))
    return N.apply(project, preview, confirm, **kwargs)


def restore(project, confirm=yes, **kwargs):
    kwargs.setdefault("version", FIXED_KICAD)
    kwargs.setdefault("board", FakeBoard(project._kicad))
    return N.restore(project, confirm, **kwargs)


def after_send(kicad, change):
    """Let *kicad* take each command as it would, then call ``change(kicad)``."""
    real_send = kicad.send

    def send(command, response_type):
        answer = real_send(command, response_type)
        change(kicad)
        return answer

    kicad.send = send


def entries(history, key="/work/rf-board"):
    return json.loads(history.read_text(encoding="utf-8"))["projects"][key]


def without_target_fields(serialized: bytes) -> bytes:
    from kipy.proto.common.types import project_settings_pb2

    message = project_settings_pb2.NetClass()
    message.ParseFromString(serialized)
    for name in N.FIELDS:
        message.board.ClearField(name)
    return message.SerializeToString(deterministic=True)


# ── Preview ──────────────────────────────────────────────────────────────────


def test_mm_results_become_whole_nanometres():
    assert N.mm_to_nm(0.29) == 290000
    assert N.mm_to_nm(2.784) == 2784000
    assert N.mm_to_nm(0.1 + 0.2) == 300000


def test_the_preview_lists_current_and_proposed_for_each_selected_class(project):
    preview = N.make_preview(project, {
        "HighSpeed": {N.TRACK_WIDTH: 290000, N.DIFF_PAIR_GAP: 250000},  # gap unchanged
        "USB": {N.DIFF_PAIR_GAP: 120000},
    })
    assert [(c.netclass, c.field, c.current_nm, c.proposed_nm) for c in preview.changes] == [
        ("HighSpeed", N.TRACK_WIDTH, 200000, 290000),
        ("USB", N.DIFF_PAIR_GAP, 154000, 120000),
    ]
    assert preview.class_names == ("HighSpeed", "USB")
    [high, usb] = preview.classes
    assert high.rows() == [
        ("Track width", "0.2 mm", "0.29 mm", True),
        ("Diff pair width", "0.13 mm", "unchanged", False),
        ("Diff pair gap", "0.25 mm", "unchanged", False),
    ]
    assert usb.rows()[2] == ("Diff pair gap", "0.154 mm", "0.12 mm", True)
    assert "HighSpeed: track width 0.2 mm → 0.29 mm" in preview.text()


def test_a_preview_names_a_class_the_project_no_longer_has(project):
    preview = N.make_preview(project, {"Gone": {N.TRACK_WIDTH: 100000}})
    assert preview.empty
    assert any("Gone is no longer in the project" in n for n in preview.notes)


def test_only_the_three_fields_can_be_proposed(project):
    with pytest.raises(ValueError, match="only track_width"):
        N.make_preview(project, {"HighSpeed": {"clearance": 100000}})


# ── Apply ────────────────────────────────────────────────────────────────────


def test_cancel_sends_nothing_and_records_nothing(project, kicad, history):
    before = kicad.serialized()
    preview = N.make_preview(project, HIGH_TO_029)
    result = apply(project, preview, no, history_file=history)
    assert not result.applied and result.cancelled and not result.sent
    assert "nothing was written" in result.message
    assert kicad.sent == []
    assert not history.exists()
    assert kicad.serialized() == before


def test_the_write_changes_only_the_target_fields_of_the_selected_class(project, kicad, history):
    before = kicad.serialized()
    preview = N.make_preview(project, {
        "HighSpeed": {N.TRACK_WIDTH: 290000, N.DIFF_PAIR_WIDTH: 120000, N.DIFF_PAIR_GAP: 180000},
    })
    result = apply(project, preview, yes, history_file=history)
    assert result.applied, result.message
    assert result.status == N.STATUS_APPLIED and result.method == N.METHOD_COMMAND
    after = kicad.serialized()
    # Every unselected class is byte-identical.
    for name in ("Default", "USB", "Power"):
        assert after[name] == before[name], name
    # The written class differs only in the three fields...
    assert after["HighSpeed"] != before["HighSpeed"]
    assert without_target_fields(after["HighSpeed"]) == without_target_fields(before["HighSpeed"])
    # ...which hold the proposed values.
    assert N.field_values(kicad.classes["HighSpeed"]) == {
        N.TRACK_WIDTH: 290000, N.DIFF_PAIR_WIDTH: 120000, N.DIFF_PAIR_GAP: 180000,
    }


def test_the_sent_class_is_whole_explicit_and_without_constituents(project, kicad, history):
    from kipy.proto.common.commands import project_commands_pb2
    from kipy.proto.common.types import MapMergeMode, project_settings_pb2

    read = {p.name: p for p in N.read_classes(project)}
    apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    [command] = kicad.sent
    assert isinstance(command, project_commands_pb2.SetNetClasses)
    assert command.merge_mode == MapMergeMode.MMM_MERGE
    [sent] = command.net_classes  # only the changed class is sent
    assert sent.name == "HighSpeed"
    assert sent.type == project_settings_pb2.NetClassType.NCT_EXPLICIT
    assert list(sent.constituents) == []
    # Whole, as read: identical to the read class but for the field, the type
    # and the constituents.
    expected = project_settings_pb2.NetClass()
    expected.CopyFrom(read["HighSpeed"])
    expected.board.track_width.value_nm = 290000
    expected.type = project_settings_pb2.NetClassType.NCT_EXPLICIT
    del expected.constituents[:]
    assert sent.SerializeToString(deterministic=True) == expected.SerializeToString(
        deterministic=True
    )
    assert sent.priority == 0 and sent.board.color.b == 1.0
    assert sent.board.clearance.value_nm == 200000


def test_the_previous_values_are_recorded_before_the_write_is_sent(project, kicad, history):
    seen = []

    def on_send(command):
        seen.append(entries(history))

    kicad.on_send = on_send
    apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    [[entry]] = seen
    assert entry["action"] == "apply" and entry["status"] == N.STATUS_SENDING
    assert entry["changes"] == [
        {"netclass": "HighSpeed", "field": "track_width", "before": 200000, "after": 290000},
    ]
    [entry] = entries(history)
    assert entry["status"] == N.STATUS_APPLIED


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX file modes")
def test_the_history_file_is_readable_by_its_owner_only(project, history):
    apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    assert stat.S_IMODE(os.stat(history).st_mode) == 0o600


def test_kicad_python_0_9_writes_through_set_net_classes(kicad, history):
    from kipy.proto.common.types import MapMergeMode, project_settings_pb2

    project = FakeProject09(kicad)
    before = kicad.serialized()
    result = apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    assert result.applied and result.method == N.METHOD_SET_NET_CLASSES
    [(classes, mode)] = project.set_calls
    assert mode == MapMergeMode.MMM_MERGE
    [sent] = classes
    assert sent.proto.type == project_settings_pb2.NetClassType.NCT_EXPLICIT
    assert sent.constituents is None  # kicad-python reports none for an explicit class
    after = kicad.serialized()
    assert all(after[n] == before[n] for n in ("Default", "USB", "Power"))


def implicit_write(project):
    from kipy.proto.common.commands import project_commands_pb2
    from kipy.proto.common.types import MapMergeMode, project_settings_pb2

    [high] = [p for p in N.read_classes(project) if p.name == "HighSpeed"]
    high.board.track_width.value_nm = 290000
    high.type = project_settings_pb2.NetClassType.NCT_IMPLICIT
    command = project_commands_pb2.SetNetClasses(merge_mode=MapMergeMode.MMM_MERGE)
    command.net_classes.append(high)
    return command


def test_a_class_sent_back_implicit_would_be_silently_dropped_by_10_0_6():
    # The reason for the explicit rule, as the fake reproduces KiCad 10.0.6:
    # every class reads back implicit, and one sent back so is dropped.
    from google.protobuf.empty_pb2 import Empty
    from kipy.proto.common.types import project_settings_pb2

    kicad = FakeKiCad(sample_classes(), version=BROKEN_KICAD)
    project = FakeProject(kicad)
    assert all(p.type == project_settings_pb2.NetClassType.NCT_IMPLICIT
               for p in N.read_classes(project))
    before = kicad.serialized()
    kicad.send(implicit_write(project), Empty)  # answers success
    assert kicad.serialized() == before


def test_a_class_sent_implicit_is_refused_by_a_fixed_kicad(project, kicad):
    from google.protobuf.empty_pb2 import Empty
    from kipy.errors import ApiError
    from kipy.proto.common.types import project_settings_pb2

    # A KiCad with d622c37a reports a one-constituent class as explicit.
    assert all(p.type == project_settings_pb2.NetClassType.NCT_EXPLICIT
               and list(p.constituents) == [p.name] for p in N.read_classes(project))
    with pytest.raises(ApiError, match="could not unpack netclass 'HighSpeed'"):
        kicad.send(implicit_write(project), Empty)


def test_a_write_kicad_silently_ignored_is_reported_as_not_applied(kicad, history):
    kicad.ignore = True
    project = FakeProject(kicad)
    before = kicad.serialized()
    result = apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    assert not result.applied and result.sent
    assert result.status == N.STATUS_NOT_APPLIED
    assert "not applied" in result.message and "unchanged" in result.message
    assert kicad.serialized() == before
    [entry] = entries(history)
    assert entry["status"] == N.STATUS_NOT_APPLIED
    # Nothing took effect, so there is nothing to restore.
    preview, reason = N.restore_preview(project, history_file=history)
    assert preview is None and "no write" in reason


def test_a_write_that_lands_differently_is_reported_as_not_applied(kicad, history):
    project = FakeProject(kicad)

    def mangle(command):
        # KiCad takes the class but rounds the width to another value.
        command.net_classes[0].board.track_width.value_nm = 300000

    real_send = kicad.send

    def send(command, response_type):
        mangle(command)
        return real_send(command, response_type)

    kicad.send = send
    result = apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    assert not result.applied and result.status == N.STATUS_MISMATCH
    assert "HighSpeed's track width is 0.3 mm, not 0.29 mm" in result.message
    # Something changed, so it can be restored.
    preview, _ = N.restore_preview(project, history_file=history)
    assert preview is not None


def test_kicad_refusing_the_command_is_reported_and_changes_nothing(kicad, history):
    from kipy.errors import ApiError

    kicad.refuse = ApiError("net classes are read-only here")
    project = FakeProject(kicad)
    before = kicad.serialized()
    result = apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    assert not result.applied and result.status == N.STATUS_REFUSED
    assert "KiCad refused the write" in result.message and "read-only" in result.message
    assert kicad.serialized() == before
    assert entries(history)[0]["status"] == N.STATUS_REFUSED


def test_a_class_edited_in_kicad_after_the_preview_is_not_overwritten(project, kicad, history):
    preview = N.make_preview(project, HIGH_TO_029)
    kicad.classes["HighSpeed"].board.track_width.value_nm = 210000  # the user edits it
    result = apply(project, preview, yes, history_file=history)
    assert not result.applied and not result.sent
    assert "changed in KiCad since the preview" in result.message
    assert "0.21 mm" in result.message
    assert kicad.sent == []


def test_an_empty_preview_sends_nothing(project, kicad, history):
    preview = N.make_preview(project, {"HighSpeed": {N.TRACK_WIDTH: 200000}})  # already so
    result = apply(project, preview, yes, history_file=history)
    assert not result.applied and "Nothing to write" in result.message
    assert kicad.sent == []


def test_an_unreadable_history_blocks_the_write(project, kicad, history):
    history.parent.mkdir(parents=True)
    history.write_text("{not json", encoding="utf-8")
    result = apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    assert not result.applied and "cannot be read" in result.message
    assert kicad.sent == []


# ── Unavailable ──────────────────────────────────────────────────────────────


def test_no_project_is_unavailable_with_a_reason(history):
    method, reason = N.availability(None, FIXED_KICAD)
    assert method is None and "No KiCad project" in reason
    result = N.apply(None, N.Preview(()), yes, version=FIXED_KICAD, history_file=history)
    assert not result.applied and "No KiCad project" in result.message


def test_a_library_with_neither_path_is_unavailable_with_a_reason(monkeypatch, kicad, history):
    from kipy.proto.common.commands import project_commands_pb2

    monkeypatch.delattr(project_commands_pb2, "SetNetClasses")
    project = FakeProject(kicad)
    method, reason = N.availability(project, FIXED_KICAD)
    assert method is None
    assert "neither Project.set_net_classes nor the SetNetClasses command" in reason
    assert "kicad-python to 0.9" in reason
    confirmed = []
    result = apply(project, N.Preview((N.FieldChange("HighSpeed", N.TRACK_WIDTH, 1, 2),)),
                   lambda p: confirmed.append(p) or True, history_file=history)
    assert not result.applied and confirmed == [] and kicad.sent == []


def test_a_project_without_a_command_channel_is_unavailable(kicad):
    class Bare:
        path = "/x"

        def get_net_classes(self):
            return []

    method, reason = N.availability(Bare(), FIXED_KICAD)
    assert method is None and "no way to send" in reason


# ── Restore ──────────────────────────────────────────────────────────────────


def test_restore_writes_the_recorded_values_back_and_records_itself(project, kicad, history):
    original = kicad.serialized()
    apply(project, N.make_preview(project, {
        "HighSpeed": {N.TRACK_WIDTH: 290000},
        "USB": {N.DIFF_PAIR_GAP: 120000},
    }), yes, history_file=history)
    assert kicad.serialized() != original

    preview, reason = N.restore_preview(project, history_file=history)
    assert reason is None and preview.action == N.ACTION_RESTORE
    assert [(c.netclass, c.field, c.current_nm, c.proposed_nm) for c in preview.changes] == [
        ("HighSpeed", N.TRACK_WIDTH, 290000, 200000),
        ("USB", N.DIFF_PAIR_GAP, 120000, 154000),
    ]
    result = restore(project, yes, history_file=history)
    assert result.applied, result.message
    assert "Restored 2 values in 2 net classes" in result.message
    assert kicad.serialized() == original  # every class exactly as before

    # The restore went out the same way: whole, explicit, MMM_MERGE.
    from kipy.proto.common.types import project_settings_pb2

    restore_command = kicad.sent[-1]
    assert [c.name for c in restore_command.net_classes] == ["HighSpeed", "USB"]
    assert all(c.type == project_settings_pb2.NetClassType.NCT_EXPLICIT
               for c in restore_command.net_classes)

    first, second = entries(history)
    assert first["action"] == "apply" and first["restoredBy"] == second["id"]
    assert second["action"] == "restore" and second["restores"] == first["id"]
    assert second["status"] == N.STATUS_APPLIED
    assert second["changes"][0] == {
        "netclass": "HighSpeed", "field": "track_width", "before": 290000, "after": 200000,
    }
    # Nothing left to restore.
    assert N.restore_preview(project, history_file=history)[0] is None


def test_restores_walk_back_through_successive_writes(project, kicad, history):
    apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    apply(project, N.make_preview(project, {"HighSpeed": {N.TRACK_WIDTH: 310000}}), yes,
            history_file=history)
    assert restore(project, yes, history_file=history).applied
    assert N.field_values(kicad.classes["HighSpeed"])[N.TRACK_WIDTH] == 290000
    assert restore(project, yes, history_file=history).applied
    assert N.field_values(kicad.classes["HighSpeed"])[N.TRACK_WIDTH] == 200000


def test_restore_is_kept_per_project(kicad, history):
    one = FakeProject(kicad, path="/work/one")
    apply(one, N.make_preview(one, HIGH_TO_029), yes, history_file=history)
    other = FakeProject(kicad, path="/work/other")
    preview, reason = N.restore_preview(other, history_file=history)
    assert preview is None and "no write" in reason
    assert N.restore_preview(one, history_file=history)[0] is not None


def test_restore_notes_a_value_changed_after_the_write(project, kicad, history):
    apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    kicad.classes["HighSpeed"].board.track_width.value_nm = 300000
    preview, _ = N.restore_preview(project, history_file=history)
    assert any("changed after the plugin wrote 0.29 mm; it is now 0.3 mm" in n
               for n in preview.notes)


def test_a_cancelled_restore_changes_nothing(project, kicad, history):
    apply(project, N.make_preview(project, HIGH_TO_029), yes, history_file=history)
    written = kicad.serialized()
    sent = len(kicad.sent)
    result = restore(project, no, history_file=history)
    assert result.cancelled and len(kicad.sent) == sent and kicad.serialized() == written
    assert len(entries(history)) == 1


def test_restoring_an_unset_field_clears_it(kicad, history):
    project = FakeProject(kicad)
    # Power has no differential-pair fields set.
    assert N.field_values(kicad.classes["Power"])[N.DIFF_PAIR_GAP] is None
    apply(project, N.make_preview(project, {"Power": {N.DIFF_PAIR_GAP: 200000}}), yes,
            history_file=history)
    assert N.field_values(kicad.classes["Power"])[N.DIFF_PAIR_GAP] == 200000
    assert restore(project, yes, history_file=history).applied
    assert N.field_values(kicad.classes["Power"])[N.DIFF_PAIR_GAP] is None


# ── The version gate ─────────────────────────────────────────────────────────


def test_the_version_is_read_from_kicad_get_version():
    from kipy.kicad import KiCadVersion

    assert N.kicad_version(FakeKiCad([], version=(10, 0, 6))) == (10, 0, 6)
    assert N.kicad_version(FakeKiCad([], version=(11, 0, 0))) == (11, 0, 0)
    assert N.kicad_version(FakeKiCad([], version=None)) is None  # GetVersion not answered
    assert N.kicad_version(None) is None

    class Empty:
        def get_version(self):
            return KiCadVersion(0, 0, 0, "")

    assert N.kicad_version(Empty()) is None


def test_the_gate_is_one_constant_strictly_after_10_0_6():
    assert N.FIRST_SAFE_WRITE_AFTER == (10, 0, 6)
    assert N.write_blocked((10, 0, 6)) is not None
    assert N.write_blocked((10, 0, 7)) is None


@pytest.mark.parametrize("version", [(10, 0, 6), (10, 0, 5), (10, 0, 0), (9, 0, 4)])
def test_kicad_10_0_6_and_earlier_is_never_written(version, project, kicad, history):
    method, reason = N.availability(project, version)
    assert method is None
    assert f"This is KiCad {N.version_text(version)}." in reason
    assert "KiCad 10.0.6 and earlier corrupt a net class written through the API" in reason
    assert "loses its nets" in reason and "the next save can hang or crash KiCad" in reason
    assert "Board Setup › Net Classes" in reason
    assert "turns on with the next KiCad release" in reason
    confirmed = []
    result = apply(project, N.make_preview(project, HIGH_TO_029),
                   lambda p: confirmed.append(p) or True, version=version, history_file=history)
    assert not result.applied and not result.sent and result.message == reason
    assert confirmed == [] and kicad.sent == [] and not history.exists()


@pytest.mark.parametrize("version", [(10, 0, 7), (10, 1, 0), (11, 0, 0)])
def test_a_kicad_after_10_0_6_is_written(version, project, kicad, history):
    assert N.availability(project, version) == (N.METHOD_COMMAND, None)
    result = apply(project, N.make_preview(project, HIGH_TO_029), version=version,
                   history_file=history)
    assert result.applied, result.message
    assert N.field_values(kicad.classes["HighSpeed"])[N.TRACK_WIDTH] == 290000


def test_a_version_that_cannot_be_read_is_never_written(project, kicad, history):
    method, reason = N.availability(project, None)
    assert method is None
    assert reason.startswith("KiCad's version could not be read, so net classes are not written.")
    assert "KiCad 10.0.6 and earlier corrupt" in reason
    # Leaving the version out is the same as not being able to read it.
    result = N.apply(project, N.make_preview(project, HIGH_TO_029), yes,
                     board=FakeBoard(kicad), history_file=history)
    assert not result.applied and result.message == reason
    assert kicad.sent == []


def test_restore_is_unavailable_on_10_0_6_and_the_history_stays_readable(project, kicad,
                                                                         history):
    assert apply(project, N.make_preview(project, HIGH_TO_029), history_file=history).applied
    sent = len(kicad.sent)
    result = restore(project, version=BROKEN_KICAD, history_file=history)
    assert not result.applied and not result.sent
    assert "KiCad 10.0.6 and earlier corrupt" in result.message
    assert len(kicad.sent) == sent
    preview, reason = N.restore_preview(project, history_file=history)
    assert reason is None
    assert [(c.netclass, c.current_nm, c.proposed_nm) for c in preview.changes] == [
        ("HighSpeed", 290000, 200000),
    ]
    assert entries(history)[0].get("restoredBy") is None


# ── Read-back verification ───────────────────────────────────────────────────


def test_a_write_to_a_kicad_with_the_10_0_6_fault_is_caught(history):
    # A build numbered above 10.0.6 but made before d622c37a passes the gate;
    # the fake reproduces what 10.0.6 does with the write (spike, 2026-09-25).
    kicad = FakeKiCad(sample_classes(), version=BROKEN_KICAD)
    project = FakeProject(kicad)
    result = apply(project, N.make_preview(project, HIGH_TO_029), version=FIXED_KICAD,
                   history_file=history)
    assert not result.applied and result.sent and result.status == N.STATUS_DAMAGED
    assert result.message.startswith(
        "The write did not apply correctly. Do not save the board; close it without saving "
        "and reopen it."
    )
    assert ("net class HighSpeed no longer lists itself as its only constituent "
            "(it lists none)") in result.message
    assert f"net class HighSpeed holds 0 nets, not {NETS_PER_CLASS}" in result.message
    [entry] = entries(history)
    assert entry["status"] == N.STATUS_DAMAGED
    assert any("holds 0 nets" in p for p in entry["problems"])
    # Closing without saving discards the write, so there is nothing to restore.
    preview, reason = N.restore_preview(project, history_file=history)
    assert preview is None and "no write" in reason


def test_a_written_class_that_loses_its_constituent_is_reported(project, kicad, history):
    def extra_constituent(k):
        # Its nets stay (the class still lists itself), but not alone.
        k.classes["HighSpeed"].constituents.append("Default")

    after_send(kicad, extra_constituent)
    result = apply(project, N.make_preview(project, HIGH_TO_029), history_file=history)
    assert result.status == N.STATUS_DAMAGED and not result.applied
    assert N.DO_NOT_SAVE in result.message
    assert ("net class HighSpeed no longer lists itself as its only constituent "
            "(it lists HighSpeed, Default)") in result.message
    assert "nets, not" not in result.message
    assert entries(history)[0]["status"] == N.STATUS_DAMAGED


def test_a_written_class_whose_nets_change_is_reported(project, kicad, history):
    def lose_a_net(k):
        k.nets["HighSpeed"] -= 1

    after_send(kicad, lose_a_net)
    result = apply(project, N.make_preview(project, HIGH_TO_029), history_file=history)
    assert result.status == N.STATUS_DAMAGED
    assert N.DO_NOT_SAVE in result.message
    assert f"net class HighSpeed holds {NETS_PER_CLASS - 1} nets, not {NETS_PER_CLASS}" in (
        result.message
    )
    assert "constituent" not in result.message


def test_the_nets_are_counted_per_written_class_before_and_after(project, kicad, history):
    board = FakeBoard(kicad)
    apply(project, N.make_preview(project, {
        "HighSpeed": {N.TRACK_WIDTH: 290000}, "USB": {N.DIFF_PAIR_GAP: 120000},
    }), board=board, history_file=history)
    assert board.calls == ["HighSpeed", "USB", "HighSpeed", "USB"]


def test_a_written_class_of_another_type_is_logged_not_failed(project, kicad, history, caplog):
    from kipy.proto.common.types import project_settings_pb2

    def implicit(k):
        k.classes["HighSpeed"].type = project_settings_pb2.NetClassType.NCT_IMPLICIT

    after_send(kicad, implicit)
    with caplog.at_level(logging.WARNING, logger="rftools_kicad.netclasses"):
        result = apply(project, N.make_preview(project, HIGH_TO_029), history_file=history)
    assert result.applied, result.message
    assert any("HighSpeed reads back as NCT_IMPLICIT" in r.getMessage() for r in caplog.records)


def test_nets_that_cannot_be_counted_block_the_write(project, kicad, history):
    from kipy.errors import ApiError

    board = FakeBoard(kicad, refuse=ApiError("GetNets is not answered"))
    result = apply(project, N.make_preview(project, HIGH_TO_029), board=board,
                   history_file=history)
    assert not result.applied and not result.sent
    assert result.message.startswith("Nothing was written: the nets of net class HighSpeed "
                                      "could not be read")
    assert kicad.sent == [] and not history.exists()
    result = apply(project, N.make_preview(project, HIGH_TO_029), board=None,
                   history_file=history)
    assert not result.applied and "the board's nets cannot be read" in result.message
    assert kicad.sent == []


def test_a_read_back_that_fails_is_reported(project, kicad, history):
    from kipy.errors import ApiError

    board = FakeBoard(kicad)

    def gone(k):
        board.refuse = ApiError("KiCad stopped answering")

    after_send(kicad, gone)
    result = apply(project, N.make_preview(project, HIGH_TO_029), board=board,
                   history_file=history)
    assert result.status == N.STATUS_DAMAGED and result.sent
    assert N.DO_NOT_SAVE in result.message and "KiCad stopped answering" in result.message
    assert entries(history)[0]["status"] == N.STATUS_DAMAGED


def test_a_refusal_after_part_of_the_write_took_effect_is_reported(project, kicad, history):
    from google.protobuf.empty_pb2 import Empty
    from kipy.errors import ApiError

    real_send = kicad.send

    def send(command, response_type):
        # KiCad changes each class in place as it unpacks it: the first lands,
        # then the command is refused.
        first = type(command)()
        first.CopyFrom(command)
        del first.net_classes[1:]
        real_send(first, Empty)
        raise ApiError("could not unpack netclass 'USB'")

    kicad.send = send
    result = apply(project, N.make_preview(project, {
        "HighSpeed": {N.TRACK_WIDTH: 290000}, "USB": {N.DIFF_PAIR_GAP: 120000},
    }), history_file=history)
    assert result.status == N.STATUS_DAMAGED and not result.applied
    assert result.message.startswith(
        "KiCad refused the write (ApiError: could not unpack netclass 'USB'). " + N.DO_NOT_SAVE
    )
    assert "net class HighSpeed changed although it was not written" in result.message
    assert entries(history)[0]["status"] == N.STATUS_DAMAGED


# ── Entering the values by hand ──────────────────────────────────────────────


def test_a_manual_preview_lists_every_field_in_board_setups_columns(project):
    preview = N.make_preview(project, {
        "HighSpeed": {N.TRACK_WIDTH: 290000},
        "Power": {N.DIFF_PAIR_GAP: 200000},
    })
    text = preview.manual_text()
    lines = text.splitlines()
    assert lines[0] == ("Net-class values proposed by the rftools.io plugin, to enter in "
                        "KiCad's Board Setup › Net Classes:")
    assert lines[1] == "Net class\tColumn\tCurrent\tEnter"
    assert lines[2:8] == [
        "HighSpeed\tTrack Width\t0.2 mm\t0.29 mm",
        "HighSpeed\tDP Width\t0.13 mm\tunchanged",
        "HighSpeed\tDP Gap\t0.25 mm\tunchanged",
        "Power\tTrack Width\t0.5 mm\tunchanged",
        "Power\tDP Width\tnot set\tunchanged",
        "Power\tDP Gap\tnot set\t0.2 mm",
    ]


def test_a_manual_preview_is_never_written(project, kicad, history):
    preview = dataclasses.replace(N.make_preview(project, HIGH_TO_029), manual="entered by hand")
    confirmed = []
    result = apply(project, preview, lambda p: confirmed.append(p) or True,
                   history_file=history)
    assert not result.applied and result.message == "entered by hand"
    assert confirmed == [] and kicad.sent == []
