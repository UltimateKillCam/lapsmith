"""The MANAGEMENT window: a normal, focusable QMainWindow used between sessions.

This is the second of the two UI surfaces (the first is the non-activating live
overlay). Because it's only used when NOT driving, it can take focus and use real
tabs + mouse buttons:

  Dashboard/Stats | Previous Tunes | Logs | Settings | Help

A prominent START TUNING button begins a session (car detect/name + discipline +
bounds setup happen HERE, focus is fine), then the app hides this window and shows
the overlay for the drive. It is a presentation layer over the SAME Controller -
it reads the controller's data providers and calls its edit methods; it never
touches the tuning logic or file outputs.

PySide6 is imported lazily so the package imports without it.
"""
from __future__ import annotations

import os
import shutil
from typing import Callable, Dict, Optional

from .. import PRODUCT_NAME


# Rendered by the Help tab's QTextBrowser (themed by the app stylesheet + the
# browser's default style sheet). Plain HTML so links open in the system browser.
_HELP_HTML = """\
<h2>LapSmith — quick guide</h2>
<p>LapSmith reads the game's telemetry and your tyre <b>Heat</b> page, then tells you the exact values to
enter. You drive and type the values in yourself — it never touches the game.</p>
<h3>How a session goes</h3>
<ol>
<li><b>Select car</b> — confirm the detected car (name it if it's new), then pick the discipline and how
wide the slider search ranges should be.</li>
<li><b>Apply the tune</b> — type the shown values into the in-game tune menu, then load a Rivals event.</li>
<li><b>Baseline</b> — drive 2 laps. Lap 1 is a warm-up and is ignored; lap 2 sets your baseline.</li>
<li><b>Make the change</b> — apply the one change LapSmith shows, <b>restart</b> the event, drive 2 laps.</li>
<li><b>Test</b> — lap 1 warm-up (ignored), the next laps measured against your best. A change is kept only
if the <b>telemetry</b> shows the car really got better (see below), not just because the lap was faster.</li>
<li><b>Converged</b> — when nothing more helps (or the time budget runs out), LapSmith re-measures your
ORIGINAL baseline once more for an honest verdict, then saves the final tune and shareable files.</li>
</ol>
<h3>How lap times are validated (separating tune gains from driver improvement)</h3>
<p>Over a session you naturally learn the track and get faster — so a single lap time is a weak, misleading
signal: a change can look like a win when it was really just you driving better. LapSmith guards against this:</p>
<ul>
<li><b>Telemetry-primary fitness.</b> A change is judged mainly on the car's telemetry — cornering grip,
corner-exit forward-g (how quickly it accelerates off a corner), traction efficiency and corner speed — not
the raw clock. Lap time is a secondary guardrail.</li>
<li><b>Track-position binning.</b> Because Rivals is the same track every lap, telemetry is binned by position
on track, so the <i>same corner</i> is compared across laps. That cancels out driving-line variation.</li>
<li><b>Multi-lap measurements.</b> Each measurement aggregates a few clean green laps, not one, to beat noise.</li>
<li><b>A/B/A confirmation</b> (Test rigour = Confirmed). When a change looks faster, LapSmith has you revert
to the previous tune and re-measure it. If your reverted baseline is now just as quick, the “gain” was you
improving — so the change is discarded, not banked.</li>
<li><b>Honest final check.</b> On stop it re-measures your original baseline. If that’s now as fast as the
“optimised” tune, it reports <i>“net improvement within driver variation — changes not confirmed”</i> rather
than claiming a tune win.</li>
</ul>
<h3>Test rigour &amp; the time budget</h3>
<ul>
<li><b>Test rigour</b> (setup) — <b>Confirmed</b> (default) runs the full A/B/A confirmation on apparent wins;
<b>Quick</b> is a single pass per change (faster, less rigorous) but still re-anchors and runs the honest
final check, warning you about possible driver drift instead of confirming it.</li>
<li><b>Max tuning time</b> (default <b>20 min</b>) — a real wall-clock <i>ceiling</i>, started on your FIRST
Rivals lap and counting continuously, <i>including loading screens, menus and entering tune changes</i> — it
is never paused. It is a ceiling, not a target: if the tool converges first it stops and saves immediately,
with time to spare. On expiry it finishes the test in progress (never a half-tested change), runs the honest
final check, then stops. Set it to <b>0</b> for Unlimited / off. Editable in <b>Settings → Max tuning time
(minutes)</b> (applies live, even mid-run) and in the setup form — they share one value. The overlay shows
the time remaining; the saved status reads <i>"converged"</i> or <i>"stopped: time budget"</i> accordingly.</li>
</ul>
<h3>Knowing if it's getting anywhere (progress)</h3>
<p>The overlay always shows a progress line — <i>"Confirmed gains: 3 · Best so far: 51.86 (-0.9s vs start)"</i>
— plus a plain-language trend: <b>Improving</b>, <b>Fine-tuning</b>, or <b>Not finding much — may finish
soon</b>. If it stops making progress it says so honestly rather than grinding in silence, and (with a time
budget) finishes. It also shows how many re-test laps it saved using your inputs (below).</p>
<h3>Why each change — and using your inputs to cut re-tests</h3>
<p>Every proposed change shows a one-line <b>why</b> tied to the actual telemetry that triggered it (e.g.
<i>"On-power oversteer: rear slip 0.45 under throttle"</i>) — never a generic template.</p>
<p>To tell a real tune gain from you simply driving better, LapSmith now reads your <b>inputs</b> (throttle,
brake, steering) binned by track position. When a lap looks faster but your inputs changed a lot, it credits
<i>you</i>, not the tune, and moves on — <b>without</b> a full re-test. It only falls back to the slower
A/B/A re-drive when your inputs look the <i>same</i> but the result moved (the genuinely ambiguous case), so
there's far less repetition. (Inputs don't fully isolate driver from tune — a better line can be faster on
the same inputs — so A/B/A stays the tiebreaker, just used much less often.)</p>
<h3>Rejecting a change you don't want</h3>
<p>If you don't want a suggested change, press <b>[F10]</b> (or it's offered on the overlay). It won't be
applied, and that lever is <b>locked for the rest of the session</b> — it'll never be suggested again, and
the loop moves on to other changes and can still converge. Every rejection is written to the session log.</p>
<h3>Reading the overlay — two kinds of state</h3>
<p>Every overlay state is one of two clearly different colours so a timed lap can never be mistaken for a
go-to-the-menu prompt:</p>
<ul>
<li><b>Amber — CHANGE THESE NOW.</b> Edit the in-game tune menu. It lists the exact fields and target values
as <i>from → to</i> (only what must change, including setting a reverted change back), and ends with
<b>"press F8 when applied"</b>. Used for applying a change, reverting, and re-entering the baseline.</li>
<li><b>Green — just drive, don't touch anything.</b> No checklist. Sub-states: <b>WARM-UP</b> (not counted),
<b>OUT-LAP</b> (not counted, get back to the line), <b>MEASURING — lap x/y</b> (a counted timed lap),
<b>RE-ANCHOR</b> (drive the current tune, no changes), and <b>FINAL CHECK</b> (drive the baseline). If
everything was already reverted it says <i>"Car is already on the baseline — just drive"</i>.</li>
<li><b>DONE</b> — <i>converged</i> (tune saved) or <i>TIME BUDGET REACHED</i>.</li>
</ul>
<h3>Hotkeys</h3>
<table>
<tr><td><b>F8</b></td><td>advance / confirm / apply</td></tr>
<tr><td><b>F11</b></td><td>end manual test</td></tr>
<tr><td><b>F10</b></td><td>reject the shown change (auto-lap) · mark segment END (manual free-roam)</td></tr>
<tr><td><b>F9</b></td><td>manual segment start (free-roam, no lap timer)</td></tr>
<tr><td><b>F6</b></td><td>simple / advanced overlay view</td></tr>
<tr><td><b>F7</b></td><td>switch tab</td></tr>
<tr><td><b>Ctrl+F12</b></td><td>quit</td></tr>
</table>
<h3>Tyre temps</h3>
<p>Read locally on your PC, fully offline. If a lap's Heat page can't be read, camber is tuned by lap time
instead, so it never blocks.</p>
<h3>Console / Xbox (telemetry over the LAN)</h3>
<p>LapSmith runs on a <b>Windows PC</b>, not on the console. If you play Forza on an Xbox/console, it can
stream its Data Out telemetry across your home network to the PC running LapSmith — the tuning is the same,
with one caveat below. Turn on <b>Console mode</b> (setup form, or <b>Settings → Console mode</b>), then on
the console set Forza's <b>Data Out</b> to:</p>
<ul>
<li><b>IP address</b> — your PC's LAN IP (LapSmith shows it when Console mode is on)</li>
<li><b>Port</b> — the same port as LapSmith (default 5607)</li>
<li><b>Format: Dash</b> (the layout that includes tyre temps)</li>
</ul>
<p>Both devices must be on the same network, and Windows Firewall must allow LapSmith to receive UDP on that
port (accept the prompt). In Console mode LapSmith listens on all interfaces; on PC it stays on loopback.</p>
<p><b>The one caveat — camber/toe accuracy.</b> There's no in-game Heat screen to screenshot on a console, so
tyre temps fall back to the <b>single</b> per-corner temperature in the UDP packet. That's fine for pressure
and everything else, but <b>camber and toe are less accurate</b> and tuned by lap time instead. A clear
notice shows on the overlay while Console mode is on.</p>
<h3>Car names</h3>
<p>Telemetry only gives a numeric car ID. LapSmith asks you to name a car the first time it sees one, and you
can bulk-import the community ID→name list under <b>Settings → Import car names</b> (from the Nexus "Forza
Horizon 6 Car ID List" by xEDWARDSZz). Your own names always win.</p>
<h3>Settings worth knowing</h3>
<ul>
<li><b>Telemetry port</b> — must match the game's Data Out port; applies on next start.</li>
<li><b>Drivetrain</b> (setup) — leave on Auto-detect unless it's wrong; forcing FWD/RWD/AWD makes the
differential suggestions match the car (FWD = front diff only, never rear/centre).</li>
<li><b>Tyre-temp reader</b> — leave on auto.</li>
<li><b>Cloud reader</b> — off by default; only used if you opt in and set a key.</li>
<li><b>Overlay default view</b> — simple or advanced.</li>
</ul>
<h3>Outputs</h3>
<p>The value sheet, JSON, and optn.club block are values to <b>type in</b> — not an in-game share code.</p>
"""


def build_main_window(ctrl, hooks: Dict[str, Callable]):
    """Create (don't show) the management window. `hooks` supplies app-level
    actions: start_tuning(), support_bundle()->path|None, quit(). Returns the
    window (a QMainWindow). Raises RuntimeError with guidance if PySide6 missing."""
    try:
        from PySide6 import QtWidgets, QtCore, QtGui
    except Exception as e:  # pragma: no cover - optional dep
        raise RuntimeError(
            "PySide6 is required for the main window. Install GUI extras:\n"
            "  pip install PySide6 keyboard\n"
            f"(import error: {e})")

    def _open_path(path: Optional[str]):
        if not path:
            return
        try:
            if os.path.isfile(path) or os.path.isdir(path):
                os.startfile(path)            # Windows
        except Exception:
            pass

    class MainWindow(QtWidgets.QMainWindow):
        def __init__(self):
            super().__init__()
            self.ctrl = ctrl
            self.hooks = hooks
            self.setWindowTitle(PRODUCT_NAME)
            self.resize(880, 640)

            central = QtWidgets.QWidget()
            central.setObjectName("central")
            self.setCentralWidget(central)
            root = QtWidgets.QVBoxLayout(central)
            root.setContentsMargins(16, 16, 16, 12)
            root.setSpacing(12)

            # prominent START TUNING bar (filled-green primary button)
            top = QtWidgets.QHBoxLayout()
            top.setSpacing(12)
            self.start_btn = QtWidgets.QPushButton("  ▶  START TUNING  ")
            self.start_btn.setObjectName("primary")
            self.start_btn.setMinimumHeight(44)
            self.start_btn.setCursor(QtCore.Qt.PointingHandCursor)
            self.start_btn.clicked.connect(self._start_tuning)
            top.addWidget(self.start_btn)
            self.status_lbl = QtWidgets.QLabel("")
            self.status_lbl.setObjectName("muted")
            top.addWidget(self.status_lbl, 1)
            root.addLayout(top)

            self.tabs = QtWidgets.QTabWidget()
            root.addWidget(self.tabs, 1)
            self.tabs.addTab(self._build_dashboard(), "Dashboard")
            self.tabs.addTab(self._build_previous(), "Previous Tunes")
            self.tabs.addTab(self._build_logs(), "Logs")
            self.tabs.addTab(self._build_settings(), "Settings")
            self.tabs.addTab(self._build_help(), "Help")
            self.tabs.currentChanged.connect(lambda *_: self.refresh())

            self.refresh()

        def closeEvent(self, event):
            """Closing the window EXITS the app cleanly (saves the in-progress session,
            flushes its log, releases the UDP port) instead of lingering in the tray -
            users expect the X to actually close it, and a lingering process is exactly
            what loses sessions and blocks the port on the next launch."""
            if not getattr(self, "_quitting", False):
                self._quitting = True
                q = self.hooks.get("quit")
                if q:
                    q()
            event.accept()

        # ----- card / chip helpers --------------------------------------
        def _wrap_card(self, inner_layout):
            """Put a tab's content layout inside a card panel: 16px outer margin
            (around the tab content) + a panel with 1px hairline, radius 10, and
            16px inner padding / 12px element spacing."""
            page = QtWidgets.QWidget()
            page.setObjectName("tabPage")
            outer = QtWidgets.QVBoxLayout(page)
            outer.setContentsMargins(16, 16, 16, 16)
            card = QtWidgets.QFrame()
            card.setObjectName("card")
            inner_layout.setContentsMargins(16, 16, 16, 16)
            inner_layout.setSpacing(12)
            card.setLayout(inner_layout)
            outer.addWidget(card)
            return page

        def _stat_chip(self, label):
            """A dashboard stat chip: big accent number over a small muted label.
            Returns (frame, number_label) so the number can be refreshed."""
            frame = QtWidgets.QFrame()
            frame.setObjectName("statChip")
            v = QtWidgets.QVBoxLayout(frame)
            v.setContentsMargins(14, 12, 14, 12)
            v.setSpacing(2)
            num = QtWidgets.QLabel("-")
            num.setObjectName("statNumber")
            lbl = QtWidgets.QLabel(label)
            lbl.setObjectName("statLabel")
            v.addWidget(num)
            v.addWidget(lbl)
            return frame, num

        # ----- Dashboard -------------------------------------------------
        def _build_first_run_note(self):
            """A plain-language 'getting started' panel - shown until the first tune
            exists. Everything here is in-game; there is no command line."""
            note = QtWidgets.QFrame()
            note.setObjectName("noteCard")
            v = QtWidgets.QVBoxLayout(note)
            v.setContentsMargins(14, 12, 14, 12)
            v.setSpacing(4)
            title = QtWidgets.QLabel("Getting started — turn on the game's telemetry")
            title.setObjectName("noteTitle")
            body = QtWidgets.QLabel(
                "1.&nbsp; In Forza Horizon 6: <b>Settings → HUD and Gameplay → Data Out</b> "
                "— set it <b>On</b>.<br>"
                "2.&nbsp; Set Data Out IP <b>127.0.0.1</b> and Port <b>5607</b>.<br>"
                "3.&nbsp; Run the game <b>Borderless</b> (fullscreen-windowed) so the overlay "
                "can sit on top.<br>"
                "4.&nbsp; Drive for a few seconds, then press <b>▶ START TUNING</b> above. "
                "It only reads the game — you type the values in yourself.")
            body.setObjectName("noteBody")
            body.setTextFormat(QtCore.Qt.RichText)
            body.setWordWrap(True)
            v.addWidget(title)
            v.addWidget(body)
            return note

        def _build_dashboard(self):
            lay = QtWidgets.QVBoxLayout()
            self.first_run_note = self._build_first_run_note()
            lay.addWidget(self.first_run_note)
            strip = QtWidgets.QHBoxLayout()
            strip.setSpacing(12)
            tunes_chip, self.stat_tunes = self._stat_chip("tunes")
            iters_chip, self.stat_iters = self._stat_chip("iterations")
            time_chip, self.stat_time = self._stat_chip("time spent")
            for chip in (tunes_chip, iters_chip, time_chip):
                strip.addWidget(chip, 1)
            lay.addLayout(strip)
            self.dash_detail = QtWidgets.QTextEdit()
            self.dash_detail.setReadOnly(True)
            lay.addWidget(self.dash_detail, 1)
            return self._wrap_card(lay)

        def _refresh_dashboard(self):
            ss = self.ctrl.stats_summary()
            tt = ss.get("total_time_s")
            tt_s = f"{tt/3600:.1f} h" if tt else "-"
            self.stat_tunes.setText(str(ss["total_tunes"]))
            self.stat_iters.setText(str(ss["total_iterations"]))
            self.stat_time.setText(tt_s)
            # the getting-started note is for first run only - hide once a tune exists
            self.first_run_note.setVisible(ss["total_tunes"] == 0)
            L = []
            if ss["best_lap_by_car"]:
                L.append("Best lap per car:")
                for car, bl in sorted(ss["best_lap_by_car"].items()):
                    L.append(f"  {car}: {bl:.2f}s")
                L.append("")
            for title, d in (("By car", ss["by_car"]),
                             ("By discipline", ss["by_discipline"]),
                             ("By class", ss["by_class"])):
                if d:
                    L.append(title + ": " + ", ".join(f"{k} ({v})" for k, v in sorted(d.items())))
            L.append("")
            L.append("Recent activity:")
            for s in ss["recent"]:
                bl = f"{s['best_lap_s']:.2f}s" if s.get("best_lap_s") else "-"
                date = (s.get("date") or "")[:16].replace("T", " ")
                L.append(f"  {date}  {s['car']} | {s['class']} | {s['discipline']} | best {bl}")
            if not ss["recent"]:
                L.append("  (none yet - press START TUNING to make your first tune)")
            self.dash_detail.setPlainText("\n".join(L))

        # ----- Previous Tunes -------------------------------------------
        def _build_previous(self):
            lay = QtWidgets.QHBoxLayout()
            left = QtWidgets.QVBoxLayout()
            left.setSpacing(12)
            self.prev_list = QtWidgets.QListWidget()
            self.prev_list.setAlternatingRowColors(True)
            self.prev_list.currentRowChanged.connect(self._on_prev_select)
            left.addWidget(self.prev_list, 1)
            btns = QtWidgets.QHBoxLayout()
            btns.setSpacing(12)
            for label, fn in (("Load / Copy", self._prev_load),
                              ("Export...", self._prev_export),
                              ("Open run log", self._prev_open_log)):
                b = QtWidgets.QPushButton(label)
                b.clicked.connect(fn)
                btns.addWidget(b)
            left.addLayout(btns)
            lay.addLayout(left, 1)
            # the value sheet - the only main-window text area in monospace.
            self.prev_view = QtWidgets.QTextEdit()
            self.prev_view.setObjectName("valueSheet")
            self.prev_view.setReadOnly(True)
            lay.addWidget(self.prev_view, 1)
            return self._wrap_card(lay)

        def _refresh_previous(self):
            self._sessions = self.ctrl.previous_tunes()
            self.prev_list.clear()
            for s in self._sessions:
                bl = f"{s['best_lap_s']:.2f}s" if s.get("best_lap_s") else "-"
                date = (s.get("date") or "")[:10]
                self.prev_list.addItem(f"{s['car']}  |  {s['class']} {s['discipline']}  "
                                       f"|  {date}  |  best {bl}")
            if self._sessions:
                self.prev_list.setCurrentRow(0)
            else:
                self.prev_view.setPlainText("No saved tunes yet.")

        def _selected_session(self):
            i = self.prev_list.currentRow()
            if 0 <= i < len(getattr(self, "_sessions", [])):
                return self._sessions[i]
            return None

        def _on_prev_select(self, *_):
            s = self._selected_session()
            if not s:
                return
            txt = s.get("final_txt")
            if txt and os.path.exists(txt):
                try:
                    self.prev_view.setPlainText(open(txt, encoding="utf-8").read())
                    return
                except OSError:
                    pass
            self.prev_view.setPlainText("(tune sheet not found on disk)")

        def _prev_load(self):
            s = self._selected_session()
            if not s:
                return
            QtWidgets.QApplication.clipboard().setText(self.prev_view.toPlainText())
            self._toast("Tune copied to clipboard.")

        def _prev_export(self):
            s = self._selected_session()
            if not s:
                return
            dest = QtWidgets.QFileDialog.getExistingDirectory(self, "Export tune files to...")
            if not dest:
                return
            n = 0
            for key in ("final_txt", "tune_json", "session_json"):
                p = s.get(key)
                if p and os.path.exists(p):
                    try:
                        shutil.copy(p, dest)
                        n += 1
                    except OSError:
                        pass
            self._toast(f"Exported {n} file(s) to {dest}.")

        def _prev_open_log(self):
            s = self._selected_session()
            if s:
                _open_path(s.get("session_json"))

        # ----- Logs ------------------------------------------------------
        def _build_logs(self):
            lay = QtWidgets.QVBoxLayout()
            intro = QtWidgets.QLabel(
                "Diagnostics and exports. The support bundle is one zip you can send "
                "for help (run JSON + final tune + recent app.log + Heat frames + env).")
            intro.setWordWrap(True)
            lay.addWidget(intro)
            for label, fn in (("Write support bundle (zip)", self._write_bundle),
                              ("Save current session now", self._save_now),
                              ("Export cumulative tune log", self._export_cumulative),
                              ("Open tunes / logs folder", lambda: _open_path(self._tunes_dir())),
                              ("Open captures folder", lambda: _open_path(self._captures_dir())),
                              ("Open app log file", self._open_app_log)):
                b = QtWidgets.QPushButton(label)
                b.setMinimumHeight(32)
                b.clicked.connect(fn)
                lay.addWidget(b)
            lay.addStretch(1)
            self.logs_status = QtWidgets.QLabel("")
            self.logs_status.setObjectName("muted")
            lay.addWidget(self.logs_status)
            return self._wrap_card(lay)

        def _tunes_dir(self):
            from ..state import store
            return store.SESSIONS_DIR

        def _captures_dir(self):
            return self.hooks.get("captures_dir", lambda: "captures")()

        def _write_bundle(self):
            fn = self.hooks.get("support_bundle")
            path = fn() if fn else None
            self.logs_status.setText(f"Support bundle: {path}" if path else "Bundle failed.")

        def _save_now(self):
            """Force-save the current (possibly in-progress) session to disk now -
            works mid-session, before any normal completion."""
            fn = self.hooks.get("save_now")
            ok = fn() if fn else False
            self.logs_status.setText("Session saved to the tunes/logs folder."
                                     if ok else "No active session to save.")

        def _export_cumulative(self):
            src = os.path.join(self._tunes_dir(), "cumulative_tune_log.md")
            if not os.path.exists(src):
                self.logs_status.setText("No cumulative log yet.")
                return
            dest, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "Save cumulative log", "cumulative_tune_log.md", "Markdown (*.md)")
            if dest:
                try:
                    shutil.copy(src, dest)
                    self.logs_status.setText(f"Saved {dest}")
                except OSError as e:
                    self.logs_status.setText(f"Save failed: {e}")

        def _open_app_log(self):
            _open_path(self.hooks.get("app_log"))

        # ----- Settings --------------------------------------------------
        def _build_settings(self):
            lay = QtWidgets.QVBoxLayout()

            form = QtWidgets.QFormLayout()
            self.set_port = QtWidgets.QSpinBox()
            self.set_port.setRange(1, 65535)
            self.set_port.setValue(int(getattr(self.ctrl, "port", 5607)))
            self.set_port.valueChanged.connect(
                lambda v: setattr(self.ctrl, "port", int(v)))
            form.addRow("Telemetry port (applies next start)", self.set_port)

            # Max tuning time: the wall-clock CEILING for a run (0 = unlimited). The tool
            # can still finish earlier if it converges. Shared with the setup form via
            # prefs (one source of truth) and applied live to a running budget.
            self.set_budget = QtWidgets.QSpinBox()
            self.set_budget.setRange(0, 240)
            self.set_budget.setSuffix(" min")
            self.set_budget.setSpecialValueText("Unlimited / off")
            self.set_budget.setValue(int(getattr(self.ctrl, "time_budget_min", 0) or 0))
            self.set_budget.setToolTip(
                "Wall-clock ceiling for a tuning run, from your first Rivals lap "
                "(includes loads/menus; never paused). 0 = unlimited. The tool stops "
                "earlier if it converges. Changing this applies to a run already going.")
            self.set_budget.valueChanged.connect(self._set_budget)
            form.addRow("Max tuning time (minutes)", self.set_budget)

            # Console mode: Forza on Xbox/console streaming Data Out over the LAN. Tyre
            # temps fall back to the single UDP value (camber/toe less accurate).
            self.set_console = QtWidgets.QCheckBox(
                "Forza runs on Xbox/console (telemetry over the LAN)")
            self.set_console.setChecked(bool(getattr(self.ctrl, "console_mode", False)))
            self.set_console.setToolTip(
                "Receive telemetry from a console on your network instead of this PC. "
                "Listens on all interfaces; tyre temps use the single per-corner UDP "
                "value so camber/toe are less accurate (no in-game Heat screen to read).")
            self.set_console.toggled.connect(self._set_console)
            form.addRow("Console mode", self.set_console)

            # Verbose telemetry logging: the high-frequency raw per-packet dumps. OFF by
            # default (kept out of app.log + the support bundle); ON writes them to a
            # separate raw_telemetry.log for deep debugging.
            from ..state import prefs as _prefs
            self.set_verbose = QtWidgets.QCheckBox(
                "Verbose telemetry logging (raw packets -> raw_telemetry.log)")
            self.set_verbose.setChecked(bool(_prefs.get("verbose_telemetry", False)))
            self.set_verbose.setToolTip(
                "Off by default. The decision log (app.log + the per-session log) stays "
                "readable; turn this on only to capture raw per-packet telemetry for "
                "deep debugging - it does NOT go in the support bundle.")
            self.set_verbose.toggled.connect(self._set_verbose)
            form.addRow("Diagnostics", self.set_verbose)
            self.console_hint = QtWidgets.QLabel()
            self.console_hint.setWordWrap(True)
            self.console_hint.setStyleSheet("color:#9aa;font-size:11px")
            self._refresh_console_hint(self.set_console.isChecked())
            form.addRow("", self.console_hint)

            self.set_temp = QtWidgets.QComboBox()
            self.set_temp.addItems(["auto", "manual"])
            self.set_temp.setCurrentText(getattr(self.ctrl, "temp_mode", "auto"))
            self.set_temp.currentTextChanged.connect(
                lambda t: setattr(self.ctrl, "temp_mode", t))
            form.addRow("Tyre-temp reader", self.set_temp)

            self.set_vision = QtWidgets.QCheckBox("Use Anthropic vision API if a key is set")
            self.set_vision.setChecked(bool(getattr(self.ctrl, "use_vision_api", False)))
            self.set_vision.toggled.connect(
                lambda b: setattr(self.ctrl, "use_vision_api", bool(b)))
            form.addRow("Cloud reader (optional)", self.set_vision)

            self.set_view = QtWidgets.QComboBox()
            self.set_view.addItems(["simple", "advanced"])
            self.set_view.setCurrentText(getattr(self.ctrl, "view_mode", "simple"))
            self.set_view.currentTextChanged.connect(self.ctrl.set_view_mode)
            form.addRow("Overlay default view", self.set_view)

            # Show the overlay in screen recordings/screenshots (default OFF). Uses
            # `clicked` (user action only) so reverting on Cancel doesn't re-trigger.
            self.set_capture = QtWidgets.QCheckBox("Show overlay in screen recordings")
            self.set_capture.setChecked(bool(getattr(self.ctrl, "overlay_capturable", False)))
            self.set_capture.clicked.connect(self._toggle_capture)
            form.addRow("Screen recording", self.set_capture)
            lay.addLayout(form)

            hdr = QtWidgets.QHBoxLayout()
            hdr.addWidget(QtWidgets.QLabel("<b>Saved car names</b> (edit or forget)"))
            hdr.addStretch(1)
            import_b = QtWidgets.QPushButton("Import car names...")
            import_b.clicked.connect(self._import_names)
            hdr.addWidget(import_b)
            lay.addLayout(hdr)
            self.names_table = QtWidgets.QTableWidget(0, 2)
            self.names_table.setHorizontalHeaderLabels(["Ordinal", "Name"])
            self.names_table.horizontalHeader().setStretchLastSection(True)
            self.names_table.verticalHeader().setVisible(False)
            self.names_table.verticalHeader().setDefaultSectionSize(28)
            self.names_table.setAlternatingRowColors(True)
            lay.addWidget(self.names_table, 1)
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(12)
            save_b = QtWidgets.QPushButton("Save name")
            save_b.clicked.connect(self._save_name)
            forget_b = QtWidgets.QPushButton("Forget selected")
            forget_b.setObjectName("danger")
            forget_b.clicked.connect(self._forget_name)
            row.addWidget(save_b)
            row.addWidget(forget_b)
            row.addStretch(1)
            lay.addLayout(row)
            return self._wrap_card(lay)

        def _import_names(self):
            from . import import_dialog
            try:
                s = import_dialog.show_import_dialog(self)
            except Exception as e:
                self._toast(f"Import failed: {e}")
                return
            self._refresh_settings()
            if s and s.get("parsed"):
                self._toast(f"Imported {s['imported']} new name(s); kept {s['already']} "
                            f"yours; {s['malformed']} skipped.")

        def _refresh_settings(self):
            sv = self.ctrl.settings_view()
            self.names_table.setRowCount(0)
            for c in sv.get("car_names", []):
                r = self.names_table.rowCount()
                self.names_table.insertRow(r)
                ord_item = QtWidgets.QTableWidgetItem(str(c["ordinal"]))
                ord_item.setFlags(ord_item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.names_table.setItem(r, 0, ord_item)
                self.names_table.setItem(r, 1, QtWidgets.QTableWidgetItem(c["name"]))

        def _save_name(self):
            r = self.names_table.currentRow()
            if r < 0:
                return
            try:
                ordinal = int(self.names_table.item(r, 0).text())
            except (TypeError, ValueError):
                return
            name = self.names_table.item(r, 1).text() if self.names_table.item(r, 1) else ""
            self.ctrl.rename_car(ordinal, name)
            self._toast(f"Saved #{ordinal} -> {name}")

        def _set_budget(self, minutes):
            """Apply the Max-tuning-time control: update the live controller budget and
            persist it (shared with the setup form). 0 = unlimited."""
            from ..state import prefs
            self.ctrl.set_time_budget(float(minutes))
            prefs.set("time_budget_min", float(minutes))

        def _set_verbose(self, on):
            from ..state import prefs
            from . import app as _app
            prefs.set("verbose_telemetry", bool(on))
            _app.configure_raw_telemetry_log(bool(on))   # applies live

        def _set_console(self, on):
            """Toggle console mode: rebind the listener live, persist, update the hint."""
            from ..state import prefs
            self.ctrl.set_console_mode(bool(on))
            prefs.set("console_mode", bool(on))
            self._refresh_console_hint(bool(on))

        def _refresh_console_hint(self, on):
            if on:
                ip = self.ctrl.lan_ip()
                self.console_hint.setText(
                    f"In Forza on the console, set <b>Data Out</b> IP to <b>{ip}</b>, "
                    f"port <b>{int(getattr(self.ctrl, 'port', 5607))}</b>, format "
                    "<b>Dash</b>. Camber/toe are less accurate on console (single tyre "
                    "temp, no 3-zone Heat reading).")
            else:
                self.console_hint.setText(
                    "Off: telemetry from Forza on this PC (loopback), full 3-zone Heat OCR.")

        def _toggle_capture(self, checked):
            """Ticking ON requires confirmation (it can let the overlay obscure the
            Heat-page temps); unticking is immediate. The change applies to the live
            overlay at once via the apply_overlay_capture hook."""
            if checked:
                ok = QtWidgets.QMessageBox.warning(
                    self, "Show overlay in recordings?",
                    "With this on, the overlay will appear in screen recordings and "
                    "screenshots — including the tyre-temperature screenshots LapSmith "
                    "takes to read your Heat page. Make sure the overlay is positioned "
                    "so it doesn't cover any of the tyre temperatures on the Heat page, "
                    "or those temps may not read and LapSmith will fall back to tuning "
                    "camber by lap time. Move the overlay out of the way before bringing "
                    "up the Heat page.",
                    QtWidgets.QMessageBox.Ok | QtWidgets.QMessageBox.Cancel,
                    QtWidgets.QMessageBox.Cancel)
                if ok != QtWidgets.QMessageBox.Ok:
                    self.set_capture.setChecked(False)   # leave it OFF, no change
                    return
            self.ctrl.overlay_capturable = bool(checked)
            self.hooks.get("apply_overlay_capture", lambda: None)()
            self._toast("Overlay will show in recordings." if checked
                        else "Overlay hidden from recordings.")

        def _forget_name(self):
            r = self.names_table.currentRow()
            if r < 0:
                return
            try:
                ordinal = int(self.names_table.item(r, 0).text())
            except (TypeError, ValueError):
                return
            self.ctrl.forget_car(ordinal)
            self._refresh_settings()

        # ----- Help ------------------------------------------------------
        def _build_help(self):
            lay = QtWidgets.QVBoxLayout()
            view = QtWidgets.QTextBrowser()
            view.setOpenExternalLinks(True)   # any links open in the system browser
            # comfortable line height + themed headings/tables for the rich guide
            view.document().setDefaultStyleSheet(
                "h2{color:#e6eaed;font-size:18px;margin:0 0 6px;}"
                "h3{color:#2fb24c;font-size:14px;margin:16px 0 4px;}"
                "p,li{color:#e6eaed;line-height:150%;}"
                "b{color:#e6eaed;}"
                "td{padding:3px 14px 3px 0;color:#e6eaed;}")
            view.setHtml(_HELP_HTML)
            lay.addWidget(view)
            return self._wrap_card(lay)

        # ----- shared ----------------------------------------------------
        def refresh(self):
            try:
                self._refresh_dashboard()
                self._refresh_previous()
                self._refresh_settings()
            except Exception:
                pass

        def _toast(self, msg: str):
            self.status_lbl.setText(msg)

        def _start_tuning(self):
            fn = self.hooks.get("start_tuning")
            if fn:
                fn()

        def closeEvent(self, ev):
            # closing hides to the tray; the app keeps running (telemetry + overlay
            # stay alive). Only the tray Quit action actually exits.
            ev.ignore()
            self.hide()
            notify = self.hooks.get("on_hidden")
            if notify:
                notify()

    return MainWindow()
