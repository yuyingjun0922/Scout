"""
infra/data_adapters/gov_cn.py — D1 国务院政策采集器

数据源：gov.cn 搜索 API（政策文件库）
    https://sousuo.www.gov.cn/search-gov/data?t=zhengcelibrary&q=...

响应结构（侦察后确认）：
    {
      "code": 200,
      "searchVO": {
        "catMap": {
          "gongwen":   {"listVO": [...]},   # 国务院/办公厅级公文（最权威）
          "bumenfile": {"listVO": [...]},   # 部门文件（发改委/工信部等）
          "gongbao":   {"listVO": [...]},   # 公报（党中央+国务院联合文件）
          "otherfile": {"listVO": [...]},   # 解读/新闻（默认不收）
        }
      }
    }

    每条 item 关键字段：
        title       文件标题
        pcode       文号（国办函〔2026〕14号 / 财办建〔2026〕14号 等；otherfile/gongbao 可能空）
        pubtimeStr  "YYYY.MM.DD" 显示格式
        pubtime     发布时间 ms 时间戳（权威）
        ptime       成文时间 ms 时间戳（可能为 0，回退到 pubtime）
        puborg      发文机关（权威字段；otherfile/gongbao 可能空）
        url         gov.cn 政策正文 URL
        summary     前 200-500 字摘要
        childtype   主题分类如 "民政、扶贫、救灾\\其他"
        id          站内 ID

Phase 1 策略：
    - 多关键词并行查询
    - 按 pcode(文号) → url → title+date 三级去重
    - 同文件多关键词命中时，keyword_hits 合并
    - policy_direction 一律填 None（留给方向判断 Agent 交叉验证）
"""
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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


# ═══ 内部数据结构 ═══

@dataclass
class _PolicyData:
    """原始 API item → InfoUnitV1 的中间形态。"""
    title: str
    doc_number: Optional[str]
    publisher: Optional[str]
    published_date: str            # 'YYYY-MM-DD'
    issued_date: str               # 'YYYY-MM-DD'
    pubtime_ms: int                # 原始毫秒时间戳（UTC）
    url: Optional[str]
    summary: str
    subject: Optional[str]
    source_category: str           # gongwen/bumenfile/gongbao/otherfile
    keyword_hits: List[str] = field(default_factory=list)
    industries: List[str] = field(default_factory=list)

    def dedupe_key(self) -> str:
        """文号优先 > url > 标题+日期"""
        if self.doc_number:
            return f"doc:{self.doc_number.strip()}"
        if self.url:
            return f"url:{self.url.strip().lower()}"
        return f"td:{self.title[:80].strip()}:{self.published_date}"


# ═══ GovCNCollector ═══

class GovCNCollector(Collector, BaseAgent):
    """D1 国务院 / gov.cn 政策采集器"""

    SOURCE_CODE = "D1"
    CREDIBILITY = "权威"

    DEFAULT_KEYWORDS: List[str] = [
        "半导体", "集成电路", "芯片",
        "新能源汽车", "动力电池",
        "人工智能", "算力",
        "HBM", "存储器",
        "光伏", "风电",
    ]

    # 关键词→行业标签（命中后 related_industries 并集）
    KEYWORD_TO_INDUSTRIES: Dict[str, List[str]] = {
        "半导体": ["半导体设备", "AI算力"],
        "集成电路": ["半导体设备"],
        "芯片": ["半导体设备", "AI算力"],
        "新能源汽车": ["新能源车"],
        "动力电池": ["新能源车", "固态电池"],
        "人工智能": ["AI算力"],
        "算力": ["AI算力"],
        "HBM": ["HBM"],
        "存储器": ["HBM"],
        "光伏": ["光伏"],
        "风电": ["风电"],
    }

    # 默认采集类别（policy 性质强的）；otherfile(解读/新闻) 默认不收
    DEFAULT_CATEGORIES: List[str] = ["gongwen", "bumenfile", "gongbao"]
    ALL_CATEGORIES: List[str] = ["gongwen", "bumenfile", "gongbao", "otherfile"]

    # 文号前缀 → 发文机关（puborg 缺失时兜底；长前缀优先）
    _PCODE_PUBLISHER_MAP = [
        ("国办发明电", "国务院办公厅"),
        ("国办发", "国务院办公厅"),
        ("国办函", "国务院办公厅"),
        ("国发明电", "国务院"),
        ("国发", "国务院"),
        ("国函", "国务院"),
        ("国令", "国务院"),
        ("工信部令", "工业和信息化部"),
        ("工信部", "工业和信息化部"),
        ("发改", "国家发展和改革委员会"),
        ("财办", "财政部办公厅"),
        ("财", "财政部"),
        ("商务部", "商务部"),
        ("人社部", "人力资源和社会保障部"),
    ]

    SEARCH_ENDPOINT = "https://sousuo.www.gov.cn/search-gov/data"

    MIN_INTERVAL_SECONDS: float = 1.5
    MAX_RESULTS_PER_QUERY: int = 20
    HTTP_TIMEOUT: float = 30.0
    SUMMARY_MAX_LEN: int = 500

    def __init__(
        self,
        db: DatabaseManager,
        keywords: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        name: str = "govcn_d1",
    ):
        Collector.__init__(self, db=db)
        BaseAgent.__init__(self, name=name, db=db)

        self.keywords = (
            list(self.DEFAULT_KEYWORDS) if keywords is None else list(keywords)
        )
        self.categories = (
            list(self.DEFAULT_CATEGORIES) if categories is None else list(categories)
        )
        unknown_cats = [c for c in self.categories if c not in self.ALL_CATEGORIES]
        if unknown_cats:
            raise ValueError(
                f"Unknown categories: {unknown_cats}; valid: {self.ALL_CATEGORIES}"
            )
        self._last_call_time: float = 0.0

    # ── 主入口 ──

    def run(self, days: int = 7, keywords: Optional[List[str]] = None) -> int:
        units = self.collect_recent(days=days, keywords=keywords)
        return self.persist_batch(units) if units else 0

    def collect_recent(
        self,
        days: int = 7,
        keywords: Optional[List[str]] = None,
    ) -> List[InfoUnitV1]:
        """对每个关键词查询 gov.cn，按 pcode/url 合并去重。"""
        kws = list(keywords) if keywords is not None else list(self.keywords)
        if not kws:
            return []

        cutoff_ms = int(
            (datetime.now(tz=timezone.utc) - timedelta(days=days)).timestamp() * 1000
        )
        registry: Dict[str, _PolicyData] = {}

        for keyword in kws:
            items = self.run_with_error_handling(
                self._fetch_keyword, keyword
            ) or []
            for raw in items:
                policy = self._parse_item(raw)
                if policy is None:
                    continue
                if policy.pubtime_ms < cutoff_ms:
                    continue
                self._merge_policy(registry, policy, keyword)

        return [self._to_info_unit(p) for p in registry.values()]

    # ── HTTP + 解析 ──

    def _fetch_keyword(self, keyword: str) -> List[Dict[str, Any]]:
        """单关键词查询 → 扁平化多类别的 item 列表。"""
        self._rate_limit()

        params = {
            "t": "zhengcelibrary",
            "q": keyword,
            "sort": "pubtime",
            "sortType": 1,
            "p": 1,
            "n": self.MAX_RESULTS_PER_QUERY,
            "timetype": "timeqb",
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://sousuo.www.gov.cn/zhengceku/",
        }

        try:
            response = httpx.get(
                self.SEARCH_ENDPOINT,
                params=params,
                headers=headers,
                timeout=self.HTTP_TIMEOUT,
                follow_redirects=True,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
            raise NetworkError(f"gov.cn network: {type(e).__name__}: {e}") from e
        except httpx.HTTPError as e:
            raise ParseError(f"gov.cn request err: {type(e).__name__}: {e}") from e

        status = response.status_code
        if status == 429 or status >= 500:
            raise NetworkError(f"gov.cn HTTP {status} (retryable)")
        if status >= 400:
            raise ParseError(f"gov.cn HTTP {status}: {response.text[:300]}")

        try:
            payload = response.json()
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"gov.cn JSON decode err: {e}; raw={response.text[:300]}") from e

        # 展平 catMap 的几个类别
        search_vo = (payload or {}).get("searchVO") or {}
        cat_map = search_vo.get("catMap") or {}

        items: List[Dict[str, Any]] = []
        for cat in self.categories:
            cat_data = cat_map.get(cat) or {}
            list_vo = cat_data.get("listVO") or []
            for entry in list_vo:
                if isinstance(entry, dict):
                    entry["_source_category"] = cat  # 保留类别标记
                    items.append(entry)
        return items

    def _parse_item(self, raw: Dict[str, Any]) -> Optional[_PolicyData]:
        """一个 API item → _PolicyData；容错解析失败就 skip + 日志 warn。"""
        try:
            title = self._clean_text(raw.get("title") or "")
            if not title:
                return None

            pubtime_ms = int(raw.get("pubtime") or 0)
            if pubtime_ms <= 0:
                # 没有 pubtime ms，尝试从 pubtimeStr 解析
                pubtime_str = (raw.get("pubtimeStr") or "").strip()
                pubtime_ms = self._parse_pubtimestr_to_ms(pubtime_str)
                if pubtime_ms <= 0:
                    return None

            published_date = datetime.fromtimestamp(
                pubtime_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%d")

            # issued_date = ptime（成文）；0 时回退到 published
            ptime_ms = int(raw.get("ptime") or 0)
            if ptime_ms > 0:
                issued_date = datetime.fromtimestamp(
                    ptime_ms / 1000, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            else:
                issued_date = published_date

            pcode = (raw.get("pcode") or "").strip() or None
            url = (raw.get("url") or "").strip() or None
            puborg = (raw.get("puborg") or "").strip() or None
            publisher = puborg or self._infer_publisher_from_pcode(pcode)

            summary = self._clean_text(raw.get("summary") or "")[: self.SUMMARY_MAX_LEN]

            childtype = (raw.get("childtype") or "").strip()
            subject = childtype.split("\\")[0].strip() if childtype else None

            source_category = raw.get("_source_category") or "unknown"

            return _PolicyData(
                title=title,
                doc_number=pcode,
                publisher=publisher,
                published_date=published_date,
                issued_date=issued_date,
                pubtime_ms=pubtime_ms,
                url=url,
                summary=summary,
                subject=subject,
                source_category=source_category,
            )
        except Exception as e:  # noqa: BLE001
            self.logger.warning(
                f"gov.cn item parse skipped ({type(e).__name__}: {e}); "
                f"raw_keys={list(raw.keys())[:10]}"
            )
            return None

    # 块级 HTML 标签：转成空格（<br/>、<p>、</div> 等换行等价物）
    # 行内标签（<em>、<strong>）直接剔除，避免 "<em>芯片</em>" → "空格 芯片 空格"
    _BLOCK_HTML_RE = re.compile(
        r"</?(?:br|p|div|tr|li|h[1-6])\b[^>]*>",
        re.IGNORECASE,
    )
    _ANY_HTML_RE = re.compile(r"<[^>]+>")

    @classmethod
    def _clean_text(cls, s: str) -> str:
        """剥 HTML 标签 + 规整空白。块级标签→空格，行内标签→删除。"""
        if not s:
            return s
        s = cls._BLOCK_HTML_RE.sub(" ", s)
        s = cls._ANY_HTML_RE.sub("", s)
        s = re.sub(r"\s+", " ", s)
        return s.strip()

    @staticmethod
    def _parse_pubtimestr_to_ms(s: str) -> int:
        """'2026.04.17' / '2026-04-17' → UTC ms；失败返 0"""
        if not s:
            return 0
        m = re.match(r"(\d{4})[\.\-/](\d{1,2})[\.\-/](\d{1,2})", s)
        if not m:
            return 0
        try:
            y, mo, d = (int(x) for x in m.groups())
            dt = datetime(y, mo, d, tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except (ValueError, OverflowError):
            return 0

    @classmethod
    def _infer_publisher_from_pcode(cls, pcode: Optional[str]) -> Optional[str]:
        if not pcode:
            return None
        for prefix, publisher in cls._PCODE_PUBLISHER_MAP:
            if pcode.startswith(prefix):
                return publisher
        return None

    # ── 合并 ──

    def _merge_policy(
        self,
        registry: Dict[str, _PolicyData],
        policy: _PolicyData,
        keyword: str,
    ) -> None:
        key = policy.dedupe_key()
        new_inds = self.KEYWORD_TO_INDUSTRIES.get(keyword, [])
        if key in registry:
            existing = registry[key]
            if keyword not in existing.keyword_hits:
                existing.keyword_hits.append(keyword)
            for ind in new_inds:
                if ind not in existing.industries:
                    existing.industries.append(ind)
        else:
            policy.keyword_hits = [keyword]
            policy.industries = list(new_inds)
            registry[key] = policy

    # ── InfoUnitV1 构造 ──

    def _to_info_unit(self, p: _PolicyData) -> InfoUnitV1:
        identifier = p.doc_number or p.url or p.title[:80]
        content = json.dumps(
            {
                "title": p.title,
                "publisher": p.publisher,
                "doc_number": p.doc_number,
                "issued_date": p.issued_date,
                "published_date": p.published_date,
                "subject": p.subject,
                "url": p.url,
                "summary": p.summary,
                "keyword_hits": p.keyword_hits,
                "source_category": p.source_category,
            },
            ensure_ascii=False,
        )
        timestamp = datetime.fromtimestamp(
            p.pubtime_ms / 1000, tz=timezone.utc
        ).isoformat()
        return InfoUnitV1(
            id=self._make_policy_id(identifier, p.published_date),
            source=self.SOURCE_CODE,
            source_credibility=self.CREDIBILITY,
            timestamp=timestamp,
            category="政策",
            content=content,
            related_industries=list(p.industries),
            policy_direction=None,  # Phase 1 留给方向判断 Agent
        )

    def _make_policy_id(self, identifier: str, published_date: str) -> str:
        """hash(D1 + identifier + published_date)；identifier = doc_number 或 url 或 标题"""
        return info_unit_id(self.SOURCE_CODE, identifier, published_date)

    # ── 限流 ──

    def _rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.MIN_INTERVAL_SECONDS:
            time.sleep(self.MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_time = time.time()
