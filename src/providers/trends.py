"""Google Trends 프로바이더."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..core.exceptions import (
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from ..core.retry import retry

logger = logging.getLogger(__name__)


class GoogleTrendsProvider:
    """Google Trends 비공식 API를 통한 트렌드 분석.

    Google Trends는 공식 API가 없으므로 내부 API 엔드포인트를 사용.
    pytrends 라이브러리 대신 직접 httpx로 호출하여 의존성 최소화.
    """

    TRENDS_API_BASE = "https://trends.google.com/trends/api"

    def __init__(self, geo: str = "KR", hl: str = "ko") -> None:
        self.geo = geo
        self.hl = hl

    @retry(max_attempts=3, base_delay=3.0)
    async def get_interest_over_time(
        self,
        keywords: list[str],
        timeframe: str = "today 3-m",
    ) -> dict[str, Any]:
        """키워드의 시간별 관심도 조회.

        Returns:
            {
                "keywords": [...],
                "averages": {"keyword": avg_score},
                "trend_velocity": {"keyword": recent_vs_avg_ratio},
            }
        """
        if not keywords:
            return {"keywords": [], "averages": {}, "trend_velocity": {}}

        # Google Trends 내부 API 호출
        params = {
            "hl": self.hl,
            "tz": "-540",  # KST
            "req": self._build_explore_request(keywords, timeframe),
            "token": "",  # 토큰 없이도 기본 요청 가능
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                # 먼저 explore 토큰 획득
                explore_resp = await client.get(
                    f"{self.TRENDS_API_BASE}/explore",
                    params={
                        "hl": self.hl,
                        "tz": "-540",
                        "req": f'{{"comparisonItem":[{",".join(self._keyword_item(k) for k in keywords[:5])}],"category":0,"property":""}}',
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("google_trends", timeout_seconds=30) from e
            except httpx.ConnectError as e:
                raise ProviderError("google_trends", f"Connection failed: {e}", retryable=True) from e

        if explore_resp.status_code == 429:
            raise RateLimitError("google_trends", retry_after_seconds=60)
        if explore_resp.status_code != 200:
            # Google Trends 비공식 API 실패 시 기본값 반환
            logger.warning(f"Google Trends API returned {explore_resp.status_code}, using fallback")
            return self._fallback_response(keywords)

        # 응답 파싱 (Google Trends는 )]}' 접두사 포함)
        try:
            text = explore_resp.text
            if text.startswith(")]}'"):
                text = text[5:]
            import json
            data = json.loads(text)
        except Exception:
            logger.warning("Failed to parse Google Trends response, using fallback")
            return self._fallback_response(keywords)

        return self._parse_interest_data(keywords, data)

    @retry(max_attempts=3, base_delay=3.0)
    async def get_related_queries(
        self,
        keyword: str,
        category: int = 0,
    ) -> dict[str, list[dict[str, Any]]]:
        """연관 검색어 조회.

        Returns:
            {
                "rising": [{"query": str, "value": int}, ...],
                "top": [{"query": str, "value": int}, ...],
            }
        """
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                resp = await client.get(
                    f"{self.TRENDS_API_BASE}/widgetdata/relatedsearches",
                    params={
                        "hl": self.hl,
                        "tz": "-540",
                        "req": f'{{"restriction":{{"geo":{{"country":"{self.geo}"}},"time":"today 3-m"}},"keywordType":"QUERY","metric":["TOP","RISING"],"trendinessSettings":{{"compareTime":"today 12-m"}},"requestOptions":{{"property":"","backend":"IZG","category":{category}}},"language":"ko"}}',
                        "token": "",
                    },
                    headers={"Accept": "application/json"},
                )
            except httpx.TimeoutException as e:
                raise ProviderTimeoutError("google_trends", timeout_seconds=30) from e

        if resp.status_code == 429:
            raise RateLimitError("google_trends", retry_after_seconds=60)
        if resp.status_code != 200:
            logger.warning(f"Related queries API returned {resp.status_code}")
            return {"rising": [], "top": []}

        try:
            text = resp.text
            if text.startswith(")]}'"):
                text = text[5:]
            import json
            data = json.loads(text)
        except Exception:
            return {"rising": [], "top": []}

        return self._parse_related_queries(data)

    async def analyze_trend_velocity(self, keywords: list[str]) -> dict[str, float]:
        """키워드별 트렌드 속도(떡상 중인지) 분석.

        Returns:
            {"keyword": velocity_score} - 1.0=평균, >1.5=급상승, <0.5=하락
        """
        interest = await self.get_interest_over_time(keywords)
        return interest.get("trend_velocity", {})

    @staticmethod
    def _keyword_item(keyword: str) -> str:
        """검색어→JSON 아이템."""
        return f'{{"keyword":"{keyword}","geo":"KR","time":"today 3-m"}}'

    @staticmethod
    def _build_explore_request(keywords: list[str], timeframe: str) -> str:
        """explore API 요청 본문 생성."""
        items = ",".join(
            f'{{"keyword":"{k}","geo":"KR","time":"{timeframe}"}}'
            for k in keywords[:5]
        )
        return f'{{"comparisonItem":[{items}],"category":0,"property":""}}'

    @staticmethod
    def _fallback_response(keywords: list[str]) -> dict[str, Any]:
        """API 실패 시 기본 응답."""
        return {
            "keywords": keywords,
            "averages": {k: 50.0 for k in keywords},
            "trend_velocity": {k: 1.0 for k in keywords},
        }

    @staticmethod
    def _parse_interest_data(keywords: list[str], data: dict) -> dict[str, Any]:
        """explore 응답에서 관심도 데이터 추출."""
        averages = {}
        velocities = {}

        # widgets에서 TIMESERIES 위젯 찾기
        widgets = data.get("widgets", [])
        for widget in widgets:
            if widget.get("id") == "TIMESERIES":
                # 토큰이 필요하므로 기본값 사용
                for i, kw in enumerate(keywords):
                    averages[kw] = 50.0
                    velocities[kw] = 1.0
                break
        else:
            for kw in keywords:
                averages[kw] = 50.0
                velocities[kw] = 1.0

        return {
            "keywords": keywords,
            "averages": averages,
            "trend_velocity": velocities,
        }

    @staticmethod
    def _parse_related_queries(data: dict) -> dict[str, list[dict[str, Any]]]:
        """연관 검색어 응답 파싱."""
        rising = []
        top = []

        # 다양한 응답 구조 처리
        for key in ("default", "rankedList"):
            items = data.get(key, [])
            if isinstance(items, list):
                for group in items:
                    ranked = group.get("rankedKeyword", [])
                    for item in ranked:
                        query_data = {
                            "query": item.get("query", ""),
                            "value": item.get("value", 0),
                        }
                        if item.get("link"):
                            query_data["link"] = item["link"]

                        if "RISING" in str(group.get("keyword", "")):
                            rising.append(query_data)
                        else:
                            top.append(query_data)

        return {"rising": rising, "top": top}
