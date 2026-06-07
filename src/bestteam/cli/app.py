from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from .. import __version__
from ..core.loader import load_workflow
from ..exceptions import BestTeamError
from . import scaffold

app = typer.Typer(
    name="bestteam",
    help="Build and run multi-agent workflows without touching the underlying engine.",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"bestteam {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the bestteam version and exit.",
    )
) -> None:
    """bestteam — business-friendly multi-agent workflows, powered by LangGraph under the hood."""


@app.command()
def init(
    project: str = typer.Argument(..., help="Name of the project directory to create"),
    directory: Path = typer.Option(Path("."), "--dir", help="Parent directory for the new project"),
) -> None:
    """Scaffold a new bestteam project with a sample reviewer/fixer workflow."""
    target = directory / project
    try:
        created = scaffold.create_project(target)
    except FileExistsError:
        err_console.print(f"[red]Error:[/red] '{target}' already exists")
        raise typer.Exit(code=1)

    console.print(f"[green]Created project[/green] at [bold]{target}[/bold]")
    for file_path in created:
        console.print(f"  + {file_path.relative_to(target)}")

    console.print("\n[bold]Next steps[/bold]")
    console.print(f"  cd {target}")
    console.print("  pip install langchain langchain-openai   # or your preferred provider")
    console.print('  $env:OPENAI_API_KEY = "sk-..."')
    console.print('  bestteam run workflow.yaml "Review this Python function for bugs: ..."')


@app.command()
def run(
    workflow_file: Path = typer.Argument(..., help="Path to a workflow YAML file"),
    input: str = typer.Argument(..., help="Input text to feed the workflow"),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print each agent's intermediate output"
    ),
) -> None:
    """Load a workflow from YAML and run it against the given input."""
    try:
        workflow = load_workflow(workflow_file)
        result = workflow.run(input)
    except BestTeamError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    if verbose:
        for step in result.steps:
            console.print(Panel(step["output"], title=step["agent"], border_style="cyan"))

    console.print(Panel(result.output, title="Final output", border_style="green"))


@app.command()
def graph(
    workflow_file: Path = typer.Argument(..., help="Path to a workflow YAML file"),
) -> None:
    """Print the compiled workflow graph as Mermaid markup (paste into a Mermaid viewer)."""
    try:
        workflow = load_workflow(workflow_file)
        diagram = workflow.visualize()
    except BestTeamError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1)

    console.print(diagram)
