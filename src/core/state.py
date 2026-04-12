"""SQLite 상태 관리 - Resume 기능의 핵심."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

from .config import PROJECT_ROOT
from .models import (
    VideoProduction,
    Stage,
    ProductionStatus,
    CostEntry,
)


class StateManager:
    """프로덕션 상태를 SQLite로 관리. Resume-from-failure의 핵심."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else PROJECT_ROOT / "data" / "db" / "production.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """DB 스키마 초기화."""
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS productions (
                    production_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    topic TEXT DEFAULT '',
                    current_stage TEXT DEFAULT 'benchmark',
                    status TEXT DEFAULT 'pending',
                    error_message TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_productions_channel
                    ON productions(channel_id);
                CREATE INDEX IF NOT EXISTS idx_productions_status
                    ON productions(status);

                CREATE TABLE IF NOT EXISTS stage_outputs (
                    production_id TEXT NOT NULL,
                    stage_name TEXT NOT NULL,
                    output_json TEXT NOT NULL,
                    output_dir TEXT DEFAULT '',
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (production_id, stage_name),
                    FOREIGN KEY (production_id) REFERENCES productions(production_id)
                );

                CREATE TABLE IF NOT EXISTS cost_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    production_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    operation TEXT DEFAULT '',
                    units REAL DEFAULT 0,
                    unit_cost REAL DEFAULT 0,
                    total_cost REAL DEFAULT 0,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (production_id) REFERENCES productions(production_id)
                );

                CREATE INDEX IF NOT EXISTS idx_costs_production
                    ON cost_entries(production_id);

                CREATE TABLE IF NOT EXISTS stage_timings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    production_id TEXT NOT NULL,
                    stage_name TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    error TEXT DEFAULT '',
                    FOREIGN KEY (production_id) REFERENCES productions(production_id)
                );

                CREATE INDEX IF NOT EXISTS idx_stage_timings_production
                    ON stage_timings(production_id);
            """)

    @contextmanager
    def _connect(self):
        """SQLite 연결 컨텍스트 매니저."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_production(self, production: VideoProduction) -> None:
        """새 프로덕션 생성."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO productions
                   (production_id, channel_id, topic, current_stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    production.production_id,
                    production.channel_id,
                    production.topic,
                    production.current_stage.value,
                    production.status.value,
                    now,
                    now,
                ),
            )
        # 프로덕션 디렉토리 생성
        prod_dir = production.production_dir
        prod_dir.mkdir(parents=True, exist_ok=True)
        for stage in Stage:
            (prod_dir / stage.value).mkdir(exist_ok=True)
        self._write_state_json(production)

    def save_stage_output(
        self, production_id: str, stage: Stage, output_data: dict
    ) -> None:
        """스테이지 결과 저장."""
        now = datetime.now().isoformat()
        output_json = json.dumps(output_data, ensure_ascii=False, default=str)
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO stage_outputs
                   (production_id, stage_name, output_json, completed_at)
                   VALUES (?, ?, ?, ?)""",
                (production_id, stage.value, output_json, now),
            )

    def load_stage_output(self, production_id: str, stage: Stage) -> dict | None:
        """스테이지 결과 로드."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT output_json FROM stage_outputs WHERE production_id=? AND stage_name=?",
                (production_id, stage.value),
            ).fetchone()
        if row:
            return json.loads(row["output_json"])
        return None

    def advance_stage(
        self, production_id: str, stage: Stage, status: ProductionStatus = ProductionStatus.RUNNING
    ) -> None:
        """스테이지 진행."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE productions SET current_stage=?, status=?, updated_at=? WHERE production_id=?",
                (stage.value, status.value, now, production_id),
            )
        self._update_state_json(production_id)

    def mark_failed(self, production_id: str, error: str) -> None:
        """프로덕션 실패 기록."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE productions SET status=?, error_message=?, updated_at=? WHERE production_id=?",
                (ProductionStatus.FAILED.value, error, now, production_id),
            )
        self._update_state_json(production_id)

    def mark_completed(self, production_id: str) -> None:
        """프로덕션 완료."""
        now = datetime.now().isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE productions SET status=?, updated_at=? WHERE production_id=?",
                (ProductionStatus.COMPLETED.value, now, production_id),
            )
        self._update_state_json(production_id)

    def get_production(self, production_id: str) -> dict | None:
        """프로덕션 조회."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM productions WHERE production_id=?",
                (production_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_resumable(self, channel_id: str | None = None) -> list[dict]:
        """재개 가능한 프로덕션 목록 (failed 또는 running)."""
        query = "SELECT * FROM productions WHERE status IN ('failed', 'running')"
        params: list = []
        if channel_id:
            query += " AND channel_id=?"
            params.append(channel_id)
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def list_productions(
        self, channel_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        """프로덕션 목록 조회."""
        query = "SELECT * FROM productions"
        params: list = []
        if channel_id:
            query += " WHERE channel_id=?"
            params.append(channel_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def record_cost(self, entry: CostEntry, production_id: str) -> None:
        """비용 기록."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO cost_entries
                   (production_id, stage, provider, operation, units, unit_cost, total_cost, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    production_id,
                    entry.stage.value,
                    entry.provider,
                    entry.operation,
                    entry.units,
                    entry.unit_cost,
                    entry.total_cost,
                    entry.timestamp.isoformat(),
                ),
            )

    def get_costs(
        self, production_id: str | None = None, month: str | None = None
    ) -> list[dict]:
        """비용 조회."""
        query = "SELECT * FROM cost_entries WHERE 1=1"
        params: list = []
        if production_id:
            query += " AND production_id=?"
            params.append(production_id)
        if month:  # "2026-04" 형식
            query += " AND timestamp LIKE ?"
            params.append(f"{month}%")
        query += " ORDER BY timestamp DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_total_cost(
        self, production_id: str | None = None, month: str | None = None
    ) -> float:
        """총 비용."""
        query = "SELECT COALESCE(SUM(total_cost), 0) as total FROM cost_entries WHERE 1=1"
        params: list = []
        if production_id:
            query += " AND production_id=?"
            params.append(production_id)
        if month:
            query += " AND timestamp LIKE ?"
            params.append(f"{month}%")
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return float(row["total"]) if row else 0.0

    def record_stage_timing(
        self,
        production_id: str,
        stage_name: str,
        started_at: str,
        completed_at: str,
        duration_seconds: float,
        error: str = "",
    ) -> None:
        """스테이지 실행 시간 기록."""
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO stage_timings
                   (production_id, stage_name, started_at, completed_at, duration_seconds, error)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (production_id, stage_name, started_at, completed_at, duration_seconds, error),
            )

    def get_stage_timings(self, production_id: str) -> list[dict]:
        """스테이지별 실행 시간 조회."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stage_timings WHERE production_id=? ORDER BY id",
                (production_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _update_state_json(self, production_id: str) -> None:
        """SQLite 상태를 state.json에 동기화 (파이프라인 실패 방지를 위해 예외 무시)."""
        try:
            from .config import DATA_DIR
            prod = self.get_production(production_id)
            if prod is None:
                return
            state_path = DATA_DIR / "productions" / production_id / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_data = {
                "production_id": prod["production_id"],
                "channel_id": prod["channel_id"],
                "topic": prod["topic"],
                "current_stage": prod["current_stage"],
                "status": prod["status"],
                "updated_at": prod["updated_at"],
            }
            state_path.write_text(
                json.dumps(state_data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # state.json 동기화 실패가 파이프라인을 중단시키지 않도록 함
            pass

    def _write_state_json(self, production: VideoProduction) -> None:
        """프로덕션 디렉토리에 state.json 기록 (디버깅용)."""
        state_path = production.production_dir / "state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_data = {
            "production_id": production.production_id,
            "channel_id": production.channel_id,
            "topic": production.topic,
            "current_stage": production.current_stage.value,
            "status": production.status.value,
            "total_cost": production.total_cost,
            "updated_at": datetime.now().isoformat(),
        }
        state_path.write_text(
            json.dumps(state_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
