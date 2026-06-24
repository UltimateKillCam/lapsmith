# LapSmith

A free, telemetry-driven tuning assistant for Forza Horizon 6. It watches the game's own Data Out feed and your tyre Heat page, then tells you the exact change to make next — one tweak per lap — until the car stops getting faster. You drive, you type the numbers into the in-game tune menu, and LapSmith works out where to go from there. It never touches the game itself.

## What it actually does

LapSmith reads two things: the telemetry Forza already broadcasts when you turn on Data Out, and the tyre temperatures on the in-game Heat page (read locally on your PC, no internet needed). From those it suggests a single setting change. You apply it, drive a couple of laps, and it keeps the change if you got faster or backs it out if you didn't. Repeat until it settles on a tune.

That's the whole loop. It's deliberately one change at a time so you can see what each adjustment does — the same way you'd tune by hand, just without the guesswork about which direction to go.

## What it does *not* do

This matters, so it's up front: LapSmith only **reads**. It doesn't inject code, touch game memory, automate your inputs, or change any game files. Every tune value is something you type in yourself in the normal tune menu. It listens to the official Data Out UDP feed and looks at your screen — that's all. If you're wondering whether it's the sort of thing that gets people banned: no. It's a read-only assistant, not a trainer or a bot.

## Install

**[⬇ Download the latest version](https://github.com/UltimateKillCam/lapsmith/releases/latest)**

Two options, no Python or command line required:

- **Installer** — download `LapSmith-Setup.exe`, run it, done. Adds Start-menu and optional desktop shortcuts plus an uninstaller.
- **Portable** — download `LapSmith-<version>-portable.zip`, unzip it anywhere, run `LapSmith.exe`.

The build is unsigned (signing certificates cost money I'm not spending on a free tool yet), so the first time you run it Windows SmartScreen will say "Windows protected your PC." Click **More info → Run anyway**. If you grabbed the zip, you might need to right-click it → Properties → **Unblock** before extracting.

It comes back clean on VirusTotal — 0 of 68 engines flag it: [scan result](https://www.virustotal.com/gui/file/10b811875070296cf1f5c7f4666cc2e20818fe55b85857acbfa67511436753e8/detection). PyInstaller apps occasionally trip an antivirus heuristic anyway; if yours grumbles, it's a false positive and you can allow it.

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
5. **Test** — first lap warm-up, then a few clean laps measured. A change is kept only if the car's *telemetry* shows it got better (see below), not just because a lap was faster.
6. **Converged** — when nothing more helps (or the time budget runs out), LapSmith re-measures your original baseline once more for an honest verdict, then saves your final tune and the shareable files.

The hotkeys, settings, and full walkthrough live on the **Help** tab inside the app.

## How lap times are validated (tune gains vs. driver improvement)

Over a session you learn the track and get faster on your own — so a single lap time is a weak, driver-confounded signal: a change can look like a win when it was really just you driving better. LapSmith separates the two:

- **Telemetry-primary fitness.** A change is judged mainly on the car's telemetry — cornering grip, corner-exit forward-g (how quickly it accelerates off a corner), traction efficiency, and corner speed — with lap time as a secondary guardrail (if the telemetry says "better" but the clock is clearly and repeatably worse, the change is rejected).
- **Track-position binning.** Rivals is the same track every lap, so telemetry is binned by position on track (DistanceTraveled): the *same corner* is compared lap to lap, which cancels driving-line variation.
- **Multi-lap measurements.** Each measurement aggregates several clean green laps, not one.
- **A/B/A confirmation** (Test rigour = *Confirmed*, the default): when a change looks faster, LapSmith has you revert to the previous tune and re-measure it. If the reverted baseline is now just as quick, the "gain" was you improving — so the change is discarded. *Quick* rigour does a single pass and warns about drift instead.
- **Honest final check.** On stop it re-measures your original baseline. If that's now as fast as the "optimised" tune, it reports *"net improvement within driver variation — changes not confirmed"* instead of claiming a tune win.

## Tuning time budget

There's a **Max tuning time** setting (default **20 minutes**; set **0** for unlimited), in both the setup form and **Settings → Max tuning time (minutes)** in the main window — they share one persisted value, and the main-window control applies live even mid-run. It's *real wall-clock* time that starts on your **first Rivals lap** and runs continuously — including loading screens, menu time, and entering tune changes — and is never paused.

It's a **ceiling, not a target**: if the tool converges first (every lever improved-or-locked, nothing left to try) it stops and saves immediately, with time to spare — it never idles or re-tests just to fill the clock. On expiry it finishes the test already in progress (never a half-tested change), runs the honest final check, then stops. The overlay shows the time remaining; the saved status reads `converged` or `stopped: time budget` accordingly.

## Console / Xbox (telemetry over the LAN)

LapSmith runs on a **Windows PC**, not on the console. If you play Forza on an Xbox/console, it can stream its Data Out telemetry across your home network to the PC running LapSmith — the tuning works exactly the same, with one difference (below).

Turn on **Console mode** (in the setup form, or **Settings → Console mode**). Then on the console, in Forza's **HUD & Gameplay → Data Out / telemetry** settings:

- **Data Out: ON**
- **IP address:** your PC's LAN IP — LapSmith shows it when Console mode is on (e.g. `192.168.1.42`)
- **Port:** the same port as LapSmith (default `5607`)
- **Format: Dash** (the "car dash" / Sled+Dash layout — that's the one with tyre temps)

Both devices must be on the same network, and Windows Firewall must allow LapSmith to receive UDP on that port (accept the prompt on first run). In Console mode LapSmith listens on all interfaces so it can receive the console's packets; on PC it stays on loopback.

**The one caveat — camber/toe accuracy.** On PC, LapSmith reads the in-game tyre **Heat** screen to get three temperatures per tyre (inner/middle/outer), which is what pins down camber and toe. There's no way to screenshot that screen on a console, so in Console mode tyre temps fall back to the **single** per-corner temperature in the UDP packet. That's still enough for pressure (left/right balance) and everything else, but **camber and toe are less accurate** and get tuned by lap time instead. LapSmith shows a clear notice while Console mode is on.

## Progress, why-reasons, and rejecting changes

- **Progress** — the overlay always shows *"Confirmed gains: N · Best so far: T (±Δ vs start)"* and a plain trend (**Improving** / **Fine-tuning** / **Not finding much — may finish soon**), so you can always tell if it's getting anywhere. If it stalls it says so instead of grinding silently.
- **Why** — every proposed change shows a one-line reason tied to the real telemetry that triggered it (e.g. *"On-power oversteer: rear slip 0.45 under throttle"*).
- **Fewer re-test laps** — LapSmith reads your inputs (throttle/brake/steering, binned by track position) to tell a tune gain from you just driving better. If a faster lap came with notably different inputs, it credits *you* and moves on **without** a full A/B/A re-drive; it only re-tests when the inputs look the same but the result moved. (Inputs don't fully isolate driver from tune, so A/B/A stays the tiebreaker — just used far less.)
- **Reject** — don't want a suggestion? Press **[F10]**. It isn't applied, that lever is **locked for the rest of the session** (never suggested again), the loop continues and can still converge, and the rejection is logged.

## Reading the overlay

Overlay states come in two unmistakable colours. **Amber "CHANGE THESE NOW"** means edit the tune menu — it lists the exact fields as *from → to* (only what changed, including any revert) and ends with "press F8 when applied". **Green** means just drive, hands off the menu — `WARM-UP`, `OUT-LAP` (neither counted), `MEASURING — lap x/y` (a counted lap), `RE-ANCHOR`, or `FINAL CHECK`.

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
