"""Prompt texts live in prompts/<name>.md next to the package, so they can be read and edited as prose."""

from pathlib import Path

DIR = Path(__file__).resolve().parent.parent / "prompts"


def load(name):
    """The prompt prompts/<name>.md, without its trailing newline."""
    return (DIR / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")
