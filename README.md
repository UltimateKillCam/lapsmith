# LapSmith

A free, telemetry-driven tuning assistant for Forza Horizon 6. It watches the game's own Data Out feed and your tyre Heat page, then tells you the exact change to make next — one tweak per lap — until the car stops getting faster. You drive, you type the numbers into the in-game tune menu, and LapSmith works out where to go from there. It never touches the game itself.

## What it actually does

LapSmith reads two things: the telemetry Forza already broadcasts when you turn on Data Out, and the tyre temperatures on the in-game Heat page (read locally on your PC, no internet needed). From those it suggests a single setting change. You apply it, drive a couple of laps, and it keeps the change if you got faster or backs it out if you didn't. Repeat until it settles on a tune.

That's the whole loop. It's deliberately one change at a time so you can see what each adjustment does — the same way you'd tune by hand, just without the guesswork about which direction to go.

## What it does *not* do

This matters, so it's up front: LapSmith only **reads**. It doesn't inject code, touch game memory, automate your inputs, or change any game files. Every tune value is something you type in yourself in the normal tune menu. It listens to the official Data Out UDP feed and looks at your screen — that's all. If you're wondering whether it's the sort of thing that gets people banned: no. It's a read-only assistant, not a trainer or a bot.

## Install

Two options, no Python or command line required:

- **Installer** — download `LapSmith-Setup.exe`, run it, done. Adds Start-menu and optional desktop shortcuts plus an uninstaller.
- **Portable** — download `LapSmith-<version>-portable.zip`, unzip it anywhere, run `LapSmith.exe`.

The build is unsigned (signing certificates cost money I'm not spending on a free tool yet), so the first time you run it Windows SmartScreen will say "Windows protected your PC." Click **More info → Run anyway**. If you grabbed the zip, you might need to right-click it → Properties → **Unblock** before extracting.

It comes back clean on VirusTotal — 0 of 68 engines flag it: [scan result](https://www.virustotal.com/gui/file/d20f7935b72274e67a17c78e981c4cf2d657c24d6dfe2ba8cc45d2e24f04afd7/detection). PyInstaller apps occasionally trip an antivirus heuristic anyway; if yours grumbles, it's a false positive and you can allow it.

## Quick start

1. In Forza Horizon 6, turn on **Settings → HUD and Gameplay → Data Out**. Set the IP to `127.0.0.1` and the port to `5607`.
2. Run the game in borderless or fullscreen.
3. Open LapSmith and hit **Start Tuning**.
4. Confirm the car it detects, pick your discipline, and drive.

Note that Forza only streams telemetry while you're actually on track — nothing comes through in the garage or menus, so don't expect a signal until you're driving.

## A typical session

1. **Select car** — confirm the detected car (name it if LapSmith hasn't seen it before) and pick how wide a range to search.
2. **Apply the tune** — type the shown values into the tune menu, then load a Rivals event.
3. **Baseline** — drive 2 laps. The first is a warm-up and gets ignored; the second sets your baseline.
4. **Make the change** — apply the one change it shows, restart the event, drive 2 laps.
5. **Test** — first lap warm-up, second lap timed against your best. Faster sticks, slower gets reverted.
6. **Converged** — when nothing more helps, your final tune and the shareable files are saved.

The hotkeys, settings, and full walkthrough live on the **Help** tab inside the app.

## Car names

Telemetry gives a numeric car ID, not a name. LapSmith asks you to name a car the first time it sees one and remembers it from then on. To fill in names in bulk, import the community list under **Settings → Import car names** — the "Forza Horizon 6 Car ID List" by **xEDWARDSZz** on [Nexus Mods](https://www.nexusmods.com/forzahorizon6/mods/309). Download it there and import it; LapSmith merges it in and never overwrites a name you set yourself.

## Outputs

When a tune's done, LapSmith writes a value sheet, a JSON file, and an OPTN.club-format block. These are values for you to type into the game — not an in-game share code.

## Troubleshooting

- **"No telemetry"** — the Data Out port in the game has to match LapSmith's (default `5607`), the IP set to `127.0.0.1`, and you need to be driving. If you just quit and relaunched, give it a second to release the port.
- **SmartScreen or antivirus warning** — expected for an unsigned build; see Install above.
- **Tyre temps not reading** — LapSmith falls back to tuning camber by lap time, so it keeps working. Make sure the game is borderless or fullscreen so the Heat page is on screen to read.

## Support

LapSmith is free and always will be — nothing is locked behind a payment. If it's saved you a few laps and you feel like buying me a coffee, there's a tip jar here: **[ko-fi.com/ultimatekillcam](https://ko-fi.com/ultimatekillcam)**. Completely optional.

## License and credits

Released under the MIT License — see `LICENSE`. Bundled components and their licences are listed in `THIRD-PARTY-NOTICES.txt` (RapidOCR / PP-OCR, ONNX Runtime, PySide6 / Qt, Python).

Car ID list by xEDWARDSZz on Nexus Mods. Offline OCR by RapidOCR running on ONNX Runtime.

## Not affiliated

LapSmith is an independent, fan-made tool. It isn't affiliated with, endorsed by, or associated with Microsoft, Playground Games, or Turn 10 Studios. "Forza Horizon" is used only to describe what the tool works with.
