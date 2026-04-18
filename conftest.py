"""
conftest.py — pytest项目根配置

将项目根加入sys.path，让tests/能以包路径导入knowledge/infra/utils。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
