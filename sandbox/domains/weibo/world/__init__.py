"""世界层：加载场景，把场景和账户合并成运行时世界。

场景（scenes/*.json）放所有人都能看到的公共内容：虚拟时间、公开用户、公开微博和评论、热搜榜。
账户（accounts/*.json）放当前授权用户的内容，见 accounts.py。开局时两者合并成一个 world dict。
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

SCENE_DIR = Path(__file__).resolve().parent.parent / "scenes"


class SceneError(ValueError):
    pass


def list_scenes() -> list[str]:
    return sorted(p.stem for p in SCENE_DIR.glob("*.json"))


def load_scene(scene_id: str) -> dict[str, Any]:
    """读取内置场景 JSON。"""
    if scene_id not in list_scenes():
        raise SceneError(f"场景不存在: {scene_id}")
    return json.loads((SCENE_DIR / f"{scene_id}.json").read_text(encoding="utf-8"))


def merge(scene: dict, account: dict) -> dict:
    """场景 + 账户 → 运行时世界，并检查引用是否自洽，不自洽就拒绝开局。"""
    me = deepcopy(account["me"])
    users = {u["uid"]: deepcopy(u) for u in scene["users"]}
    if me["uid"] in users:
        raise SceneError(f"账户用户 {me['uid']} 和场景里的公开用户重复")
    users[me["uid"]] = me

    statuses = [deepcopy(s) for s in scene["statuses"] + (account.get("statuses") or [])]
    comments = [deepcopy(c) for c in (scene.get("comments") or []) + (account.get("comments") or [])]
    ids = [s["idstr"] for s in statuses]
    if len(ids) != len(set(ids)):
        raise SceneError("微博编号重复")
    for s in statuses:
        if s["uid"] not in users:
            raise SceneError(f"微博 {s['idstr']} 的作者 {s['uid']} 不存在")
        s.setdefault("deleted", False)
        s.setdefault("featured", False)
        s.setdefault("retweeted_id", None)
    for c in comments:
        if c["status_id"] not in ids:
            raise SceneError(f"评论 {c['idstr']} 指向不存在的微博 {c['status_id']}")
        if c["uid"] not in users:
            raise SceneError(f"评论 {c['idstr']} 的作者 {c['uid']} 不存在")
        c.setdefault("reply_to", None)
    for a in account.get("attitudes") or []:
        if a["status_id"] not in ids:
            raise SceneError(f"点赞记录指向不存在的微博 {a['status_id']}")
    categories = list(scene.get("hotCategories") or [])
    for row in scene.get("hotWords") or []:
        if row["category"] not in categories:
            raise SceneError(f"热搜词 {row['word']} 的分类 {row['category']} 不在 hotCategories 里")

    return {
        "sceneId": scene["sceneId"],
        "accountId": account["accountId"],
        "clock": scene["clock"],
        "meUid": me["uid"],
        "users": users,
        "statuses": statuses,
        "comments": comments,
        "attitudes": [deepcopy(a) for a in account.get("attitudes") or []],
        "hotCategories": categories,
        "hotWords": [deepcopy(r) for r in scene.get("hotWords") or []],
        "flags": {},      # 业务故障留下的标记，见 Weibo.fault
        "writeSeq": 0,    # 写操作序号：决定新微博、新评论的编号和时间
    }


# ---------- 时间 ----------

def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def weibo_time(value: str) -> str:
    """ISO 时间 → 微博接口常见的 created_at 格式，如 Wed Nov 18 09:12:00 +0800 2026。"""
    return parse_time(value).strftime("%a %b %d %H:%M:%S %z %Y")
