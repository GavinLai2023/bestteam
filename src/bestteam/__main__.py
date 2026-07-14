"""Enable `python -m bestteam` (CR-015).

The installed console script `bestteam` points at `bestteam.cli:app`; this
module makes the documented `python -m bestteam ...` invocation work too by
delegating to the same Typer app.
"""

from bestteam.cli import app

if __name__ == "__main__":
    app()
