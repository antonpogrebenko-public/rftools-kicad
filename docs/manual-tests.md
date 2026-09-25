# Manual tests

The unit tests cover what the plugin decides: the layer model, the class form, the budget, the key flow, the run, refusals, the net-class write and restore, the report, and the release files. They use a fake KiCad and a fake API. What they cannot reach is recorded here from real runs: KiCad launching the action, the wx dialog on screen, a real `SetNetClasses`, and an install from the Plugin and Content Manager.

## The dialog on KiCad 10.0 (task 4.1)

Not yet run. Record the platform, KiCad version, Python version and board for each run.

1. Launch **rftools.io: impedance and widths** from the PCB editor's toolbar.
2. **Layer model.** Every copper layer is listed and ticked as a plane (KiCad reports no layer types). Untick the signal layers. The structures change at once, and no API call is made.
3. **Key.** With no key saved, click **Get a free key**. The browser opens the dashboard with `client=kicad-plugin`. Paste the key and save it. The dialog shows only `rfc_` and 8 characters. On macOS and Linux, `settings.json` is mode 0600.
4. **Budget.** Select one class with a 50 Ω target. The budget line says at most 2 calls and the allowance remaining, before any calculation.
5. **Run.** The results show current, suggested, achieved, formula, range, engine and cache. Run again: every row says `cached`, and the allowance is unchanged.
6. **Apply.** The preview shows three fields per class and bolds the changed ones. Cancel: Board Setup › Net Classes is unchanged. Apply: only that class's track width changes. Save the board, close and reopen the project, and check the width is kept.
7. **Restore.** The previous width returns, and `history.json` records the apply and the restore.

| Date | Platform | KiCad | Python | Board | Steps passed | Notes |
|---|---|---|---|---|---|---|

## Install from the custom repository (task 6.2)

Not yet run: it needs the repository public and `v0.1.0` released. On at least two platforms (one Windows or macOS, one Linux):

1. Add `https://antonpogrebenko-public.github.io/rftools-kicad/repository.json` in the Plugin and Content Manager, install the plugin, and apply.
2. Run G1, G2 and G3 on boards built from the stackups in `golden/kicad-golden.json`. Each result must match the golden file to a relative 1e-6.
3. Apply and restore one net class.
4. Record how many calls a first run of the four-layer reference board used, and name the board. A symmetric stack computes both outer layers in one call, so it uses 8 calls instead of 12.

| Date | Platform | KiCad | G1 | G2 | G3 | Apply / restore | First-run calls (board) |
|---|---|---|---|---|---|---|---|
