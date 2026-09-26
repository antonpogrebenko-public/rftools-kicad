# Manual tests

The unit tests cover what the plugin decides: the layer model, the class form, the budget, the key flow, the run, refusals, the net-class write and restore, the report, and the release files. They use a fake KiCad and a fake API; the API client's own tests run it over real HTTP against a local server. CI runs them on Python 3.9, 3.11, 3.12 and 3.13, and they also pass on KiCad's bundled Python 3.9.13 (macOS) with its wx, where the dialog tests run for real. What they cannot reach is recorded here from real runs: KiCad building the plugin's environment and launching the action, the wx dialog on screen, HTTPS from KiCad's own Python, a real `SetNetClasses`, and an install from the Plugin and Content Manager.

## Setup: KiCad's own Python

The plugin runs on the interpreter KiCad uses by default (design Decision 7a): the bundled Python 3.9.13 on macOS, the bundled `pythonw.exe` 3.11.5 on Windows, and the system `python3` on Linux. Check this before every other section, on each platform.

1. In Preferences › Plugins, **Python Interpreter** is KiCad's own. If another was set (earlier spikes set python.org 3.12 on macOS), click **Detect Automatically**. On macOS it reads `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3`.
2. On Linux, `python3-venv` and `python3-wxgtk4.0` are installed.
3. Right-click the plugin under Action Plugins, choose **Recreate Plugin Environment** (or delete the environment folder listed in the README), and restart KiCad.
4. The plugin's environment is built from that interpreter: `<env>/bin/python --version` (`<env>\Scripts\python.exe` on Windows) gives 3.9.13 on macOS, 3.11.5 on Windows, or the system version on Linux. `<env>/bin/python -m pip list` shows `kicad-python` and `certifi` (on macOS `certifi` may come from KiCad's own site packages instead) and no `rftools-io`.
5. The action **rftools.io: impedance and widths** is on the PCB editor's toolbar, and the dialog opens (the report in the browser means wx did not import).
6. With a key saved, the budget line shows the allowance remaining. That read went over HTTPS from KiCad's Python with certifi's certificates: on macOS the bundled Python has none of its own. Checked from the command line on 2026-09-25: the bundled 3.9.13 trusts 0 certificates by default and 121 with the plugin's context, and `GET /api/py/v1/usage` without a key answered 401 with the key link.

| Date | Platform | KiCad | Interpreter (version) | Environment built | Dialog | HTTPS | Notes |
|---|---|---|---|---|---|---|---|

## The dialog on KiCad 10.0 (task 4.1)

Not yet run. Record the platform, KiCad version, Python version (KiCad's own, from the setup above) and board for each run.

1. Launch **rftools.io: impedance and widths** from the PCB editor's toolbar.
2. **Layer model.** Every copper layer is listed and ticked as a plane (KiCad reports no layer types). Untick the signal layers. The structures change at once, and no API call is made.
3. **Key.** With no key saved, click **Get a free key**. The browser opens the dashboard with `client=kicad-plugin`. Paste the key and save it. The dialog shows only `rfc_` and 8 characters. On macOS and Linux, `settings.json` is mode 0600.
4. **Budget.** Select one class with a 50 Ω target. The budget line says at most 2 calls and the allowance remaining, before any calculation.
5. **Run.** The results show current, suggested, achieved, formula, range, engine and cache. Run again: every row says `cached`, and the allowance is unchanged.
6. **Apply.** On a KiCad later than 10.0.6 only. The preview shows three fields per class and bolds the changed ones. Cancel: Board Setup › Net Classes is unchanged. Apply: only that class's track width changes. Save the board, close and reopen the project, and check the width is kept.
7. **Restore.** On a KiCad later than 10.0.6 only. The previous width returns, and `history.json` records the apply and the restore.

| Date | Platform | KiCad | Python | Board | Steps passed | Notes |
|---|---|---|---|---|---|---|

### Run 1 — macOS, KiCad 10.0.6, 2026-09-26

Machine: Apple silicon Mac. KiCad 10.0.6 from the official installer, with the API enabled and the interpreter left as KiCad's bundled Python 3.9.13. The package was installed with Install from File.

What happened:
- **Environment:** it built with the bundled 3.9.13 at 12:06, holding `kicad-python` 0.8.0 plus `protobuf` and `pynng`. `certifi` was already satisfied from the user's own Python 3.9 site-packages.
- **Toolbar button:** absent from the PCB editor that was already open. It appeared after quitting KiCad and reopening it. KiCad 10.0.6 adds a plugin's button only to a PCB editor opened after the plugin is ready. The README now says so.
- **"Get a free key":** opened nothing, because Python's `webbrowser` goes through AppleScript and fails silently in a process KiCad starts. Fixed in `aff9e63` (`browser.open_url`); this run used the key link directly. The fixed button is still to be checked on the next install.
- **The run:** a key was saved, and one class was run on `F.Cu` with Z₀ target 50 Ω. The board uses KiCad's default two-layer stackup (1.51 mm FR-4, εr 4.5, 35 µm), which is golden case G1. The plugin's result cache shows 2 calls:
  - the current width, 0.2 mm → 132.82412963196327 Ω;
  - the solve → **2.784 mm**, reached, 68 evaluations, 50.0001127254694 Ω. Both equal `golden/kicad-golden.json` G1 exactly.

  Both results carry `api@0c02e01f0c4d` and a `validRange` of `inside`. "Values for Board Setup…" listed the proposal, and nothing was written, since KiCad 10.0.6 blocks the write.

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

Not yet run: it needs the repository public and `v0.1.0` released. On at least two platforms (one Windows or macOS, one Linux), with the interpreter left at KiCad's default:

1. Add `https://antonpogrebenko-public.github.io/rftools-kicad/repository.json` in the Plugin and Content Manager, install the plugin, and apply. Nothing else is installed first: no Python, no packages (on Linux, only the distribution's `python3-venv` and `python3-wxgtk4.0`).
2. Run G1, G2 and G3 on boards built from the stackups in `golden/kicad-golden.json`. Each result must match the golden file to a relative 1e-6.
3. Apply and restore one net class on a KiCad later than 10.0.6. On 10.0.6, check that the values are listed for Board Setup and nothing is written.
4. Record how many calls a first run of the four-layer reference board used, and name the board. A symmetric stack computes both outer layers in one call, so it uses 8 calls instead of 12.

| Date | Platform | KiCad | G1 | G2 | G3 | Apply / restore | First-run calls (board) |
|---|---|---|---|---|---|---|---|
