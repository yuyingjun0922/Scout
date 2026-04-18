"""
infra/data_adapters/korea_customs.py — V3 韩国关税厅采集器

Phase 1 目标：通过 UniPass/tradedata.go.kr 的公开页面采集半导体类 HS 编码
月度进出口数据。

⚠️ 现实情况（2026-04 实测）：
    UniPass (https://unipass.customs.go.kr/ets/) → 重定向到 tradedata.go.kr
    该站整站 JavaScript 驱动，具体数据页通过 `ets_f_prccMenuLoad()` 等
    JS 函数异步加载；直接 HTTP GET 只拿到 ~1.5KB 的骨架错误页。

    本 adapter 的 HTTP fetcher 做了 best-effort 尝试（GET 根页建立 cookie + POST
    查询），但实际 Phase 1 无法拿到生产数据。有两条升级路径：

        Phase 2A 方案 A: 引入 playwright，headless 执行 JS 后抓渲染后的 DOM
        Phase 2A 方案 B: 申请 data.go.kr / API 并走官方 REST 接口（需韩国手机认证）

    在 2A 方案之前，此 adapter 的 _parse_trade_html 是**纯函数且可独立测试**
    的。只要未来 fetcher 能拿到合规 HTML，parser 立刻可用。若真实 HTML 结构
    与 fixture 不同，重写 _parse_trade_html 即可，契约不变。

设计：
    - _http_fetch_raw(hs_code) : 真实网络抓取（目前会失败，但尝试正确的握手）
    - _parse_trade_html(html, hs_code) : 纯解析（mock 可验证）
    - 两者分离 → tests 用 fixture HTML 校验 parser；real script 探测 fetcher
"""
import json
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup

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


class KoreaCustomsCollector(Collector, BaseAgent):
    """V3 韩国关税厅采集器（Korea Customs Service / UniPass）"""

    SOURCE_CODE = "V3"
    CREDIBILITY = "权威"

    # Phase 1 只采 3 个半导体相关 HS code
    HS_CODES_PHASE1: Dict[str, Dict[str, Any]] = {
        "8542": {
            "name_en": "Semiconductor ICs",
            "name_ko": "반도체 집적회로",
            "industries": ["半导体设备", "AI算力"],
        },
        "854232": {
            "name_en": "Memory DRAM/HBM",
            "name_ko": "메모리 반도체 (DRAM/HBM)",
            "industries": ["HBM", "半导体设备"],
        },
        "8541": {
            "name_en": "Semiconductor Devices/Diodes",
            "name_ko": "반도체 소자",
            "industries": ["半导体设备"],
        },
    }

    BASE_URL = "https://unipass.customs.go.kr/ets/"
    STATS_ENDPOINT_PATH = "/cts/hmpg/openETS0100037Q.do"  # Top 10 品목/국가

    MIN_INTERVAL_SECONDS: float = 2.0
    HTTP_TIMEOUT: float = 30.0

    # 启发式：短 HTML 且含 errortype 关键字 → 判定为错误骨架页
    ERROR_PAGE_LEN_THRESHOLD = 3000

    def __init__(
        self,
        db: DatabaseManager,
        hs_codes: Optional[List[str]] = None,
        name: str = "korea_customs_v3",
    ):
        Collector.__init__(self, db=db)
        BaseAgent.__init__(self, name=name, db=db)

        requested = list(hs_codes) if hs_codes else list(self.HS_CODES_PHASE1.keys())
        unknown = [h for h in requested if h not in self.HS_CODES_PHASE1]
        if unknown:
            raise ValueError(
                f"Unknown HS codes: {unknown}; "
                f"Phase 1 supports: {list(self.HS_CODES_PHASE1.keys())}"
            )
        self.hs_codes = requested
        self._last_call_time: float = 0.0

    # ── 主入口 ──

    def run(self, months: int = 6) -> int:
        units = self.collect_recent(months=months)
        return self.persist_batch(units) if units else 0

    def collect_recent(self, months: int = 6) -> List[InfoUnitV1]:
        """对每个 HS code 独立采集+错误隔离。"""
        result: List[InfoUnitV1] = []
        for hs_code in self.hs_codes:
            units = self.run_with_error_handling(
                self._collect_one_hs_code, hs_code, months
            )
            if units:
                result.extend(units)
        return result

    # ── 单 HS code ──

    def _collect_one_hs_code(self, hs_code: str, months: int) -> List[InfoUnitV1]:
        self._rate_limit()
        html = self._http_fetch_raw(hs_code)
        rows = self._parse_trade_html(html, hs_code)
        if not rows:
            raise DataMissingError(f"V3 {hs_code}: no parseable rows")
        rows_sorted = sorted(rows, key=lambda r: r["period"])
        recent = rows_sorted[-months:]
        return [self._row_to_unit(hs_code, r) for r in recent]

    # ── HTTP 抓取（best-effort；现实中多半失败） ──

    def _http_fetch_raw(self, hs_code: str) -> str:
        """HTTP 抓取 UniPass trade stats HTML。

        Phase 1 现实：UniPass 整站 JS 驱动，直接 GET/POST 拿到的是骨架错误页。
        这里尽力做了 "先 GET 根、再 POST 查询" 的握手尝试；若返回错误骨架页则
        抛 ParseError 让调用方感知。
        """
        try:
            with httpx.Client(
                timeout=self.HTTP_TIMEOUT,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Scout/0.1; +local-research)",
                    "Accept-Language": "ko,en;q=0.8",
                },
            ) as client:
                # 1) 建立 session（拿 cookie）
                try:
                    client.get(self.BASE_URL)
                except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
                    raise NetworkError(
                        f"V3 {hs_code} root GET: {type(e).__name__}: {e}"
                    ) from e

                # 2) POST 查询
                target = self._resolved_stats_url(client)
                try:
                    response = client.post(
                        target,
                        data={"hsCd": hs_code, "schFrYmd": "", "schToYmd": ""},
                    )
                except (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError) as e:
                    raise NetworkError(
                        f"V3 {hs_code} stats POST: {type(e).__name__}: {e}"
                    ) from e
                except httpx.HTTPError as e:
                    raise ParseError(
                        f"V3 {hs_code} stats HTTP err: {type(e).__name__}: {e}"
                    ) from e

                status = response.status_code
                if status == 429 or status >= 500:
                    raise NetworkError(f"V3 {hs_code}: HTTP {status}")
                if status >= 400:
                    raise ParseError(f"V3 {hs_code}: HTTP {status}")

                text = response.text
                if self._looks_like_error_page(text):
                    raise ParseError(
                        f"V3 {hs_code}: UniPass returned skeleton error page "
                        f"(len={len(text)}). JS-driven site; requires "
                        f"Playwright or data.go.kr API (see adapter docstring)."
                    )
                return text
        except (NetworkError, ParseError, DataMissingError):
            raise
        except Exception as e:  # noqa: BLE001
            # 兜底：任何预期外抛成 ParseError 让 BaseAgent 记录
            raise ParseError(f"V3 {hs_code} fetch err: {type(e).__name__}: {e}") from e

    def _resolved_stats_url(self, client: httpx.Client) -> str:
        """redirect 之后根在 tradedata.go.kr；stats path 相同"""
        # httpx 的 base URL 不同步到重定向后的域，这里手工拼
        return "https://tradedata.go.kr" + self.STATS_ENDPOINT_PATH

    @classmethod
    def _looks_like_error_page(cls, html: str) -> bool:
        if len(html) >= cls.ERROR_PAGE_LEN_THRESHOLD:
            return False
        lower = html.lower()
        return "errortype" in lower or "errorsavedtoken" in lower

    # ── 解析（纯函数，可独立测试） ──

    def _parse_trade_html(self, html: str, hs_code: str) -> List[Dict[str, Any]]:
        """从 UniPass trade stats HTML 提取月度数据行。

        期望结构（见 tests/fixtures/korea_customs_sample.html）：
            <table class="monthly-data">
              <thead><tr>(기간/수출/수입/수량/동월대비)</tr></thead>
              <tbody><tr>...</tr> × N</tbody>
            </table>
            <table class="top-countries">
              <tbody><tr><td>국가</td><td>금액</td></tr>...</tbody>
            </table>

        Returns:
            List[dict]: 每月一个 dict，keys:
                period / export_usd / import_usd / export_qty /
                yoy_export_pct / top_countries
            top_countries 仅最新月份的那条非空（其它月为 []）。
        """
        soup = BeautifulSoup(html, "html.parser")

        monthly = soup.find("table", class_="monthly-data") or soup.find("table")
        if not monthly:
            raise ParseError(
                f"V3 {hs_code}: no <table> in HTML (len={len(html)})"
            )

        tbody = monthly.find("tbody") or monthly
        rows: List[Dict[str, Any]] = []
        for tr in tbody.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            try:
                period = self._normalize_period(cells[0])
                if not period:
                    continue
                export_usd = self._parse_amount(cells[1])
                import_usd = self._parse_amount(cells[2])
                qty = self._parse_amount(cells[3])
                yoy_pct = self._parse_pct(cells[4])
            except (ValueError, TypeError):
                continue
            rows.append({
                "period": period,
                "export_usd": export_usd,
                "import_usd": import_usd,
                "export_qty": qty,
                "yoy_export_pct": yoy_pct,
                "top_countries": [],
            })

        if not rows:
            # 保留少量 raw 以便排查（不是 DataMissing；表格在但没能解析）
            raw_snippet = str(monthly)[:500]
            raise ParseError(
                f"V3 {hs_code}: table present but zero rows parsed; "
                f"raw_sample={raw_snippet!r}"
            )

        # top countries 表（可选）
        top_table = soup.find("table", class_="top-countries")
        top_countries: List[Dict[str, Any]] = []
        if top_table:
            for tr in top_table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) >= 2 and cells[0]:
                    try:
                        amount = self._parse_amount(cells[1])
                    except (ValueError, TypeError):
                        continue
                    top_countries.append({
                        "country": cells[0],
                        "export_usd": amount,
                    })
            top_countries = top_countries[:5]

        # 只把最新月份的 row 填 top_countries（真实语义：site 通常是"当期 top 5"）
        if rows and top_countries:
            latest = max(rows, key=lambda r: r["period"])
            latest["top_countries"] = top_countries

        return rows

    # ── 格式规范化 ──

    @staticmethod
    def _normalize_period(raw: str) -> Optional[str]:
        """'2026.03' / '2026-03' / '202603' / '2026년 3월' → 'YYYY-MM'"""
        if not raw:
            return None
        s = str(raw).strip()
        # 纯数字 YYYYMM
        if re.fullmatch(r"\d{6}", s):
            return f"{s[:4]}-{s[4:]}"
        m = re.match(r"(\d{4})[\-\./년]?\s*(\d{1,2})", s)
        if m:
            year, month = m.groups()
            month_i = int(month)
            if 1 <= month_i <= 12:
                return f"{year}-{month_i:02d}"
        return None

    @staticmethod
    def _parse_amount(raw: str) -> float:
        """'12,345,678,901' / '1.2억' / '1,234' → float"""
        if raw is None:
            raise ValueError("empty amount")
        s = str(raw).strip().replace(",", "").replace(" ", "")
        if not s or s == "-":
            raise ValueError("empty amount")
        return float(s)

    @staticmethod
    def _parse_pct(raw: str) -> Optional[float]:
        """'+15.3%' / '-5.2%' / '12.5' → float，失败返 None"""
        if raw is None:
            return None
        s = str(raw).strip().replace("%", "").replace(" ", "").replace("+", "")
        if not s or s == "-":
            return None
        try:
            return float(s)
        except ValueError:
            return None

    # ── InfoUnitV1 构造 ──

    def _row_to_unit(self, hs_code: str, row: Dict[str, Any]) -> InfoUnitV1:
        meta = self.HS_CODES_PHASE1[hs_code]
        content = json.dumps(
            {
                "hs_code": hs_code,
                "hs_name_ko": meta["name_ko"],
                "hs_name_en": meta["name_en"],
                "period": row["period"],
                "export_usd": row["export_usd"],
                "import_usd": row["import_usd"],
                "export_qty": row["export_qty"],
                "top_countries": row["top_countries"],
                "yoy_export_pct": row["yoy_export_pct"],
            },
            ensure_ascii=False,
        )
        timestamp = f"{row['period']}-01T00:00:00+00:00"
        return InfoUnitV1(
            id=self._make_v3_id(hs_code, row["period"]),
            source=self.SOURCE_CODE,
            source_credibility=self.CREDIBILITY,
            timestamp=timestamp,
            category="宏观",
            content=content,
            related_industries=list(meta["industries"]),
        )

    def _make_v3_id(self, hs_code: str, period: str) -> str:
        """id = hash(V3 + hs_code + period)"""
        return info_unit_id(self.SOURCE_CODE, hs_code, period)

    # ── 限流 ──

    def _rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.MIN_INTERVAL_SECONDS:
            time.sleep(self.MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_time = time.time()
