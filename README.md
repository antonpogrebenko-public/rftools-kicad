# rftools.io board calculations for KiCad

A KiCad 10 plugin that reads the open board's stackup and net classes and works out, through the [rftools.io](https://rftools.io) API:

- single-ended impedance: microstrip on outer layers, stripline between planes on inner layers, grounded coplanar waveguide;
- differential impedance of edge-coupled pairs;
- the track width that reaches a target impedance, or the pair gap that reaches a target differential impedance;
- the IPC-2152 track width for a current;
- via impedance, capacitance, inductance and current capacity.

Each number is the one the rftools.io web calculators give for the same inputs. Each is shown with its formula reference, whether its inputs are inside the calculator's valid range, and the engine version that computed it. If you ask, the plugin writes the suggested widths and gaps into your net classes. It shows you every change first, and each write can be undone. On KiCad 10.0.6 and earlier it lists the values for you to enter by hand instead (see [Writing widths to net classes](#writing-widths-to-net-classes)).

## Requirements

- **KiCad 10.0 or later.** The plugin uses KiCad's IPC API, not the older SWIG plugin interface.
- **The KiCad API switched on.** In KiCad, open Preferences › Plugins and tick **Enable KiCad API**.
- **KiCad's own Python.** There is nothing to install or set up: the plugin runs on the Python KiCad uses for plugins by default, which is its bundled Python on macOS (3.9) and Windows (3.11) and the system `python3` on Linux. It needs Python 3.9 or later.
  - **Linux:** the system `python3` also needs your distribution's `python3-venv` and `python3-wxgtk4.0` packages (Debian and Ubuntu names):
    - Without `python3-venv`, KiCad builds the plugin's environment with no pip, so the plugin's libraries never install and the plugin never loads. Install `python3-venv`, delete the plugin's environment folder (see below), and restart KiCad.
    - Without `python3-wxgtk4.0`, the plugin opens a read-only report in your browser instead of its dialog (see [Without the dialog](#without-the-dialog)).
  - **If you changed Preferences › Plugins › Python Interpreter,** set it back to KiCad's own Python: click **Detect Automatically** beside it, which picks KiCad's bundled Python on macOS and Windows and the system `python3` on Linux. On macOS no other Python can run KiCad plugins, because KiCad passes its own Python settings (`PYTHONHOME` and `PYTHONPATH`) to the interpreter it starts, so a different one stops at start-up and the plugin's environment stays empty.
- **After changing the interpreter, rebuild the plugin's environment.** KiCad 10.0.6 does not rebuild it on its own. Right-click the plugin in Preferences › Plugins › Action Plugins and choose **Recreate Plugin Environment**, or delete the environment folder, then restart KiCad. The folder is:

  | System | Plugin environment |
  |---|---|
  | macOS | `~/Library/Caches/KiCad/10.0/python-environments/io.rftools.kicad` |
  | Linux | `~/.cache/kicad/10.0/python-environments/io.rftools.kicad` |
  | Windows | `%LOCALAPPDATA%\KiCad\10.0\python-environments\io.rftools.kicad` |

If the interpreter is older than 3.9, the plugin computes nothing. It says which Python it found and how to set KiCad's own back.

KiCad installs the plugin's two dependencies into the plugin's own environment from `plugins/requirements.txt`: KiCad's Python library (`kicad-python`) and `certifi`, the certificate bundle the plugin checks rftools.io's HTTPS certificate against (KiCad's bundled Python on macOS has no certificates of its own). The plugin calls the rftools.io API with Python's standard library. wxPython, for the dialog, comes with KiCad on macOS and Windows and from `python3-wxgtk4.0` on Linux.

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
| Certificate not verified | that rftools.io's HTTPS certificate could not be checked, why, and how to reinstall the plugin's certificates |

## What a run costs

Each figure is one API call:

- each impedance at a class's current width;
- each width or gap for a target (one solve call, not a search);
- each IPC-2152 width;
- each via check.

The same figure asked for twice, such as the two outer layers of a symmetric board, is sent once. Results are kept for seven days in a local cache (`~/Library/Caches/rftools-kicad/`, `~/.cache/rftools-kicad/` or `%LOCALAPPDATA%\rftools-kicad\`), so running the same thing again costs nothing. Checking the allowance costs nothing either.

Before each run, the dialog states the most calls the run will use and how many remain this month. The free tier has **5 calls a month**. For example, a four-layer board needs 12 calls for a first run with a 50 Ω class and a 90 Ω pair on two layers each, plus a current and a via check for each. See [rftools.io/pricing](https://rftools.io/pricing) for more calls.

## What is sent to rftools.io

Each request holds a calculator name, such as `microstrip-impedance`, and numbers: widths, heights, εr, copper thickness, a current or a target. It also carries your key (in the `X-API-Key` header, over HTTPS only) and `User-Agent: rftools-kicad/<version>`.

The plugin never sends the board file, net or net-class names, layer names, the project name or its path.

## Writing widths to net classes

Nothing in the board or the project changes unless you ask:

1. After a run, click **Apply to net classes…**. The preview lists, for each selected class, the current and proposed track width, differential-pair width and differential-pair gap.
2. Only those three fields can change, and only where a value differs. Click **Write to net classes** to confirm, or Cancel to change nothing.
3. Before writing, the plugin records the previous values and counts the nets in each class it will write. It then reads the classes and the nets back:
   - If KiCad applied nothing, or put other values in the three fields, the plugin says the write was not applied.
   - If a written class no longer lists itself as its only constituent, holds a different number of nets, or changed in any other setting, or if another class changed, the plugin says: **"The write did not apply correctly. Do not save the board; close it without saving and reopen it."**

The width proposed for a class is the width for its single-ended target on its routing layer. Without a target, it is the IPC-2152 width for its current, rounded up to the grid, but only if the class is narrower than that. The gap proposed is the gap for its differential target, at the class's current pair width.

**Restore previous values…** writes the recorded values back the same way and records that too. Each Restore undoes one write, newest first. The history is kept per project, in `history.json` beside the settings file.

KiCad applies the change to the open project's settings in memory. It reaches the project file (`.kicad_pro`) when KiCad next saves the project, so save the board before closing KiCad.

### KiCad 10.0.6 and earlier: enter the values by hand

KiCad 10.0.6 and earlier corrupt a net class written through the API. The class loses its nets: none of its nets belong to it any more. Writing the old values back does not repair it, and the next save hangs KiCad on Linux or crashes it on Windows. KiCad fixed this on 22 September 2026 (commit `d622c37a`, "API: Fix handling of netclasses"), but no KiCad release had the fix yet as of 25 September 2026. The first one will be a later 10.0.x or 11.x release.

So the plugin writes net classes only on a KiCad later than 10.0.6. On 10.0.6 and earlier, or when it cannot read KiCad's version, it writes nothing:

1. The buttons read **Values for Board Setup…** and **Previous values…**, and the dialog says why.
2. **Values for Board Setup…** shows the same preview as a write: every selected class's track width, DP width and DP gap, current and proposed, in mm, under Board Setup's column names.
3. **Copy values** puts that table on the clipboard as tab-separated text. Keep the window open, or paste the table somewhere, while you enter the values in Board Setup › Net Classes.
4. **Previous values…** shows the values recorded before the plugin's latest write, if there is one, for entering the same way. The history is still read, but nothing is written back.

Writing from the plugin turns on by itself once you run it on a KiCad release with the fix.

## Without the dialog

If wxPython cannot be loaded, the plugin opens a report in your browser instead. This happens on Linux without `python3-wxgtk4.0`, and on Linux with any interpreter other than the system `python3` (the distribution's wxPython is built for the system Python only, and PyPI has no Linux wxPython wheel). The report shows:

- the layer model;
- the calculations planned;
- the impedance of every net class's track width on every signal layer, with provenance;
- any refusal.

It spends calls only if the run fits your remaining allowance. Otherwise it shows cached results only. The report has no controls and cannot write to net classes. On Linux, install `python3-wxgtk4.0` for the full dialog.

## Troubleshooting

- **"needs Python 3.9 or later"**: KiCad's plugin interpreter was changed to an older Python. Set it back to KiCad's own as described under [Requirements](#requirements), rebuild the plugin's environment and restart KiCad.
- **On macOS, the plugin never appears after you set another Python as the interpreter**: that Python cannot start under KiCad (see [Requirements](#requirements)). Click **Detect Automatically** in Preferences › Plugins, choose **Recreate Plugin Environment**, and restart KiCad.
- **The plugin still runs on the old Python after you changed the interpreter**: KiCad 10.0.6 keeps the old environment. Right-click the plugin in Preferences › Plugins › Action Plugins and choose **Recreate Plugin Environment**, or delete the environment folder listed under [Requirements](#requirements). Then restart KiCad.
- **"The TLS certificate of rftools.io could not be verified"**: the plugin could not check rftools.io's HTTPS certificate, so it sent nothing. Choose **Recreate Plugin Environment** to reinstall its certificates (`certifi`). A network that inspects HTTPS, as some company networks do, presents its own certificate, which the plugin trusts only if Python does: on Windows and Linux, install the network's root certificate in the system; on macOS, KiCad's Python does not read the Keychain, so start KiCad with `SSL_CERT_FILE` naming a file that holds it.
- **On Linux, the plugin never appears or never starts**: check that `python3-venv` is installed. Then delete the environment folder and restart KiCad.
- **"Could not connect to KiCad"**: tick Enable KiCad API in Preferences › Plugins, then run the action again from the PCB editor.
- **On Windows without a GPU, the plugin waits or cannot connect** (a remote desktop session or a virtual machine): KiCad may be showing a modal "Could not use OpenGL" notice, possibly behind another window. KiCad's API does not answer until you close that notice.
- **A library could not be loaded**: right-click the plugin in Preferences › Plugins › Action Plugins and choose **Recreate Plugin Environment**.
- **"The write did not apply correctly. Do not save the board"**: close the board without saving, reopen it, and check Board Setup › Net Classes. The write is recorded in `history.json` as `damaged`.

## Privacy

rftools.io's privacy notice is at [rftools.io/privacy](https://rftools.io/privacy/). The plugin sends no telemetry. The rftools.io dashboard shows your key's usage under its `kicad-plugin` label.

The plugin keeps three files on your computer, and none of them is sent anywhere:

- your settings and key;
- the result cache;
- the net-class write history.

## Development

```sh
python3 -m venv .venv          # any Python from 3.9; CI tests 3.9, 3.11, 3.12 and 3.13
.venv/bin/pip install "kicad-python==0.8.0" certifi jsonschema packaging pytest ruff
.venv/bin/pip install "wxPython>=4.2.2,<4.3"   # optional: the wx dialog tests
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

To test on the interpreter macOS users have, make a scratch environment from KiCad's bundled Python the way KiCad does, with its wx: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3 -m venv --system-site-packages /tmp/kicad39`, then install the same packages into it and run the tests with `PYTHONNOUSERSITE=1`. Never install into the bundled interpreter itself. The API client's tests run against a local HTTP server, so the suite makes no call to rftools.io.

To release, set the version in `metadata.json` and `plugins/rftools_kicad/__init__.py`, commit, and push a `v<version>` tag. `.github/workflows/release.yml` then checks the versions and runs CI and the live golden cases. It builds the archive, creates the GitHub release, and publishes the PCM repository to GitHub Pages.

## License

MIT. See [LICENSE](LICENSE).
