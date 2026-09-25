# Manual tests

The unit tests cover what the plugin decides: the layer model, the class form, the budget, the key flow, the run, refusals, the net-class write and restore, the report, and the release files. They use a fake KiCad and a fake API. What they cannot reach is recorded here from real runs: KiCad launching the action, the wx dialog on screen, a real `SetNetClasses`, and an install from the Plugin and Content Manager.

## The dialog on KiCad 10.0 (task 4.1)

Not yet run. Record the platform, KiCad version, Python version and board for each run.

1. Launch **rftools.io: impedance and widths** from the PCB editor's toolbar.
2. **Layer model.** Every copper layer is listed and ticked as a plane (KiCad reports no layer types). Untick the signal layers. The structures change at once, and no API call is made.
3. **Key.** With no key saved, click **Get a free key**. The browser opens the dashboard with `client=kicad-plugin`. Paste the key and save it. The dialog shows only `rfc_` and 8 characters. On macOS and Linux, `settings.json` is mode 0600.
4. **Budget.** Select one class with a 50 Ω target. The budget line says at most 2 calls and the allowance remaining, before any calculation.
5. **Run.** The results show current, suggested, achieved, formula, range, engine and cache. Run again: every row says `cached`, and the allowance is unchanged.
6. **Apply.** On a KiCad later than 10.0.6 only. The preview shows three fields per class and bolds the changed ones. Cancel: Board Setup › Net Classes is unchanged. Apply: only that class's track width changes. Save the board, close and reopen the project, and check the width is kept.
7. **Restore.** On a KiCad later than 10.0.6 only. The previous width returns, and `history.json` records the apply and the restore.

| Date | Platform | KiCad | Python | Board | Steps passed | Notes |
|---|---|---|---|---|---|---|

## No write on KiCad 10.0.6

KiCad 10.0.6 corrupts a net class written through the API: the class loses its nets, and the next save hangs or crashes KiCad (spike, 2026-09-25). The plugin must not write there. Run this on KiCad 10.0.6 with a board whose classes hold nets, for example the demo `cm5_minima`.

1. Select a class with a 50 Ω target and run. The write button reads **Values for Board Setup…**, the restore button reads **Previous values…**, and the note above them says KiCad 10.0.6 and earlier corrupt a net class written through the API.
2. Click **Values for Board Setup…**. The window lists every selected class's Track Width, DP Width and DP Gap, current and proposed, in mm. It has **Copy values** and **Close**, and no write button.
3. Click **Copy values** and paste into a text editor. The same table arrives as tab-separated text, with a header naming Board Setup › Net Classes.
4. Board Setup › Net Classes is unchanged, `history.json` has no new entry, and the board saves promptly.
5. Enter the proposed width by hand in Board Setup and save. The class keeps its nets.

| Date | Platform | KiCad | Board | Steps passed | Notes |
|---|---|---|---|---|---|

## The write on the first KiCad release with the fix

Run this on the first release that contains KiCad commit `d622c37a` (a later 10.0.x or 11.x), before calling the write supported there. Use a board whose written class holds nets, for example `cm5_minima`'s `100ohm` class (26 nets).

1. Note how many nets the class holds, with `Board.get_nets(netclass_filter="100ohm")` from kicad-python or in the Net Inspector grouped by net class.
2. Apply a width to the class. The plugin reports the write as applied, not "Do not save the board". `history.json` records it as `applied`.
3. Save the board. The save returns promptly: KiCad does not hang or crash.
4. Close the board and the project, then reopen them. The class has the new width and still holds the same nets.
5. Restore, save, close and reopen again. The previous width returns, and the class still holds its nets.
6. If the plugin reports "The write did not apply correctly", close without saving, record what it found, and keep the version gate (`FIRST_SAFE_WRITE_AFTER` in `netclasses.py`) at or above that release.

| Date | Platform | KiCad | Board | Nets before / after | Save returned | Steps passed | Notes |
|---|---|---|---|---|---|---|---|

## Install from the custom repository (task 6.2)

Not yet run: it needs the repository public and `v0.1.0` released. On at least two platforms (one Windows or macOS, one Linux):

1. Add `https://antonpogrebenko-public.github.io/rftools-kicad/repository.json` in the Plugin and Content Manager, install the plugin, and apply.
2. Run G1, G2 and G3 on boards built from the stackups in `golden/kicad-golden.json`. Each result must match the golden file to a relative 1e-6.
3. Apply and restore one net class on a KiCad later than 10.0.6. On 10.0.6, check that the values are listed for Board Setup and nothing is written.
4. Record how many calls a first run of the four-layer reference board used, and name the board. A symmetric stack computes both outer layers in one call, so it uses 8 calls instead of 12.

| Date | Platform | KiCad | G1 | G2 | G3 | Apply / restore | First-run calls (board) |
|---|---|---|---|---|---|---|---|
