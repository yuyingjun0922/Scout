"""
Gate Agent - 信号筛选和评分模块
职责：对Scout采集的信号进行多维度评分，返回Top N高价值信号
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Union
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# 作为脚本运行（`python agents/gate_agent.py`）时，sys.path[0] 是 agents/，
# 项目根没有在 path 中。作为 package 导入（tests / 其它 agent）不触发。
if __name__ == "__main__":
    _project_root = str(Path(__file__).resolve().parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

from agents.base import BaseAgent
from infra.db_manager import DatabaseManager


class Authority(Enum):
    """发文机关权重"""
    STATE_COUNCIL = 1.0  # 国务院/国务院办公厅
    MINISTRY = 0.8      # 中央部委（工信部、发改委等）
    BUREAU = 0.7        # 行业专业部门（国家能源局等）
    LOCAL = 0.5         # 地方政府（市、省、书记会）
    ASSOCIATION = 0.3   # 行业协会


@dataclass
class SignalScore:
    """单条信号的评分结构"""
    info_unit_id: int
    source: str
    title: str
    publisher: str
    timestamp: str

    d1_score: float = 0.0      # 政策信号权重
    s4_score: float = 0.0      # 股票基本面
    d4_score: float = 0.0      # 科研趋势
    gate_score: float = 0.0    # 综合评分

    def to_dict(self) -> dict:
        return {
            'info_unit_id': self.info_unit_id,
            'source': self.source,
            'title': self.title,
            'publisher': self.publisher,
            'timestamp': self.timestamp,
            'd1_score': round(self.d1_score, 3),
            's4_score': round(self.s4_score, 3),
            'd4_score': round(self.d4_score, 3),
            'gate_score': round(self.gate_score, 3),
        }


class GateAgent(BaseAgent):
    """信号筛选Agent"""

    def __init__(self, db: Union[str, DatabaseManager]):
        # 兼容两种调用：传 db_path 字符串自动构造，或直接传 DatabaseManager 共享连接
        if isinstance(db, str):
            self.db_path = db
            db_manager = DatabaseManager(db)
        else:
            self.db_path = db.db_path
            db_manager = db
        super().__init__(name="gate_agent", db=db_manager)
        self.now = datetime.now(timezone.utc)

    def run(self, top_n: int = 10) -> Optional[dict]:
        """Agent 主入口：生成 Gate 报告，包错误处理矩阵。"""
        return self.run_with_error_handling(self.generate_report, top_n)

    def get_authority_weight(self, publisher: str) -> float:
        """根据发文机关判断权重"""
        if not publisher:
            return Authority.ASSOCIATION.value

        publisher = publisher.lower()

        # 国务院
        if '国务院' in publisher and ('办公厅' in publisher or publisher == '国务院'):
            return Authority.STATE_COUNCIL.value

        # 中央部委
        ministries = ['工信部', '发改委', '国家发改委', '财政部', '科技部',
                     '自然资源部', '生态环境部', '商务部', '国家统计局']
        if any(m in publisher for m in ministries):
            return Authority.MINISTRY.value

        # 行业专业部门
        bureaus = ['国家能源局', '国家医保局', '国家药监局', '中国证监会',
                  '交通运输部', '农业农村部']
        if any(b in publisher for b in bureaus):
            return Authority.BUREAU.value

        # 地方政府
        if any(x in publisher for x in ['市', '省', '书记会', '政府', '政协']):
            return Authority.LOCAL.value

        # 行业协会
        return Authority.ASSOCIATION.value

    def calculate_d1_score(self, info_unit: dict) -> float:
        """计算D1（政策）评分"""
        if info_unit['source'] != 'D1':
            return 0.0

        # 1. 发文机关权重
        publisher = info_unit.get('publisher', '')
        authority_weight = self.get_authority_weight(publisher)

        # 2. 命中关键词数
        keyword_hits = info_unit.get('keyword_hits', [])
        hit_count = len(keyword_hits) if keyword_hits else 0
        keyword_multiplier = 1.2 if hit_count >= 2 else 1.0

        # 3. 时间近度
        timestamp_str = info_unit.get('timestamp', '')
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            days_old = (self.now - timestamp).days
        except (ValueError, TypeError) as e:
            self.logger.debug(f"timestamp parse fallback (days_old=30): {e}")
            days_old = 30

        if days_old <= 1:
            time_multiplier = 1.0
        elif days_old <= 3:
            time_multiplier = 0.95
        elif days_old <= 7:
            time_multiplier = 0.9
        else:  # > 7 days
            time_multiplier = 0.7

        d1_score = authority_weight * keyword_multiplier * time_multiplier
        return min(d1_score, 1.2)  # Cap at 1.2

    def calculate_s4_score(self, info_unit: dict) -> float:
        """计算S4（股票基本面）评分"""
        # 暂时返回0，因为Phase 1还没有stock_financials关联
        # Phase 2A会补充：查related_stocks，获取Z-Score和PEG
        return 0.0

    def calculate_d4_score(self, info_unit: dict) -> float:
        """计算D4（科研趋势）评分"""
        # 暂时返回0，因为Phase 1还没有论文journal质量评分
        # Phase 2A会补充：查related_papers，评估期刊质量和发表时间
        return 0.0

    def calculate_gate_score(self, d1_score: float, s4_score: float, d4_score: float) -> float:
        """计算综合Gate评分"""
        # 权重：D1(政策)=60% + S4(财报)=30% + D4(论文)=10%
        gate_score = (d1_score * 0.6) + (s4_score * 0.3) + (d4_score * 0.1)
        return gate_score

    def score_signal(self, info_unit: dict) -> SignalScore:
        """对单条信号评分"""
        d1_score = self.calculate_d1_score(info_unit)
        s4_score = self.calculate_s4_score(info_unit)
        d4_score = self.calculate_d4_score(info_unit)
        gate_score = self.calculate_gate_score(d1_score, s4_score, d4_score)

        return SignalScore(
            info_unit_id=info_unit['id'],
            source=info_unit['source'],
            title=info_unit.get('title', ''),
            publisher=info_unit.get('publisher', ''),
            timestamp=info_unit.get('timestamp', ''),
            d1_score=d1_score,
            s4_score=s4_score,
            d4_score=d4_score,
            gate_score=gate_score,
        )

    def get_all_signals(self) -> List[dict]:
        """从数据库获取所有info_units"""
        try:
            rows = self.db.query("""
                SELECT
                    id,
                    source,
                    json_extract(content, '$.title') as title,
                    json_extract(content, '$.publisher') as publisher,
                    json_extract(content, '$.keyword_hits') as keyword_hits,
                    timestamp
                FROM info_units
                WHERE source IN ('D1', 'S4', 'D4')
                ORDER BY timestamp DESC
            """)

            signals = []
            for row in rows:
                keyword_hits = []
                kh = row['keyword_hits']
                if kh:
                    try:
                        keyword_hits = json.loads(kh) if isinstance(kh, str) else kh
                    except (json.JSONDecodeError, TypeError) as e:
                        self.logger.warning(f"keyword_hits parse error (row id={row['id']}): {e}")

                signals.append({
                    'id': row['id'],
                    'source': row['source'],
                    'title': row['title'] or '',
                    'publisher': row['publisher'] or '',
                    'keyword_hits': keyword_hits,
                    'timestamp': row['timestamp'] or '',
                })

            return signals

        except sqlite3.Error as e:
            self.logger.error(f"Database error in get_all_signals: {e}")
            return []

    def rank_signals(self, top_n: int = 10) -> List[SignalScore]:
        """
        对所有信号评分并排序

        Args:
            top_n: 返回Top N条信号

        Returns:
            按gate_score排序的SignalScore列表
        """
        self.logger.info("Starting signal ranking...")

        signals = self.get_all_signals()
        self.logger.info(f"Fetched {len(signals)} signals from database")

        scores = [self.score_signal(s) for s in signals]

        # 按gate_score降序排序
        scores.sort(key=lambda x: x.gate_score, reverse=True)

        self.logger.info(f"Top 3 scores: {[s.gate_score for s in scores[:3]]}")

        return scores[:top_n]

    def generate_report(self, top_n: int = 10) -> dict:
        """
        生成Gate Agent报告

        Returns:
            包含排序后的信号、统计信息的报告
        """
        ranked = self.rank_signals(top_n)
        # 复用一次查询而不是重复调 get_all_signals
        total = len(self.get_all_signals())

        report = {
            'timestamp': self.now.isoformat(),
            'total_signals_processed': total,
            'top_n': top_n,
            'signals': [s.to_dict() for s in ranked],
            'distribution': {
                'high': len([s for s in ranked if s.gate_score >= 0.7]),      # >= 0.7
                'medium': len([s for s in ranked if 0.5 <= s.gate_score < 0.7]),
                'low': len([s for s in ranked if s.gate_score < 0.5]),
            },
            'source_breakdown': {
                'D1': len([s for s in ranked if s.source == 'D1']),
                'S4': len([s for s in ranked if s.source == 'S4']),
                'D4': len([s for s in ranked if s.source == 'D4']),
            }
        }

        return report


# 使用示例
if __name__ == '__main__':
    db_path = 'data/knowledge.db'
    agent = GateAgent(db_path)

    # 走 BaseAgent 的 run_with_error_handling 路径
    report = agent.run(top_n=10)

    if report is None:
        print("[GateAgent] run() returned None — see agent_errors / logs for details")
        sys.exit(1)

    print("\n=== GATE AGENT REPORT ===")
    print(f"Timestamp: {report['timestamp']}")
    print(f"Total signals processed: {report['total_signals_processed']}")
    print(f"\nQuality distribution:")
    print(f"  High (>=0.7):     {report['distribution']['high']}")
    print(f"  Medium (0.5-0.7): {report['distribution']['medium']}")
    print(f"  Low (<0.5):       {report['distribution']['low']}")
    print(f"\nSource breakdown:")
    for source, count in report['source_breakdown'].items():
        print(f"  {source}: {count}")

    print(f"\n=== TOP 10 SIGNALS ===")
    for i, signal in enumerate(report['signals'], 1):
        print(f"\n{i}. {signal['title'][:50]}...")
        print(f"   Publisher: {signal['publisher']}")
        print(f"   Gate Score: {signal['gate_score']} (D1:{signal['d1_score']:.2f} S4:{signal['s4_score']:.2f} D4:{signal['d4_score']:.2f})")
        print(f"   Timestamp: {signal['timestamp']}")
