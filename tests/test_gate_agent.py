"""
Gate Agent 测试用例
测试发文机关权重、命中关键词、时间近度等各维度的评分逻辑
"""

import pytest
from datetime import datetime, timedelta
from agents.gate_agent import GateAgent, Authority, SignalScore


class TestGateAgent:
    """Gate Agent测试套件"""

    @pytest.fixture
    def agent(self):
        """创建测试用的GateAgent实例"""
        return GateAgent('data/knowledge.db')

    def test_authority_weight_state_council(self, agent):
        """测试国务院权重"""
        assert agent.get_authority_weight('国务院办公厅') == Authority.STATE_COUNCIL.value
        assert agent.get_authority_weight('国务院') == Authority.STATE_COUNCIL.value

    def test_authority_weight_ministry(self, agent):
        """测试部委权重"""
        assert agent.get_authority_weight('工信部') == Authority.MINISTRY.value
        assert agent.get_authority_weight('发改委') == Authority.MINISTRY.value
        assert agent.get_authority_weight('国家发改委') == Authority.MINISTRY.value

    def test_authority_weight_bureau(self, agent):
        """测试行业部门权重"""
        assert agent.get_authority_weight('国家能源局') == Authority.BUREAU.value
        assert agent.get_authority_weight('国家医保局') == Authority.BUREAU.value

    def test_authority_weight_local(self, agent):
        """测试地方政府权重"""
        assert agent.get_authority_weight('深圳市政府') == Authority.LOCAL.value
        assert agent.get_authority_weight('浙江省书记会') == Authority.LOCAL.value

    def test_authority_weight_association(self, agent):
        """测试行业协会权重"""
        assert agent.get_authority_weight('中国半导体行业协会') == Authority.ASSOCIATION.value

    def test_d1_score_state_council_recent_2keywords(self, agent):
        """测试场景1：国务院+2个关键词+2天前"""
        info_unit = {
            'source': 'D1',
            'publisher': '国务院办公厅',
            'keyword_hits': ['半导体设备', 'AI芯片'],  # 2个
            'timestamp': (datetime.utcnow() - timedelta(days=2)).isoformat() + 'Z',
        }

        score = agent.calculate_d1_score(info_unit)
        # 1.0 * 1.2 * 0.95 = 1.14
        assert abs(score - 1.14) < 0.01

    def test_d1_score_ministry_recent_1keyword(self, agent):
        """测试场景2：部委+1个关键词+1天前"""
        info_unit = {
            'source': 'D1',
            'publisher': '工信部',
            'keyword_hits': ['AI算力'],  # 1个
            'timestamp': (datetime.utcnow() - timedelta(days=1)).isoformat() + 'Z',
        }

        score = agent.calculate_d1_score(info_unit)
        # 0.8 * 1.0 * 1.0 = 0.8
        assert abs(score - 0.8) < 0.01

    def test_d1_score_local_old_1keyword(self, agent):
        """测试场景3：地方政府+1个关键词+15天前"""
        info_unit = {
            'source': 'D1',
            'publisher': '深圳市政府',
            'keyword_hits': ['储能细分'],  # 1个
            'timestamp': (datetime.utcnow() - timedelta(days=15)).isoformat() + 'Z',
        }

        score = agent.calculate_d1_score(info_unit)
        # 0.5 * 1.0 * 0.7 = 0.35
        assert abs(score - 0.35) < 0.01

    def test_d1_score_non_d1_source(self, agent):
        """测试非D1源返回0"""
        info_unit = {
            'source': 'S4',
            'publisher': '工信部',
            'keyword_hits': ['半导体'],
            'timestamp': datetime.utcnow().isoformat() + 'Z',
        }

        score = agent.calculate_d1_score(info_unit)
        assert score == 0.0

    def test_d1_score_capped_at_1_2(self, agent):
        """测试评分上限是1.2"""
        info_unit = {
            'source': 'D1',
            'publisher': '国务院办公厅',
            'keyword_hits': ['半导体', '芯片', '人工智能'],  # 多于2个也只算2个倍数
            'timestamp': datetime.utcnow().isoformat() + 'Z',
        }

        score = agent.calculate_d1_score(info_unit)
        assert score <= 1.2

    def test_gate_score_calculation(self, agent):
        """测试综合评分计算"""
        d1 = 1.0
        s4 = 0.8
        d4 = 0.5

        gate = agent.calculate_gate_score(d1, s4, d4)
        # 1.0*0.6 + 0.8*0.3 + 0.5*0.1 = 0.6 + 0.24 + 0.05 = 0.89
        assert abs(gate - 0.89) < 0.01

    def test_score_signal_comprehensive(self, agent):
        """测试完整信号评分"""
        info_unit = {
            'id': 123,
            'source': 'D1',
            'title': '国务院办公厅关于推进半导体产业发展的意见',
            'publisher': '国务院办公厅',
            'keyword_hits': ['半导体设备', 'AI芯片'],
            'timestamp': (datetime.utcnow() - timedelta(days=1)).isoformat() + 'Z',
        }

        score = agent.score_signal(info_unit)

        # D1: 1.0 * 1.2 * 1.0 = 1.2
        assert abs(score.d1_score - 1.2) < 0.01

        # S4: 0 (Phase 2A补充)
        assert score.s4_score == 0.0

        # D4: 0 (Phase 2A补充)
        assert score.d4_score == 0.0

        # Gate: 1.2 * 0.6 = 0.72
        assert abs(score.gate_score - 0.72) < 0.01

        # 检查属性
        assert score.info_unit_id == 123
        assert score.source == 'D1'

    def test_signal_score_to_dict(self, agent):
        """测试SignalScore转字典"""
        score = SignalScore(
            info_unit_id=1,
            source='D1',
            title='Test',
            publisher='Test Publisher',
            timestamp='2026-04-18T00:00:00',
            d1_score=0.8,
            s4_score=0.6,
            d4_score=0.4,
            gate_score=0.72,
        )

        d = score.to_dict()
        assert d['info_unit_id'] == 1
        assert d['d1_score'] == 0.8
        assert d['gate_score'] == 0.72


class TestGateAgentIntegration:
    """Gate Agent集成测试"""

    @pytest.fixture
    def agent(self):
        return GateAgent('data/knowledge.db')

    def test_generate_report_returns_valid_structure(self, agent):
        """测试报告结构的有效性"""
        report = agent.generate_report(top_n=5)

        assert 'timestamp' in report
        assert 'total_signals_processed' in report
        assert 'top_n' in report
        assert 'signals' in report
        assert 'distribution' in report
        assert 'source_breakdown' in report

        assert len(report['signals']) <= 5

    def test_generate_report_signals_are_sorted(self, agent):
        """测试信号按gate_score降序排列"""
        report = agent.generate_report(top_n=10)

        scores = [s['gate_score'] for s in report['signals']]
        assert scores == sorted(scores, reverse=True)


# 手动测试场景
def manual_test():
    """手动测试场景（不使用pytest）"""
    agent = GateAgent('data/knowledge.db')

    print("\n=== Manual Test Scenarios ===\n")

    # 场景1：国务院+2关键词+2天前
    print("Scenario 1: State Council + 2 keywords + 2 days old")
    s1 = agent.calculate_d1_score({
        'source': 'D1',
        'publisher': '国务院办公厅',
        'keyword_hits': ['半导体设备', 'AI芯片'],
        'timestamp': (datetime.utcnow() - timedelta(days=2)).isoformat() + 'Z',
    })
    print(f"  Expected: 1.14, Got: {s1:.2f}\n")

    # 场景2：部委+1关键词+1天前
    print("Scenario 2: Ministry + 1 keyword + 1 day old")
    s2 = agent.calculate_d1_score({
        'source': 'D1',
        'publisher': '工信部',
        'keyword_hits': ['AI算力'],
        'timestamp': (datetime.utcnow() - timedelta(days=1)).isoformat() + 'Z',
    })
    print(f"  Expected: 0.80, Got: {s2:.2f}\n")

    # 场景3：地方+1关键词+15天前
    print("Scenario 3: Local + 1 keyword + 15 days old")
    s3 = agent.calculate_d1_score({
        'source': 'D1',
        'publisher': '深圳市政府',
        'keyword_hits': ['储能细分'],
        'timestamp': (datetime.utcnow() - timedelta(days=15)).isoformat() + 'Z',
    })
    print(f"  Expected: 0.35, Got: {s3:.2f}\n")


if __name__ == '__main__':
    # 运行手动测试
    manual_test()
