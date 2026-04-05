"""CLI 진입점 - ytmaker 명령어."""

from __future__ import annotations

import click

from .produce import produce_cmd, resume_cmd, status_cmd


@click.group()
@click.version_option(version="0.1.0", prog_name="ytmaker")
def cli() -> None:
    """YouTube Long Form Maker - AI 기반 자동 영상 제작."""
    pass


cli.add_command(produce_cmd, "produce")
cli.add_command(resume_cmd, "resume")
cli.add_command(status_cmd, "status")


if __name__ == "__main__":
    cli()
