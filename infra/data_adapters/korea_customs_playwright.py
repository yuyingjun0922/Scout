"""
infra/data_adapters/korea_customs_playwright.py — V3 Playwright 采集器 (v1.11)

Phase 2A 方案 A 实现。替换 `korea_customs.py` 的 HTTP fetcher。

为什么 HTTP 方案失败（见 korea_customs.py 设计文档）：
    https://unipass.customs.go.kr/ets/ → 重定向到 https://tradedata.go.kr/
    整站 JS 驱动，具体数据通过 ets_f_prccMenuLoad() 异步加载；直接 HTTP GET
    `/cts/hmpg/openETS0100037Q.do` 返回 "시스템 에러" 骨架页（无数据）。

本 adapter 的方法：
    1. Playwright 启动 headless Chromium，访问 https://tradedata.go.kr/cts/index.do
    2. 在 JS 上下文里调 `ets_f_prccMenuLoad('/cts/hmpg/openETS0100037Q.do', {menuId:'ETS_MNK_20101000'})`
       （等价于用户点击菜单 "품목/국가" 10대 수출입 품목）
    3. 等待 `#trade_table` DOM 就绪（默认查询自动执行：YTD top 10 HS2 出口排名）
    4. 点击 "조회" 按钮刷新数据，等 5 秒
    5. 从 `#trade_table` 读取行，过滤关注的 HS2 编码
    6. 按 HS2 → 关注 industry 映射生成 InfoUnitV1

返回的数据字段：
    - 기간 (period): "2026" 等，表示 YTD 年累计
    - HS코드 (hs2): 2 位 HS 分类
    - 품목명 (name_ko): 韩文品목 description
    - 중량 (weight_kg)
    - 금액 (export_usd_thousand): 出口金额 千美元
    - 무역수지 (trade_balance_thousand): 贸易差额 千美元

为激活 recommendation_agent.d4 维度（V1/V3/D4 近 90 天条数）：
    - HS2=85 (Electrical/electronics) → 半导体设备, AI算力, HBM
    - HS2=84 (Machinery, incl. semi mfg equip) → 半导体设备
    - HS2=90 (Optical/measurement instruments) → 半导体设备
    - HS2=87 (Vehicles) → 韩国电池
    - HS2=89 (Ships) → 造船海工
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

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
from utils.time_utils import now_utc


# ── HS2 → 关注行业映射 ──
# 只映射与 watchlist 中 industry_name 完全匹配的条目，才能让 d4 query 命中。
HS2_INDUSTRY_MAP: Dict[str, List[str]] = {
    "85": ["半导体设备", "AI算力", "HBM"],  # 전기기기 - Electrical/electronics
    "84": ["半导体设备"],                    # 기계 - Machinery (semi mfg equipment)
    "90": ["半导体设备"],                    # 광학/측정기기 - Optical/measurement
    "87": ["韩国电池"],                      # 차량 - Vehicles (proxy for EV battery export)
    "89": ["造船海工"],                      # 선박 - Ships
}


class KoreaCustomsPlaywrightCollector(Collector, BaseAgent):
    """V3 韩国关税厅 Playwright 采集器。

    调用 run() 会启动 headless Chromium 并采集最新 YTD 排名。产出
    InfoUnitV1 → `info_units` 表，source='V3'，category='宏观'。
    """

    SOURCE_CODE = "V3"
    CREDIBILITY = "权威"

    INDEX_URL = "https://tradedata.go.kr/cts/index.do"
    MENU_LOAD_JS = (
        "ets_f_prccMenuLoad("
        "'/cts/hmpg/openETS0100037Q.do', "
        "{menuId:'ETS_MNK_20101000'});"
    )

    # 防止对政府站过度请求。每次 run 启动新 browser context，间隔至少 6 秒。
    MIN_INTERVAL_SECONDS: float = 6.0

    # Playwright 等待时间（秒）
    WAIT_AFTER_INDEX: float = 2.0
    WAIT_AFTER_MENU: float = 5.0
    WAIT_AFTER_QUERY: float = 5.0

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    )
    LOCALE = "ko-KR"

    # Playwright navigation timeout (ms)
    NAV_TIMEOUT_MS: int = 30000

    def __init__(
        self,
        db: DatabaseManager,
        hs_filter: Optional[List[str]] = None,
        name: str = "korea_customs_v3",
    ):
        Collector.__init__(self, db=db)
        BaseAgent.__init__(self, name=name, db=db)
        # Default: 全部 HS2 都抓
        self.hs_filter = list(hs_filter) if hs_filter else list(HS2_INDUSTRY_MAP.keys())
        self._last_call_time: float = 0.0

    # ── BaseAgent 主入口 ──

    def run(self) -> int:
        """run_with_error_handling 包装的 _run 入口，返回新增条数。"""
        result = self.run_with_error_handling(self._run_impl)
        return result if isinstance(result, int) else 0

    def _run_impl(self) -> int:
        units = self.collect_recent()
        return self.persist_batch(units) if units else 0

    # ── Collector 接口 ──

    def collect_recent(self, days: int = 0) -> List[InfoUnitV1]:
        """采集 YTD 最新数据。`days` 参数保持 Collector 兼容；Playwright
        路径只拿 YTD top 10 快照，忽略 days。
        """
        self._rate_limit()
        rows = self._fetch_table_rows()
        if not rows:
            raise DataMissingError(
                "V3 Playwright: trade_table 空或无可解析行"
            )
        units = self._rows_to_units(rows)
        if not units:
            raise DataMissingError(
                f"V3 Playwright: {len(rows)} rows fetched but none matched "
                f"HS2 filter {self.hs_filter}"
            )
        return units

    # ── Playwright fetch ──

    def _fetch_table_rows(self) -> List[List[str]]:
        """启动 Chromium → 渲染页面 → 返回 #trade_table 的 rows 矩阵。

        每个 row = List[str]（已 trim 的 cell 文本）。Header 行会保留；
        row_to_units 自己识别并跳过。
        """
        try:
            from playwright.sync_api import sync_playwright
            from playwright.sync_api import TimeoutError as PWTimeout
        except ImportError as e:
            raise NetworkError(
                "V3 Playwright: playwright 未安装。执行 "
                "`python -m pip install playwright && python -m playwright install chromium`"
            ) from e

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    ctx = browser.new_context(
                        user_agent=self.USER_AGENT, locale=self.LOCALE
                    )
                    page = ctx.new_page()
                    page.goto(
                        self.INDEX_URL,
                        timeout=self.NAV_TIMEOUT_MS,
                        wait_until="networkidle",
                    )
                    time.sleep(self.WAIT_AFTER_INDEX)
                    page.evaluate(self.MENU_LOAD_JS)
                    time.sleep(self.WAIT_AFTER_MENU)

                    # 尝试点击 "조회" 确保最新查询。失败不致命，默认 auto-load
                    # 已经在 menu load 后触发过一次。
                    try:
                        page.click(
                            'button:has-text("조회"):visible', timeout=3000
                        )
                        time.sleep(self.WAIT_AFTER_QUERY)
                    except PWTimeout:
                        self.logger.debug("V3: '조회' button click timeout; 沿用 auto-load")

                    rows = page.eval_on_selector_all(
                        "#trade_table tr",
                        "els => els.map(tr => Array.from("
                        "tr.querySelectorAll('th,td'))"
                        ".map(c => (c.textContent||'').trim()))",
                    )
                    return rows if isinstance(rows, list) else []
                finally:
                    browser.close()
        except NetworkError:
            raise
        except Exception as e:  # noqa: BLE001
            # Playwright 的异常体系较杂（TargetClosedError、Error、TimeoutError 等）
            # 统一成 NetworkError 让 BaseAgent 按网络类重试
            raise NetworkError(
                f"V3 Playwright: {type(e).__name__}: {e}"
            ) from e

    # ── 解析（纯函数） ──

    def _rows_to_units(self, rows: List[List[str]]) -> List[InfoUnitV1]:
        """把原始 rows 转换成 InfoUnitV1 列表。

        表格结构（默认查询后）：
            row 0-1: 表头
            row 2+: [checkbox, 기간, HS코드, 품목명, 중량, 금액, 무역수지]
                其中 len(cells) == 7
        """
        units: List[InfoUnitV1] = []
        seen_keys: set = set()

        for cells in rows:
            # 必须 7 个单元格（checkbox + 6 数据列）
            if len(cells) != 7:
                continue
            period_raw = cells[1].strip()
            hs2_raw = cells[2].strip()
            # 过滤表头（"기간" 等非数字）
            if not period_raw.isdigit() or not hs2_raw.isdigit():
                continue
            if hs2_raw not in self.hs_filter:
                continue
            industries = HS2_INDUSTRY_MAP.get(hs2_raw)
            if not industries:
                continue
            try:
                weight_kg = self._parse_amount(cells[4])
                export_thousand = self._parse_amount(cells[5])
                balance_thousand = self._parse_amount(cells[6])
            except ValueError:
                self.logger.warning(
                    f"V3 parse amt failed for HS={hs2_raw} period={period_raw}: "
                    f"{cells[4:7]}"
                )
                continue

            item_name = cells[3].strip()[:200]

            # 去重：同 period+HS 同批次只要一条
            key = (period_raw, hs2_raw)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            content = json.dumps(
                {
                    "hs2": hs2_raw,
                    "hs_name_ko": item_name,
                    "period": period_raw,
                    "period_type": "YTD",
                    "export_usd_thousand": export_thousand,
                    "weight_kg": weight_kg,
                    "trade_balance_thousand": balance_thousand,
                    "source_note": "tradedata.go.kr/cts (관세청 10대 수출입 품목)",
                },
                ensure_ascii=False,
            )
            # v1.11: timestamp 使用当前 UTC（快照时间），方便 d4 的 90 天窗口查询
            # period 字段留在 content 里记录业务含义（YTD 年份）
            timestamp = now_utc()

            units.append(
                InfoUnitV1(
                    id=self._make_id(hs2_raw, period_raw),
                    source=self.SOURCE_CODE,
                    source_credibility=self.CREDIBILITY,
                    timestamp=timestamp,
                    category="宏观",
                    content=content,
                    related_industries=list(industries),
                )
            )
        return units

    @staticmethod
    def _parse_amount(raw: str) -> float:
        """'88,753,776' / '644,315.7' → float。空/破折号抛 ValueError。"""
        if raw is None:
            raise ValueError("empty amount")
        s = str(raw).strip().replace(",", "").replace(" ", "")
        if not s or s in ("-", "--"):
            raise ValueError("empty amount")
        return float(s)

    def _make_id(self, hs2: str, period: str) -> str:
        """id = hash(V3 + HS2 + period)。同月/年重跑产生相同 id → INSERT OR IGNORE。"""
        return info_unit_id(self.SOURCE_CODE, f"HS{hs2}", period)

    # ── 限流 ──

    def _rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self._last_call_time
        if self._last_call_time > 0 and elapsed < self.MIN_INTERVAL_SECONDS:
            time.sleep(self.MIN_INTERVAL_SECONDS - elapsed)
        self._last_call_time = time.time()
