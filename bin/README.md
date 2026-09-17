# `bin/` — Sparrow executable

The Python runner (`core/sparrow_runner.py`) looks for the Sparrow nesting
solver here automatically, resolved relative to the project root:

- `bin/sparrow`      (Linux / macOS)
- `bin/sparrow.exe`  (Windows)

Drop the executable in this folder and the app finds it with no configuration.

## Discovery order

The runner resolves the binary in this order:

1. an explicit path passed to `run_sparrow(..., executable=...)`
2. the `SPARROW_BIN` environment variable
3. **this folder** — `bin/sparrow` / `bin/sparrow.exe`
4. `sparrow` / `sparrow.exe` on the system `PATH`

## Note on committing the binary

The executables in this folder are **git-ignored** (see `.gitignore`): they are
large and platform-specific, so the one you build/download for your machine is
usually not the one a Linux deployment (e.g. Streamlit Cloud) needs. Build or
download the right Sparrow binary for each target from
<https://github.com/JeroenGar/sparrow> and place it here.

If you deliberately want to commit a binary (e.g. a Linux build for your
deployment), force-add it:

```bash
git add -f bin/sparrow
```
