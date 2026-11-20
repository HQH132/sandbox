"""按种子生成世界变体。

只改热度和互动数，不改正文、编号和时间，所以题目里引用的微博编号在任何种子下都有效；
热搜排名会随热度变化，题目不能写死“第几名是什么”，要从工具返回里取。
种子为 0 时不做任何改动。
"""
from __future__ import annotations

import random
from copy import deepcopy


def apply_variant(world: dict, seed: int) -> dict:
    if not seed:
        return world
    rng = random.Random(f"weibo:{seed}")
    world = deepcopy(world)
    for row in world["hotWords"]:
        row["num"] = max(1000, int(row["num"] * rng.uniform(0.6, 1.4)))
    for status in world["statuses"]:
        for key in ("reposts_count", "comments_count", "attitudes_count"):
            status[key] = max(0, int(status[key] * rng.uniform(0.7, 1.3)))
    return world
