"""
infra/data_adapters/arxiv_semantic.py — D4 双源论文采集器

数据源（并行查询、结果按 DOI/arXiv id 去重合并）：
    - arXiv API              : http://export.arxiv.org/api/query  (Atom XML)
    - Semantic Scholar API   : https://api.semanticscholar.org/graph/v1/paper/search  (JSON)

Phase 1 简化：
    - 行业→关键词映射硬编码（Phase 2A 走 industry_dict 表）
    - 关键词作为英文全文搜索，命中论文打上对应行业标签
    - 同一 DOI/arXiv id 跨源出现只保留一条，industries 合并

速率限制（两源独立计时）：
    - arXiv            : 官方要求 ≥3s 间隔
    - Semantic Scholar : 免费额度 100/5min，保守用 ≥3s
"""
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import feedparser
import httpx

from agents.base import (
    BaseAgent,
    DataMissingError,
    NetworkError,
    ParseError,
)
from contracts.contracts import InfoUnitV1
from infra.collector import Collector
from infra.db_manager import DatabaseManager
from utils.hash_utils import info_unit_id


# ═══ 中间数据结构 ═══

@dataclass
class _PaperData:
    """API 响应 → InfoUnitV1 的中间形态。允许 industries 在合并时原地扩充。"""
    title: str
    abstract: str
    authors: List[str]
    published_date: str            # 'YYYY-MM-DD'
    doi: Optional[str]
    arxiv_id: Optional[str]
    venue: Optional[str]
    citations: Optional[int]
    link: str
    source_api: str                # 'arxiv' / 'semantic_scholar'
    industries: List[str] = field(default_factory=list)

    def dedupe_key(self) -> str:
        """DOI 优先；退化到去掉版本后缀的 arXiv id；再退化到 title+date"""
        if self.doi:
            return f"doi:{self.doi.strip().lower()}"
        if self.arxiv_id:
            base = self.arxiv_id.split("v")[0] if "v" in self.arxiv_id else self.arxiv_id
            return f"arxiv:{base.strip()}"
        return f"td:{self.title[:80].strip().lower()}:{self.published_date}"


# ═══ PaperCollector ═══

class PaperCollector(Collector, BaseAgent):
    """D4 双源论文采集器"""

    SOURCE_CODE = "D4"
    CREDIBILITY = "参考"

    DEFAULT_KEYWORDS: Dict[str, List[str]] = {
        "半导体设备": ["semiconductor equipment", "lithography", "etching"],
        "HBM": ["HBM", "high bandwidth memory", "DRAM stacking"],
        "固态电池": ["solid state battery", "lithium electrolyte"],
        "AI算力": ["AI accelerator", "inference chip", "GPU training"],
        "新能源车": ["electric vehicle", "battery pack"],
    }

    # HTTPS 避免 301 重定向空响应
    ARXIV_ENDPOINT = "https://export.arxiv.org/api/query"
    S2_ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper/search"

    ARXIV_MIN_INTERVAL: float = 3.0      # 官方硬要求
    # 2026-04-19: 3.0s 踩 S2 免费档红线，实测 429 率 20-40%。
    # 提到 8.0s 后预期 <5%。根治需免费 API key（P1，x-api-key header → 10x 速率）。
    S2_MIN_INTERVAL: float = 8.0
    MAX_RESULTS_PER_QUERY: int = 20
    HTTP_TIMEOUT: float = 30.0

    # S2 返回字段（按需要裁剪，减小响应体）
    S2_FIELDS = "title,authors,abstract,venue,citationCount,externalIds,publicationDate,url"

    def __init__(
        self,
        db: DatabaseManager,
        industries_keywords: Optional[Dict[str, List[str]]] = None,
        name: str = "paper_d4",
    ):
        Collector.__init__(self, db=db)
        BaseAgent.__init__(self, name=name, db=db)
        # 注意：用 is None 判断，避免空 dict {} 被当 falsy 而回落到默认
        self.industries_keywords = (
            dict(self.DEFAULT_KEYWORDS)
            if industries_keywords is None
            else dict(industries_keywords)
        )
        self._arxiv_last_call: float = 0.0
        self._s2_last_call: float = 0.0

    # ── 主入口 ──

    def run(self, days: int = 7, industries: Optional[List[str]] = None) -> int:
        units = self.collect_recent(days=days, industries=industries)
        return self.persist_batch(units) if units else 0

    def collect_recent(
        self,
        days: int = 7,
        industries: Optional[List[str]] = None,
    ) -> List[InfoUnitV1]:
        """按行业→关键词矩阵查询两个 API，按 DOI/arXiv id 合并去重。"""
        # 按 industries 过滤关键词映射
        if industries:
            kw_map = {
                k: v for k, v in self.industries_keywords.items() if k in industries
            }
        else:
            kw_map = self.industries_keywords

        if not kw_map:
            return []

        # dedupe_key → _PaperData
        registry: Dict[str, _PaperData] = {}

        for industry, keywords in kw_map.items():
            for keyword in keywords:
                arxiv_papers = self.run_with_error_handling(
                    self._fetch_arxiv, keyword, days
                ) or []
                s2_papers = self.run_with_error_handling(
                    self._fetch_semantic_scholar, keyword, days
                ) or []
                for p in list(arxiv_papers) + list(s2_papers):
                    self._merge_paper(registry, p, industry)

        return [self._paper_to_info_unit(p) for p in registry.values()]

    # ── 合并 ──

    @staticmethod
    def _merge_paper(registry: Dict[str, _PaperData], paper: _PaperData, industry: str) -> None:
        key = paper.dedupe_key()
        if key in registry:
            existing = registry[key]
            if industry and industry not in existing.industries:
                existing.industries.append(industry)
            # 若已有条目无 citations 而新条目有，补上（S2 常有 citations，arXiv 无）
            if existing.citations is None and paper.citations is not None:
                existing.citations = paper.citations
            # 填 venue
            if not existing.venue and paper.venue:
                existing.venue = paper.venue
        else:
            if industry:
                paper.industries = [industry]
            registry[key] = paper

    # ── arXiv ──

    def _fetch_arxiv(self, keyword: str, days: int) -> List[_PaperData]:
        self._rate_limit_arxiv()
        params = {
            "search_query": f'all:"{keyword}"',
            "start": 0,
            "max_results": self.MAX_RESULTS_PER_QUERY,
            "sortBy": "lastUpdatedDate",   # 取最近"动态"而非最早投稿
            "sortOrder": "descending",
        }
        response = self._http_get(self.ARXIV_ENDPOINT, params, source="arXiv")
        xml_text = response.text

        feed = feedparser.parse(xml_text)
        if feed.bozo and getattr(feed, "bozo_exception", None):
            raise ParseError(
                f"arXiv XML parse err: {feed.bozo_exception}; raw={xml_text[:300]}"
            )

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        papers: List[_PaperData] = []
        for entry in getattr(feed, "entries", []):
            paper = self._parse_arxiv_entry(entry, cutoff)
            if paper is not None:
                papers.append(paper)
        return papers

    def _parse_arxiv_entry(self, entry, cutoff: datetime) -> Optional[_PaperData]:
        """单条 entry 容错解析：任何失败→skip（日志 warn），不让整批 fail"""
        try:
            title = entry.get("title", "").strip()
            entry_id = entry.get("id", "")
            if not title or not entry_id:
                return None

            # 首选 updated（反映最近活动），退化到 published；2024 投稿 + 2026 更新的论文
            # 只看 published 会被老化过滤掉
            ref_str = entry.get("updated", "") or entry.get("published", "")
            if not ref_str:
                return None
            ref_dt = datetime.fromisoformat(ref_str.replace("Z", "+00:00"))
            if ref_dt < cutoff:
                return None
            pub_dt = ref_dt  # 用于下面的日期字段

            arxiv_id = entry_id.rsplit("/", 1)[-1]

            # feedparser 把 arxiv:doi 解析成 arxiv_doi 字段
            doi = entry.get("arxiv_doi") or entry.get("doi")
            doi = doi.strip() if isinstance(doi, str) else None

            authors: List[str] = []
            for a in getattr(entry, "authors", []) or []:
                name = getattr(a, "name", None) or (a.get("name") if isinstance(a, dict) else None)
                if name:
                    authors.append(name)

            return _PaperData(
                title=title,
                abstract=entry.get("summary", "").strip(),
                authors=authors,
                published_date=pub_dt.strftime("%Y-%m-%d"),
                doi=doi,
                arxiv_id=arxiv_id,
                venue="arXiv",
                citations=None,
                link=entry_id,
                source_api="arxiv",
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"arXiv entry skipped: {type(e).__name__}: {e}")
            return None

    # ── Semantic Scholar ──

    def _fetch_semantic_scholar(self, keyword: str, days: int) -> List[_PaperData]:
        self._rate_limit_s2()
        params = {
            "query": keyword,
            "limit": self.MAX_RESULTS_PER_QUERY,
            "fields": self.S2_FIELDS,
        }
        response = self._http_get(self.S2_ENDPOINT, params, source="SemanticScholar")
        try:
            payload = response.json()
        except Exception as e:  # noqa: BLE001
            raise ParseError(
                f"S2 JSON parse err: {e}; raw={response.text[:300]}"
            ) from e

        if not isinstance(payload, dict):
            raise ParseError(f"S2 response not a dict: {type(payload).__name__}")

        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        papers: List[_PaperData] = []
        for item in payload.get("data", []) or []:
            paper = self._parse_s2_item(item, cutoff_date)
            if paper is not None:
                papers.append(paper)
        return papers

    def _parse_s2_item(self, item: dict, cutoff_date: date) -> Optional[_PaperData]:
        try:
            title = (item.get("title") or "").strip()
            if not title:
                return None

            pub_date_str = item.get("publicationDate")
            if not pub_date_str:
                # S2 有时只给 year。Phase 1 要求 published_date 精确到日，跳过。
                return None
            pub_date = datetime.strptime(pub_date_str, "%Y-%m-%d").date()
            if pub_date < cutoff_date:
                return None

            external_ids = item.get("externalIds") or {}
            doi = external_ids.get("DOI")
            doi = doi.strip() if isinstance(doi, str) else None
            arxiv_id = external_ids.get("ArXiv")
            arxiv_id = arxiv_id.strip() if isinstance(arxiv_id, str) else None

            authors = [
                a.get("name", "")
                for a in (item.get("authors") or [])
                if isinstance(a, dict) and a.get("name")
            ]

            return _PaperData(
                title=title,
                abstract=(item.get("abstract") or "").strip(),
                authors=authors,
                published_date=pub_date.isoformat(),
                doi=doi,
                arxiv_id=arxiv_id,
                venue=item.get("venue") or None,
                citations=item.get("citationCount"),
                link=item.get("url") or "",
                source_api="semantic_scholar",
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(f"S2 item skipped: {type(e).__name__}: {e}")
            return None

    # ── HTTP ──

    def _http_get(self, url: str, params: dict, source: str):
        """统一 HTTP GET，按 httpx 异常树 + 状态码分类错误。

        follow_redirects=True 防 3xx 空响应（arXiv 把 http→https 会 301）。
        """
        try:
            response = httpx.get(
                url,
                params=params,
                timeout=self.HTTP_TIMEOUT,
                follow_redirects=True,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
            raise NetworkError(
                f"{source} network: {type(e).__name__}: {e}"
            ) from e
        except httpx.HTTPError as e:
            # 其它 httpx 错误（InvalidURL 等）归 parse
            raise ParseError(f"{source} request err: {type(e).__name__}: {e}") from e

        status = response.status_code
        if status == 429 or status >= 500:
            raise NetworkError(f"{source} HTTP {status} (retryable)")
        if status >= 400:
            raise ParseError(f"{source} HTTP {status}: {response.text[:300]}")
        return response

    # ── 速率限制 ──

    def _rate_limit_arxiv(self) -> None:
        self._rate_limit_generic("arxiv")

    def _rate_limit_s2(self) -> None:
        self._rate_limit_generic("s2")

    def _rate_limit_generic(self, source: str) -> None:
        if source == "arxiv":
            last = self._arxiv_last_call
            interval = self.ARXIV_MIN_INTERVAL
        else:
            last = self._s2_last_call
            interval = self.S2_MIN_INTERVAL

        now = time.time()
        elapsed = now - last
        if last > 0 and elapsed < interval:
            time.sleep(interval - elapsed)

        if source == "arxiv":
            self._arxiv_last_call = time.time()
        else:
            self._s2_last_call = time.time()

    # ── InfoUnitV1 构造 ──

    def _paper_to_info_unit(self, p: _PaperData) -> InfoUnitV1:
        identifier = p.doi or p.arxiv_id or p.title[:80]
        timestamp = f"{p.published_date}T00:00:00+00:00"
        content = json.dumps(
            {
                "title": p.title,
                "authors": p.authors,
                "abstract": p.abstract[:2000],  # 截断防 DB 膨胀
                "venue": p.venue,
                "citations": p.citations,
                "link": p.link,
                "doi": p.doi,
                "arxiv_id": p.arxiv_id,
                "source_api": p.source_api,
            },
            ensure_ascii=False,
        )
        return InfoUnitV1(
            id=self._make_paper_id(identifier, p.published_date),
            source=self.SOURCE_CODE,
            source_credibility=self.CREDIBILITY,
            timestamp=timestamp,
            category="科研",
            content=content,
            related_industries=list(p.industries),
        )

    def _make_paper_id(self, identifier: str, published_date: str) -> str:
        """论文专用 ID：hash(D4 + DOI/arXiv_id + 日期)"""
        return info_unit_id(self.SOURCE_CODE, identifier, published_date)
