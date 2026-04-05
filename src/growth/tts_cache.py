"""TTS 문장 해시 캐싱 - 비용 30-50% 절감.

캐시 키: hash(텍스트 + 보이스ID + 감정 파라미터)
동일 텍스트+설정이면 이전 생성 결과 재사용.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class TTSCache:
    """TTS 결과 캐싱 엔진.

    캐시 구조:
        data/tts_cache/
            {hash}.mp3     - 오디오 파일
            {hash}.meta    - 메타데이터 (텍스트, 설정, 생성일)
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        from ..core.config import DATA_DIR
        self.cache_dir = cache_dir or DATA_DIR / "tts_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stats = {"hits": 0, "misses": 0}

    @staticmethod
    def compute_key(
        text: str,
        voice_id: str = "",
        emotion: str = "",
        provider: str = "",
        **kwargs: Any,
    ) -> str:
        """캐시 키 생성.

        해시 조합: text + voice_id + emotion + provider
        """
        key_parts = f"{text}|{voice_id}|{emotion}|{provider}"
        for k, v in sorted(kwargs.items()):
            key_parts += f"|{k}={v}"
        return hashlib.sha256(key_parts.encode("utf-8")).hexdigest()[:32]

    def get(self, cache_key: str) -> Path | None:
        """캐시에서 오디오 파일 조회.

        Returns:
            캐시된 오디오 파일 경로, 없으면 None
        """
        audio_path = self.cache_dir / f"{cache_key}.mp3"
        meta_path = self.cache_dir / f"{cache_key}.meta"

        if audio_path.exists() and meta_path.exists():
            self._stats["hits"] += 1
            logger.debug(f"TTS cache hit: {cache_key}")
            return audio_path

        self._stats["misses"] += 1
        return None

    def put(
        self,
        cache_key: str,
        audio_path: Path,
        text: str = "",
        voice_id: str = "",
        provider: str = "",
        **metadata: Any,
    ) -> Path:
        """캐시에 오디오 파일 저장.

        Args:
            cache_key: compute_key()로 생성한 키
            audio_path: 원본 오디오 파일 경로
            text: 원문 (메타데이터용)
            voice_id: 보이스 ID
            provider: TTS 프로바이더명
            **metadata: 추가 메타데이터

        Returns:
            캐시된 파일 경로
        """
        cached_path = self.cache_dir / f"{cache_key}.mp3"
        meta_path = self.cache_dir / f"{cache_key}.meta"

        # 파일 복사
        shutil.copy2(str(audio_path), str(cached_path))

        # 메타데이터 저장
        from datetime import datetime
        meta = {
            "cache_key": cache_key,
            "text_preview": text[:100],
            "text_length": len(text),
            "voice_id": voice_id,
            "provider": provider,
            "cached_at": datetime.now().isoformat(),
            "file_size": cached_path.stat().st_size,
            **metadata,
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        logger.debug(f"TTS cache put: {cache_key} ({cached_path.stat().st_size} bytes)")
        return cached_path

    def get_or_generate(
        self,
        text: str,
        voice_id: str,
        provider: str = "",
        emotion: str = "",
    ) -> tuple[Path | None, str]:
        """캐시 조회 후 키 반환.

        Returns:
            (cached_path_or_None, cache_key)
        """
        key = self.compute_key(
            text=text, voice_id=voice_id,
            emotion=emotion, provider=provider,
        )
        cached = self.get(key)
        return cached, key

    def stats(self) -> dict[str, Any]:
        """캐시 통계."""
        total = self._stats["hits"] + self._stats["misses"]
        hit_rate = (self._stats["hits"] / total * 100) if total > 0 else 0

        # 디스크 사용량
        cache_files = list(self.cache_dir.glob("*.mp3"))
        total_size = sum(f.stat().st_size for f in cache_files)

        return {
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "hit_rate": f"{hit_rate:.1f}%",
            "cached_entries": len(cache_files),
            "total_size_mb": round(total_size / (1024 * 1024), 2),
        }

    def clear(self, older_than_days: int | None = None) -> int:
        """캐시 정리.

        Args:
            older_than_days: N일 이상 된 항목만 삭제 (None이면 전체)

        Returns:
            삭제된 항목 수
        """
        from datetime import datetime, timedelta

        deleted = 0
        cutoff = None
        if older_than_days is not None:
            cutoff = datetime.now() - timedelta(days=older_than_days)

        for meta_path in self.cache_dir.glob("*.meta"):
            should_delete = True

            if cutoff:
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    cached_at = datetime.fromisoformat(meta.get("cached_at", ""))
                    should_delete = cached_at < cutoff
                except Exception:
                    should_delete = True

            if should_delete:
                audio_path = meta_path.with_suffix(".mp3")
                meta_path.unlink(missing_ok=True)
                audio_path.unlink(missing_ok=True)
                deleted += 1

        logger.info(f"TTS cache cleared: {deleted} entries removed")
        return deleted
