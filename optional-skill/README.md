# Optional skill — opt in, not installed by default

The `bartertown` usage skill lives here (not under `skills/`) so importing the
pack does **not** auto-materialize it into every agent. That keeps the always-on
per-agent skill-description cost off cities that don't want it.

**To install the skill (city-wide):** copy it into your city's skill catalog —
`cp -r <pack>/optional-skill/bartertown <city>/skills/bartertown` — or into one
agent's catalog: `<city>/agents/<name>/skills/bartertown`.

Most cities don't need it: the `barter_*` tools (scoped via `config.participants`)
plus the opt-in `bartertown-v0` prompt fragment already deliver the guidance.
See the MAYOR-GUIDE "Wiring participation" checklist.
