# rftools.io board calculations for KiCad

A KiCad 10 plugin that reads the open board's stackup and net classes and works out, through the [rftools.io](https://rftools.io) API:

- single-ended impedance: microstrip on outer layers, stripline between planes on inner layers, grounded coplanar waveguide;
- differential impedance of edge-coupled pairs;
- the track width that reaches a target impedance, or the pair gap that reaches a target differential impedance;
- the IPC-2152 track width for a current;
- via impedance, capacitance, inductance and current capacity.

Each number is the one the rftools.io web calculators give for the same inputs. Each is shown with its formula reference, whether its inputs are inside the calculator's valid range, and the engine version that computed it. If you ask, the plugin writes the suggested widths and gaps into your net classes. It shows you every change first, and each write can be undone.

## Requirements

- **KiCad 10.0 or later.** The plugin uses KiCad's IPC API, not the older SWIG plugin interface.
- **The KiCad API switched on.** In KiCad, open Preferences › Plugins and tick **Enable KiCad API**.
- **Python 3.12 or later as KiCad's plugin interpreter.** Set it in Preferences › Plugins › **Python Interpreter**, then restart KiCad. KiCad rebuilds the plugin's environment when the interpreter changes.
  - **macOS:** KiCad bundles Python 3.9, which is too old. Install Python 3.12 or later from [python.org](https://www.python.org/downloads/macos/) or with Homebrew (`brew install python@3.12`). Then select it, for example `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3` or `/opt/homebrew/bin/python3.12`.
  - **Linux:** KiCad uses the system `python3`, which must be 3.12 or later. The dialog also needs your distribution's wxPython package (`python3-wxgtk4.0` on Debian and Ubuntu). Without it the plugin opens a read-only report in your browser instead (see [Without the dialog](#without-the-dialog)).
  - **Windows:** install Python 3.12 or later from [python.org](https://www.python.org/downloads/windows/) and select its `python.exe`.

If the interpreter is too old, the plugin computes nothing. It says which Python it found and how to change it.

KiCad installs the plugin's dependencies into the plugin's own environment from `plugins/requirements.txt`: the rftools.io SDK (`rftools-io`), KiCad's Python library (`kicad-python`), and wxPython on macOS and Windows.

## Install

1. In KiCad, open the **Plugin and Content Manager** and click **Manage repositories**.
2. Add this repository URL:

   ```
   https://antonpogrebenko-public.github.io/rftools-kicad/repository.json
   ```

3. Choose **rftools.io KiCad plugins** in the repository list, find **rftools.io board calculations** under Plugins, click Install, then **Apply Pending Changes**.
4. Open a board in the PCB editor. The action **rftools.io: impedance and widths** is on the toolbar.

Updates come from the same repository. The Plugin and Content Manager offers them when a new version is released.

## A free API key

Every calculation is a call to the rftools.io API, which needs a key.

1. In the plugin's dialog, click **Get a free key**. It opens `https://rftools.io/dashboard/?newKey=1&client=kicad-plugin`. Sign in or create a free account, and a key labelled for the KiCad plugin is created.
2. Paste the key into the dialog and click **Save key**. The plugin first checks the key with a usage request, which costs nothing, and saves it only if rftools.io accepts it.

The key is kept in a settings file that only you can read:

| System | Settings file |
|---|---|
| macOS | `~/Library/Application Support/rftools-kicad/settings.json` |
| Linux | `~/.config/rftools-kicad/settings.json` |
| Windows | `%APPDATA%\rftools-kicad\settings.json` |

The `RFTOOLS_API_KEY` environment variable, if set, is used instead of the saved key. The plugin never shows or logs the whole key. It shows only the key's public ID, its first 12 characters, which is how the rftools.io dashboard lists it.

## Using it

The dialog has three tabs.

1. **Layer model.** Review the structure the plugin proposes for each copper layer: microstrip on the outer layers, stripline between planes on the inner layers, with the heights and εr it read. Where two or more dielectric plies lie between a trace and its plane, it adds their heights and takes their thickness-weighted εr, and says so. KiCad does not say which copper layers are planes, so at first every copper layer is treated as a plane for its neighbours. Untick the signal layers to set the planes yourself. The model is worked out again at once. Nothing is computed until you run.
2. **Net classes.** Tick the classes to compute. For each class you can enter:
   - a single-ended target (Ω);
   - a differential target (Ω);
   - a coplanar gap (mm);
   - a current (A) for IPC-2152;
   - the layer it is routed on, plus any other layers to compute it on;
   - whether to check its via.

   A net class covers the whole board, but an impedance width depends on the layer. The width written to a class is the one for the layer it is routed on. You can also compute outer layers under their solder mask and set the manufacturing grid (0.001 mm by default).
3. **Results.** After you click **Run**, each figure shows:
   - the class's current value;
   - the suggested value (a width or gap on the grid, or the IPC-2152 width);
   - the value the API computed at it;
   - the formula reference;
   - whether the inputs are inside the calculator's range, and the bound crossed if not;
   - the engine version;
   - whether the result came from the cache.

   A target that cannot be reached within the calculator's range is marked unreachable, with the nearest value found.

If rftools.io refuses a call, the dialog says why and what to do. Results computed before the refusal stay on screen.

| Refusal | The dialog shows |
|---|---|
| Monthly allowance spent | when it resets, and where to get more calls |
| Key not accepted | the key link |
| Too many requests | how long to wait |
| No connection | that rftools.io could not be reached, and any cached results |

## What a run costs

Each figure is one API call:

- each impedance at a class's current width;
- each width or gap for a target (one solve call, not a search);
- each IPC-2152 width;
- each via check.

The same figure asked for twice, such as the two outer layers of a symmetric board, is sent once. Results are kept for seven days in a local cache (`~/Library/Caches/rftools-kicad/`, `~/.cache/rftools-kicad/` or `%LOCALAPPDATA%\rftools-kicad\`), so running the same thing again costs nothing. Checking the allowance costs nothing either.

Before each run, the dialog states the most calls the run will use and how many remain this month. The free tier has **5 calls a month**. For example, a four-layer board needs 12 calls for a first run with a 50 Ω class and a 90 Ω pair on two layers each, plus a current and a via check for each. See [rftools.io/pricing](https://rftools.io/pricing) for more calls.

## What is sent to rftools.io

Each request holds a calculator name, such as `microstrip-impedance`, and numbers: widths, heights, εr, copper thickness, a current or a target. It also carries your key and the HTTP client's standard headers.

The plugin never sends the board file, net or net-class names, layer names, the project name or its path.

## Writing widths to net classes

Nothing in the board or the project changes unless you ask:

1. After a run, click **Apply to net classes…**. The preview lists, for each selected class, the current and proposed track width, differential-pair width and differential-pair gap.
2. Only those three fields can change, and only where a value differs. Click **Write to net classes** to confirm, or Cancel to change nothing.
3. Before writing, the plugin records the previous values. It then reads the classes back. If KiCad did not apply the change exactly, the plugin says the write was not applied.

The width proposed for a class is the width for its single-ended target on its routing layer. Without a target, it is the IPC-2152 width for its current, rounded up to the grid, but only if the class is narrower than that. The gap proposed is the gap for its differential target, at the class's current pair width.

**Restore previous values…** writes the recorded values back the same way and records that too. Each Restore undoes one write, newest first. The history is kept per project, in `history.json` beside the settings file.

KiCad applies the change to the open project's settings in memory. It reaches the project file (`.kicad_pro`) when KiCad next saves the project, so save the board before closing KiCad.

## Without the dialog

If wxPython cannot be loaded, the plugin opens a report in your browser instead. This happens, for example, on Linux without `python3-wxgtk4.0`. The report shows:

- the layer model;
- the calculations planned;
- the impedance of every net class's track width on every signal layer, with provenance;
- any refusal.

It spends calls only if the run fits your remaining allowance. Otherwise it shows cached results only. The report has no controls and cannot write to net classes. Install wxPython for the full dialog.

## Troubleshooting

- **"needs Python 3.12 or later"**: set KiCad's plugin interpreter as described under [Requirements](#requirements), then restart KiCad.
- **"Could not connect to KiCad"**: tick Enable KiCad API in Preferences › Plugins, then run the action again from the PCB editor.
- **A library could not be loaded**: right-click the plugin's action in the PCB editor's plugin preferences and choose **Recreate Plugin Environment**.

## Privacy

rftools.io's privacy notice is at [rftools.io/privacy](https://rftools.io/privacy/). The plugin sends no telemetry. The rftools.io dashboard shows your key's usage under its `kicad-plugin` label.

The plugin keeps three files on your computer, and none of them is sent anywhere:

- your settings and key;
- the result cache;
- the net-class write history.

## Development

```sh
python3.12 -m venv .venv
.venv/bin/pip install "kicad-python==0.8.0" rftools-io jsonschema packaging pytest respx ruff
.venv/bin/pip install "wxPython>=4.2.2,<4.3"   # optional: the wx dialog tests
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

To release, set the version in `metadata.json` and `plugins/rftools_kicad/__init__.py`, commit, and push a `v<version>` tag. `.github/workflows/release.yml` then checks the versions and runs CI and the live golden cases. It builds the archive, creates the GitHub release, and publishes the PCM repository to GitHub Pages.

## License

MIT. See [LICENSE](LICENSE).
