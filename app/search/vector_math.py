"""SQLite 小规模向量扫描使用的数值函数。"""

import math
from collections.abc import Sequence


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """计算余弦相似度，并显式拒绝维度错配与非有限值。"""
    if not left or not right:
        raise ValueError("embedding 向量不能为空")
    if len(left) != len(right):
        raise ValueError(
            f"embedding 维度不匹配：{len(left)} != {len(right)}，请重建索引"
        )

    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for left_value, right_value in zip(left, right):
        a = float(left_value)
        b = float(right_value)
        if not math.isfinite(a) or not math.isfinite(b):
            raise ValueError("embedding 含非有限数值，请重建索引")
        dot += a * b
        left_norm += a * a
        right_norm += b * b

    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    similarity = dot / math.sqrt(left_norm * right_norm)
    return max(-1.0, min(1.0, similarity))
