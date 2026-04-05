"""CLI produce/resume/status 명령어."""

from __future__ import annotations

import asyncio
import logging
import sys

import click
from rich.console import Console
from rich.table import Table

from ..core.config import list_channels, load_channel
from ..core.cost_tracker import CostTracker
from ..core.state import StateManager
from ..pipeline.orchestrator import Orchestrator

console = Console()


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@click.command("produce")
@click.option("--channel", "-c", required=True, help="채널 ID (예: finance)")
@click.option("--topic", "-t", required=True, help="영상 주제")
@click.option("--dry-run", is_flag=True, default=False, help="API 호출 없이 테스트 실행")
@click.option("--verbose", "-v", is_flag=True, default=False, help="상세 로그")
def produce_cmd(channel: str, topic: str, dry_run: bool, verbose: bool) -> None:
    """새 영상 제작 시작."""
    _setup_logging(verbose)

    # 채널 검증
    available = list_channels()
    if channel not in available:
        console.print(f"[red]Error: 채널 '{channel}' 없음. 사용 가능: {available}[/red]")
        sys.exit(1)

    ch = load_channel(channel)
    console.print(f"[bold green]📺 채널:[/bold green] {ch.channel_name}")
    console.print(f"[bold green]📝 주제:[/bold green] {topic}")
    console.print(f"[bold green]🧪 Dry Run:[/bold green] {dry_run}")
    console.print()

    sm = StateManager()
    ct = CostTracker(state_manager=sm)
    orch = Orchestrator(state_manager=sm, cost_tracker=ct, dry_run=dry_run)

    try:
        prod_id = asyncio.run(orch.produce(channel_id=channel, topic=topic))
        total = ct.get_production_cost(prod_id)
        console.print()
        console.print(f"[bold green]✅ 완료![/bold green] ID: {prod_id}")
        console.print(f"[bold green]💰 총 비용:[/bold green] ${total:.4f}")
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ 중단됨[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]❌ 실패: {e}[/red]")
        sys.exit(1)


@click.command("resume")
@click.argument("production_id")
@click.option("--dry-run", is_flag=True, default=False, help="API 호출 없이 테스트 실행")
@click.option("--verbose", "-v", is_flag=True, default=False, help="상세 로그")
def resume_cmd(production_id: str, dry_run: bool, verbose: bool) -> None:
    """실패한 프로덕션 재개."""
    _setup_logging(verbose)
    console.print(f"[bold yellow]🔄 재개:[/bold yellow] {production_id}")

    sm = StateManager()
    ct = CostTracker(state_manager=sm)
    orch = Orchestrator(state_manager=sm, cost_tracker=ct, dry_run=dry_run)

    try:
        asyncio.run(orch.resume(production_id))
        total = ct.get_production_cost(production_id)
        console.print(f"\n[bold green]✅ 재개 완료![/bold green] 총 비용: ${total:.4f}")
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ 중단됨[/yellow]")
        sys.exit(130)
    except Exception as e:
        console.print(f"\n[red]❌ 실패: {e}[/red]")
        sys.exit(1)


@click.command("status")
@click.option("--channel", "-c", default=None, help="채널 필터")
def status_cmd(channel: str | None) -> None:
    """프로덕션 상태 조회."""
    sm = StateManager()
    prods = sm.list_productions(channel_id=channel)

    if not prods:
        console.print("[dim]프로덕션이 없습니다.[/dim]")
        return

    table = Table(title="Productions")
    table.add_column("ID", style="cyan")
    table.add_column("Channel")
    table.add_column("Topic")
    table.add_column("Stage")
    table.add_column("Status")
    table.add_column("Updated")

    for p in prods:
        status_style = {
            "completed": "green",
            "failed": "red",
            "running": "yellow",
            "pending": "dim",
        }.get(p["status"], "white")

        table.add_row(
            p["production_id"],
            p["channel_id"],
            p["topic"][:30],
            p["current_stage"],
            f"[{status_style}]{p['status']}[/{status_style}]",
            p["updated_at"][:19],
        )

    console.print(table)
