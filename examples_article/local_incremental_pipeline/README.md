# FastFlowTransform project scaffold

This project was created with `fft init`.

What lives here:
- models/: SQL (`*.ff.sql`) and Python (`*.ff.py`) models.
  - models/macros/: Jinja SQL macros loaded automatically.
  - models/macros_py/: Python helpers exposed as Jinja globals/filters.
- seeds/: CSV/Parquet inputs for reproducible seeds (see docs/Quickstart.md).
- sources.yml: External tables for source('group','table').
- profiles.yml: Engine connections; defaults come from docs/Profiles.md.
- packages.yml: Optional shared models/macros (docs/Packages.md).
- tests/unit/: YAML specs for `fft utest` (docs/Unit_Tests.md).
- tests/dq/: Custom data-quality tests for `fft test` (docs/Data_Quality_Tests.md).
- hooks/: SQL or Python hooks referenced from project.yml (docs/Hooks.md).
- docs/: Notes plus generated DAG site when using `fft dag --html`.

Next steps:
1. Update `profiles.yml` with real connection details (docs/Profiles.md).
2. Add sources in `sources.yml` and author models under `models/`    (docs/Config_and_Macros.md).
3. Wire packages (optional) in `packages.yml` if you reuse shared    models/macros (docs/Packages.md).
4. Seed sample data with `fft seed` and execute models    with `fft run` (docs/Quickstart.md).
