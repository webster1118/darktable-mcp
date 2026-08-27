# DarktableMCP

<p align="center">
  <img src="icon.png" alt="DarktableMCP Logo" width="160"/>
</p>

> Maintained by [webster1118](https://github.com/webster1118). Forked from the original DarktableMCP project by [Yadullah Abidi (YaddyVirus)](https://github.com/YaddyVirus).

A Model Context Protocol (MCP) server that lets you edit photos using Claude as your AI photo editor. Works with **Claude Desktop** and **Claude Code**.

Tell Claude what you want — *"make this warmer and more dramatic"*, *"crop to 16:9"*, *"recover the blown highlights"* — and it applies the edits, shows you a preview, and exports the final image. Supports RAW files (CR2, NEF, ARW, etc.) and JPEGs.

## Features

- 🖼️ **RAW file support** — CR2, NEF, ARW, RAF, DNG, ORF, RW2, and more
- 🎨 **Full editing toolkit** — exposure, white balance, contrast, highlights/shadows, saturation, vibrance, clarity, sharpness, noise reduction, vignette
- ✂️ **Crop & rotate** — free crop, aspect ratio crop (16:9, 4:3, 1:1…), straighten
- 📝 **Rename outputs** — give your exports meaningful names
- 📊 **Histogram analysis** — Claude checks clipping and tonal distribution before suggesting edits
- 💾 **Non-destructive** — all edits stored in a sidecar JSON; originals never touched
- 🔄 **Darktable XMP export** — edits written as `.xmp` sidecars Darktable can read
- 📋 **Batch copy settings** — apply one image's edits to many others

---

## Requirements

- Python 3.10 or newer
- [Darktable 5.6](https://www.darktable.org/install/) for RAW/DNG previews and exports
- [Claude Desktop](https://claude.ai/download) (free or Pro)

The server discovers `darktable-cli` automatically, including the standard
per-user Windows installation. If yours is elsewhere, set `DARKTABLE_CLI` to
the full path of `darktable-cli.exe`. Each render uses an isolated temporary
Darktable configuration, so it can run while the Darktable desktop application
is open without competing for its catalog lock.

Apple ProRAW DNGs can be converted before rendering with Adobe DNG Converter.
The server discovers the normal Windows install path automatically; set
`ADOBE_DNG_CONVERTER` if it is installed elsewhere. Conversion defaults to
`DARKTABLE_MCP_DNG_CONVERSION=auto`: likely Apple ProRAW DNGs are converted
up front, and other DNGs are retried through Adobe if Darktable rejects them.
Use `always` or `never` to force or disable this preprocessing.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/webster1118/darktable-mcp.git
cd darktable-mcp
```

### 2. Install the package

```bash
pip install -e .
```

This installs all dependencies automatically:
- `mcp` — Model Context Protocol SDK
- `rawpy` — RAW file decoding
- `Pillow` — image processing
- `numpy` — array operations
- `piexif` — EXIF metadata

### 3. Verify it works

```bash
python -m darktable_mcp
```

You should see it start (it will wait for MCP input — press Ctrl+C to exit). If it starts without errors, you're good.

---

## Connecting to Claude Desktop

### 1. Find your Claude Desktop config file

| Platform | Path |
|----------|------|
| Windows  | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS    | `~/Library/Application Support/Claude/claude_desktop_config.json` |

### 2. Add the server

Open the config file and add the `mcpServers` block. If the file already has content, merge carefully — don't replace the whole file.

**Windows:**
```json
{
  "mcpServers": {
    "darktable": {
      "command": "python",
      "args": ["-m", "darktable_mcp"]
    }
  }
}
```

**macOS / Linux:**
```json
{
  "mcpServers": {
    "darktable": {
      "command": "python3",
      "args": ["-m", "darktable_mcp"]
    }
  }
}
```

> **Tip:** If `python` isn't on your PATH, use the full path to the Python executable.  
> Windows example: `"C:\\Users\\YourName\\AppData\\Local\\Programs\\Python\\Python313\\python.exe"`  
> Find it by running `where python` (Windows) or `which python3` (macOS/Linux) in a terminal.

### 3. Restart Claude Desktop

Fully quit (don't just close the window) and reopen. The **darktable** server should appear when you select Connectors in Claude Chat or Code.

---

## Usage

Once connected, just talk to Claude naturally:

```
My photos are in /Users/me/Pictures/Trip

Make IMG_3500 warmer and more punchy — it looks flat and cold.

Crop it to 16:9 and export as JPEG at maximum quality, name it "jodhpur_desert".
```

Claude will:
1. Call `list_images` to see your folder
2. Call `render_and_analyze` to see the current Darktable render and metrics
3. Call `apply_edit_recipe` or `apply_adjustments` with specific values
4. Render/analyze again and decide whether the edit matched your description
5. Iterate until the image matches the requested look
6. Export the final image at full size when you're happy

### Available tools

| Tool | What it does |
|------|-------------|
| `list_images` | List all images in a directory |
| `get_image_info` | EXIF metadata + current edit state |
| `get_darktable_status` | Show Darktable CLI discovery and supported v2 edit routing |
| `get_image_preview` | Render and preview with edits applied |
| `render_and_analyze` | Render current edits and return a preview plus tone/color metrics |
| `compare_to_reference` | Compare current render metrics to a reference JPEG |
| `apply_adjustments` | Exposure, WB, tone, colour, detail, dehaze, effects |
| `apply_edit_recipe` | Apply a generic dict of adjustments, crop, output name, and mask clearing |
| `convert_dng_if_needed` | Explicitly run Adobe DNG Converter for ProRAW/diagnostics |
| `crop_image` | Crop by coordinates or aspect ratio |
| `rotate_image` | Rotate / straighten |
| `reset_crop` | Remove crop, restore full frame |
| `add_gradient_mask` | Add or replace a reusable local gradient adjustment |
| `reset_masks` | Remove all local masks |
| `rename_output` | Set the export filename |
| `export_image` | Export to JPEG / PNG / TIFF |
| `reset_edits` | Undo everything, back to original |
| `get_histogram` | Tonal/clipping analysis |
| `copy_settings` | Copy edits from one image to another |

### Example prompts

- *"What would you suggest to improve this shot?"*
- *"Make it look like a moody film photo"*
- *"Recover the blown sky and open up the shadows"*
- *"Straighten the horizon by about 1.5 degrees"*
- *"Export all photos in this folder with the same settings"*
- *"Reset everything and start fresh"*

For subjective edits, describe the desired look and ask Claude to iterate using
the bridge tools instead of calling a hardcoded preset. For example:

```text
Edit this iPhone RAW so it feels like bright sunny daylight as I remember the
scene: professional, natural, clear sky and water, detailed mountains, not HDR
and not oversaturated. Use render_and_analyze after each edit pass. Prefer
native Darktable/XMP adjustments. Do not use local masks unless I ask for them.
Export full size when the preview is good.
```

---

## How edits are stored

Each image gets a companion `.mcp.json` sidecar file (e.g. `IMG_3500.mcp.json`) that stores all adjustments non-destructively. Your original RAW files are never modified.

On RAW/DNG export, Darktable handles the RAW render through `darktable-cli`.
The MCP writes a Darktable-compatible `.xmp` sidecar for verified Darktable 5.6
module payloads including exposure, white balance/temperature, basic tone,
color balance RGB, sigmoid, haze removal, simple crop, sharpen, and vignette.
Shadows/whites/blacks use tone equalizer, denoise uses profiled denoise, and
clarity uses local contrast. Local masks and rotated crops still use the MCP
finishing pass until their native Darktable payloads are verified.

---

## Troubleshooting

**Server doesn't appear in Claude Desktop**  
Make sure you fully quit and restarted Claude Desktop after editing the config. Check that the JSON is valid (no trailing commas, matching braces).

**`python` command not found**  
Use the full path to your Python executable in the config. Run `where python` (Windows) or `which python3` (macOS) to find it.

**RAW file fails to open**  
Some camera models need an updated version of `rawpy`. Run `pip install --upgrade rawpy`.

**Preview file not opening**  
The preview is always saved as `originalname__preview.jpg` next to your source file. Open it manually if your system doesn't pop it up automatically.

---

## Maintainer

**webster1118** — [@webster1118](https://github.com/webster1118)

Original project by **Yadullah Abidi** — [@YaddyVirus](https://github.com/YaddyVirus)

## License

MIT
