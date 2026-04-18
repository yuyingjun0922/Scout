import hashlib


def info_unit_id(source: str, title: str, published_date: str) -> str:
    """生成info_units的幂等id"""
    key = f"{source}||{title}||{published_date}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
