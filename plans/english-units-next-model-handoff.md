You are working in the LapSmith repo and should continue from an already-prepared branch/fork scaffold.

Environment state
- Local repo: `/root/lapsmith`
- Current branch: `feat/english-units-scaffold`
- Fork remote: `origin https://github.com/dgallagher33/lapsmith.git`
- Upstream remote: `upstream https://github.com/UltimateKillCam/lapsmith.git`
- GitHub auth is configured and working via `gh`

User goal
- Prepare an upstream-quality PR plan and likely implementation for adding English units as an option instead of hardcoded metric/other inconsistent displays.
- The user wants a respectful, best-practices contribution they can feel confident submitting upstream.
- The user wants the smallest reviewable PR that stays on task.

What has already been researched
1. Telemetry intake path has been inspected first:
   - `lapsmith/telemetry/parser.py`
   - `lapsmith/telemetry/listener.py`
   - `lapsmith/telemetry/session.py`
2. FH6 packet findings from repo + corroborating external sources:
   - base packet length is `323`
   - common live packet length is `324`
   - there is a Horizon/FH6 12-byte insert after `NumCylinders`
   - speed is canonical in m/s
   - UDP tyre temps arrive in Fahrenheit and are normalized to Celsius in the parser
   - parser already exposes `speed_mph` and `speed_kmh`
3. Existing unit handling in repo:
   - pressure display already has an independent preference: `psi` / `bar`
   - tyre temp ingestion is already unit-aware for OCR/manual input and canonicalized internally
   - several user-facing telemetry surfaces still hardcode mph
4. GitHub scaffold is already done:
   - fork exists
   - branch is pushed
   - remotes are in standard origin/upstream layout

Current planning conclusion
- The first PR should be a narrow display-layer feature, not a repo-wide unit refactor.
- Preserve canonical internal units and existing tuning/analyzer logic.
- Leave the existing `pressure_unit` setting independent in PR #1.
- Focus on telemetry-facing display surfaces first.

Recommended PR #1 scope
In scope
- Add persisted preference: `telemetry_unit_system = english|metric`
- Default to `english` to preserve existing mph-heavy behavior
- Update speed display in overlay, web mirror, and CLI/live watch
- Refactor controller `status()` payload so display surfaces stop depending on hardcoded `speed_mph`
- Add tests for preference persistence, payload behavior, rendering, and pressure non-regression

Out of scope
- parser/listener/session logic changes
- analyzer/rules threshold changes
- sweeping rule/diagnostic prose rewrite
- tune-sheet dimension conversion (`kgf/mm`, `cm`, etc.)
- collapsing `pressure_unit` into a global unit system

Exact files likely to change
- `lapsmith/state/prefs.py`
- `lapsmith/gui/controller.py`
- `lapsmith/gui/app.py`
- `lapsmith/gui/main_window.py`
- `lapsmith/gui/overlay.py`
- `lapsmith/gui/web.py`
- `lapsmith/main_loop.py`
- `selftest.py`

Important existing code facts
- `lapsmith/state/prefs.py` is a tiny JSON key/value store used by existing settings
- `lapsmith/gui/app.py` already restores prefs like `pressure_unit`, `console_mode`, `time_budget_min`
- `lapsmith/gui/controller.py` already has `pressure_unit` and status payload shaping
- `lapsmith/gui/main_window.py` already has a `Pressure unit` combobox and live persistence pattern
- `lapsmith/gui/controller.py` currently exposes `status()["live"]["speed_mph"]`
- `lapsmith/gui/overlay.py`, `lapsmith/gui/web.py`, and `lapsmith/main_loop.py` currently hardcode mph in user-facing output

Preferred implementation shape
- Add controller field: `telemetry_unit_system: str = "english"`
- Add a controller helper for display speed conversion using canonical packet values
- Update `status()["live"]` to expose something like:
  - `speed_value`
  - `speed_unit`
  - `speed_text`
- Update overlay/web/CLI to use the new display-aware fields
- Keep pressure behavior untouched except for non-regression verification

Formatting convention
- English speed unit: `mph`
- Metric speed unit: `km/h`

Testing guidance
There is already a strong `selftest.py` suite with relevant anchors:
- status payload assertions around lines ~480, ~589, ~1311, ~1644, ~2422
- pressure/unit regression block around lines ~2218+
- overlay render checks around lines ~3021+

Add tests for:
1. new preference persistence/sanitization
2. controller status payload speed fields in english + metric
3. overlay render reflects mph/km/h correctly
4. existing pressure unit behavior still passes
5. existing drivetrain/raw status fields still pass

Verification requirement
- Actually run `python3 selftest.py`
- If it fails, capture the exact failure and distinguish pre-existing issues from regressions introduced by the change
- Do not claim green unless it is truly green

Constraints from user preference
- Keep the plan respectful to the original maintainer
- Keep the diff narrow and reviewable
- Avoid unrelated cleanup and architecture churn

Artifacts already created in repo
- `/root/lapsmith/plans/english-units-implementation-checklist.md`
- `/root/lapsmith/plans/english-units-next-model-handoff.md`

Immediate next task options
Option A: implement the scoped PR now
Option B: refine the checklist into a task-by-task coding plan before touching code

If you implement now, follow this order
1. pref plumbing
2. controller display helper(s)
3. status payload refactor
4. overlay
5. web
6. CLI
7. tests
8. run selftest
9. review diff for scope creep
