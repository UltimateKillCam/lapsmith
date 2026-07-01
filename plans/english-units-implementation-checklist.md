# LapSmith English/Metric telemetry display plan

Status
- Repo: `/root/lapsmith`
- Branch: `feat/english-units-scaffold`
- Fork remote: `origin https://github.com/dgallagher33/lapsmith.git`
- Upstream remote: `upstream https://github.com/UltimateKillCam/lapsmith.git`

Goal
- Add an upstream-friendly telemetry display unit option that lets users choose English or Metric readouts for telemetry-facing values.
- Preserve canonical internal math and packet parsing.
- Keep the existing pressure-unit preference independent in PR #1.

Non-goals for PR #1
- Do not change parser offsets or wire-unit handling.
- Do not change analyzer thresholds or fitness logic.
- Do not sweep all diagnostics/rule prose for unit conversion.
- Do not convert tune-sheet dimensions like `kgf/mm` or `cm` unless the maintainer explicitly wants that.
- Do not collapse `pressure_unit` into a new global unit setting.

## Scope definition

In scope
- New persisted preference: telemetry display unit system
- Speed display in overlay, web mirror, and CLI/live watch
- Display-aware status payload for UI surfaces
- Tests for persistence, payload, rendering, and non-regression

Maybe in scope only if naturally cheap and localized
- Direct telemetry temperature readouts that are purely display-facing

Out of scope
- Rule threshold wording repo-wide
- Export/storage canonical values
- OCR/manual temp ingestion behavior
- Tune sheet dimension conversion

## Product/UX decisions

Preference name
- Internal key: `telemetry_unit_system`

Allowed values
- `english`
- `metric`

Default
- `english`

Rationale
- Current behavior is already mph-first in user-facing telemetry surfaces.
- Preserving defaults reduces upstream surprise.

Settings label
- `Telemetry units`

Settings choices
- `English`
- `Metric`

Important UX note
- Keep `Pressure unit` as a separate setting in PR #1.

## Architecture decisions

Canonical internal units remain unchanged
- Speed stays canonical in m/s
- UDP tyre temps stay canonical in Celsius after parser normalization
- Pressure logic remains independent and unchanged

Display logic should live at the UI/controller edge
- Do not move formatting into parser strings
- Prefer controller-level helper methods or a tiny pure helper path used by controller/CLI

Preferred status payload contract
```python
live = {
    "speed_value": 123.4,
    "speed_unit": "mph",   # or "km/h"
    "speed_text": "123.4 mph",
    "rpm": 7000,
    "gear": 4,
    "lat_g": 1.02,
    "drivetrain": "AWD",
    "drivetrain_raw": 2,
    "num_cylinders": 6,
}
```

Why
- Removes hardcoded mph logic from overlay/web
- Gives a single source of truth for display formatting
- Keeps canonical math away from rendering

## Exact implementation order

### 1. Add persisted preference plumbing
Files
- `lapsmith/state/prefs.py`
- `lapsmith/gui/app.py`
- `lapsmith/gui/controller.py`
- `lapsmith/gui/main_window.py`

Changes
1. In `lapsmith/gui/controller.py`
   - Add field:
     - `telemetry_unit_system: str = "english"`
2. In `lapsmith/gui/app.py`
   - Read `prefs.get("telemetry_unit_system", "english")`
   - Validate against `("english", "metric")`
   - Apply to `ctrl.telemetry_unit_system`
3. In `lapsmith/gui/main_window.py`
   - Add a `QComboBox` near `Pressure unit`
   - UI label: `Telemetry units`
   - Choices: `English`, `Metric`
   - On change:
     - persist via `prefs.set("telemetry_unit_system", ...)`
     - update `self.ctrl.telemetry_unit_system` live
4. In `lapsmith/state/prefs.py`
   - Optional helper function:
     - `def telemetry_unit_system() -> str:`
   - If added, it should sanitize invalid values to `english`

Acceptance check
- Preference survives restart
- Invalid stored value falls back safely to `english`

### 2. Add display helpers for speed
Primary file
- `lapsmith/gui/controller.py`

Preferred helpers
- `_speed_value_unit(self, snap) -> tuple[float, str]`
- maybe `_speed_text(self, snap) -> str`

Behavior
- `english` => mph
- `metric` => km/h
- Use existing packet helpers where available:
  - `snap.speed_mph`
  - `snap.speed_kmh`

Formatting convention
- Use `mph`
- Use `km/h`
- Be consistent across every surface

Acceptance check
- A known packet speed produces the expected display value and unit for both systems

### 3. Refactor controller `status()` payload
Primary file
- `lapsmith/gui/controller.py`

Current issue
- `status()` exposes `live["speed_mph"]` only

Planned change
- Replace or augment with display-aware fields:
  - `speed_value`
  - `speed_unit`
  - `speed_text`

Recommendation
- Update all consumers in the same PR
- If useful for a gentle transition, keep `speed_mph` temporarily during the refactor, but it is not required if all current call sites are updated cleanly

Acceptance check
- `status()["live"]` exposes display-aware speed fields
- Existing tests for drivetrain/raw fields still pass unchanged

### 4. Update overlay
File
- `lapsmith/gui/overlay.py`

Current issue
- Hardcoded mph rendering in advanced live display

Planned change
- Render from `live["speed_text"]` or `live["speed_value"]` + `live["speed_unit"]`

Recommendation
- Prefer `speed_text` if controller owns final formatting

Acceptance check
- Overlay renders mph for English, km/h for Metric
- No other overlay sections regress

### 5. Update web mirror
File
- `lapsmith/gui/web.py`

Current issue
- Hardcoded `Speed ${s.live.speed_mph} mph`

Planned change
- Render `speed_text` or `speed_value` + `speed_unit`

Acceptance check
- Web line reflects selected display units
- No stale `mph` literal remains in the display line

### 6. Update CLI/live watch
File
- `lapsmith/main_loop.py`

Current issue
- Hardcoded mph in live watch output

Planned change
- Use the same speed conversion policy as the controller/UI
- If controller helpers are awkward here, use one tiny pure helper rather than duplicating multiple formatting rules

Acceptance check
- CLI live output switches between mph and km/h consistently

### 7. Decide whether to include telemetry temperature readouts
Files to inspect if included
- `lapsmith/gui/overlay.py`
- `lapsmith/gui/controller.py`
- any direct telemetry temp readout surfaces

Rule
- Include only if these are straightforward display readouts
- Do not start rewriting rules/diagnostic copy in PR #1

Acceptance check
- If included, temp display conversion is display-only and internal Celsius logic remains unchanged

### 8. Extend self-tests
Primary file
- `selftest.py`

Add tests for:
1. Preference persistence
   - `telemetry_unit_system` defaults to `english`
   - persisted `metric` reloads successfully
   - invalid value sanitizes to `english`
2. Controller status payload
   - `english` gives mph
   - `metric` gives km/h
   - `speed_unit` and `speed_text` are consistent
3. Overlay rendering
   - rendered HTML reflects mph or km/h depending on controller setting
4. Pressure non-regression
   - keep existing `psi/bar` tests green
5. Existing status payload non-regression
   - drivetrain/cylinder fields still surface correctly

Good existing test anchors
- `status()` assertions around lines ~480, ~589, ~1311, ~1644, ~2422
- pressure regression block around lines ~2218+
- overlay render tests around lines ~3021+

### 9. Verification run
Commands
```bash
cd /root/lapsmith
python3 selftest.py
```

If selftest still fails for pre-existing environment reasons
- capture the exact failure
- distinguish pre-existing failure from new regression
- do not claim green unless actually green

### 10. Review diff before PR
Checklist
- No parser logic changes unless absolutely necessary
- No analyzer/rules threshold changes
- No unrelated formatting churn
- No pressure-unit behavior regressions
- No stale mph-only UI path left behind among overlay/web/CLI

## Suggested code surfaces summary

High-confidence edit list
- `lapsmith/state/prefs.py`
- `lapsmith/gui/controller.py`
- `lapsmith/gui/app.py`
- `lapsmith/gui/main_window.py`
- `lapsmith/gui/overlay.py`
- `lapsmith/gui/web.py`
- `lapsmith/main_loop.py`
- `selftest.py`

Do not touch unless scope expands deliberately
- `lapsmith/telemetry/parser.py`
- `lapsmith/telemetry/listener.py`
- `lapsmith/telemetry/session.py`
- `lapsmith/knowledge/rules.py`

## Acceptance criteria

Functional
- User can choose `Telemetry units` in Settings
- Choice persists across app restarts
- Overlay honors selected speed unit
- Web/LAN view honors selected speed unit
- CLI live watch honors selected speed unit
- Internal parsing and analyzer math remain canonical
- Existing pressure unit setting continues to work independently

Regression
- No telemetry parser behavior changes
- No analyzer threshold changes
- Existing pressure tests remain green
- Existing status payload regression tests remain green after adapting for new speed fields

Reviewability
- Diff remains small and focused
- Defaults preserved
- PR can be described honestly as a display-layer improvement

## Risks and mitigations

Risk: scope creep into every unit string in the repo
- Mitigation: keep PR #1 to telemetry display surfaces only

Risk: inconsistency between overlay/web/CLI
- Mitigation: centralize speed display fields in controller status payload

Risk: confusion between `Pressure unit` and `Telemetry units`
- Mitigation: keep both explicit and separate in PR #1

Risk: accidental logic change in analysis
- Mitigation: no rule/fitness/parser changes; test display edges only

Risk: formatting drift (`kmh` vs `km/h`)
- Mitigation: standardize on `mph` and `km/h`

## Suggested PR title
- Add telemetry display unit option for English/Metric readouts

## Suggested PR description outline
1. Problem
   - Several telemetry-facing UI paths hardcode mph
2. Approach
   - Add persisted telemetry display-unit preference
   - Keep internal telemetry math unchanged
   - Update overlay/web/CLI together
   - Leave pressure-unit preference intact
3. Why this scope
   - Preserves defaults
   - Minimizes regression risk
   - Follows the app’s existing canonical-internal/display-edge pattern
