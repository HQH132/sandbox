"""模拟微博（Synthetic Weibo MCP adapter）。

实现框架 sandbox/episodes.py 里领域需要的 3 个属性和 6 个方法：
    name、tools、business_faults
    scenes()、accounts()、open()、state()、fault()、call()
设计说明见 docs/design/weibo-domain.md。
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from sandbox.domains.weibo.handlers import (
    CONTENT_REJECTED, CONTENT_TOOLS, HANDLERS, HOT_TOOLS, HOT_UNAVAILABLE, PARAM_ERROR, RATE_LIMITED,
    WeiboError, fail,
)
from sandbox.domains.weibo.world import list_scenes, load_scene, merge
from sandbox.domains.weibo.world.accounts import DEFAULT_ACCOUNT, list_accounts, load_account
from sandbox.domains.weibo.world.variants import apply_variant
from sandbox.episodes import Episode, Fault, Outcome


class Weibo:
    name = "weibo"
    tools = json.loads(Path(__file__).with_name("tools.json").read_text(encoding="utf-8"))
    business_faults = frozenset({"RATE_LIMITED", "HOT_UNAVAILABLE", "STATUS_DELETED", "CONTENT_REJECTED"})

    def scenes(self) -> list[str]:
        return list_scenes()

    def accounts(self) -> list[str]:
        return list_accounts()

    def open(self, scene: str, seed: int, account: str = DEFAULT_ACCOUNT) -> dict:
        return apply_variant(merge(load_scene(scene), load_account(account)), seed)

    def state(self, world: dict) -> dict[str, dict]:
        """判分要核对的东西，整理成“集合 → {编号: 实体}”。框架靠它在每次调用前后算 changes。

        写操作在这里留下的痕迹：
          发微博 / 转发 → statuses 多一条（转发时原微博的 reposts_count 也会变）
          删微博       → statuses 里那条的 deleted 从 false 变成 true
          评论 / 回复  → comments 多一条，对应微博的 comments_count 变
          点赞         → attitudes 多一条，对应微博的 attitudes_count 变
        """
        return {
            "clock": {"now": world["clock"]},
            "statuses": {s["idstr"]: s for s in world["statuses"]},
            "comments": {c["idstr"]: c for c in world["comments"]},
            "attitudes": {f"{a['uid']}:{a['status_id']}": a for a in world["attitudes"]},
            "hotWords": {r["word"]: r for r in world["hotWords"]},
            "flags": dict(world["flags"]),
        }

    def fault(self, world: dict, fault: Fault) -> None:
        """把业务异常落到世界上。开局时框架会先在世界的副本上调用一次，配错了在这里抛 ValueError。

        RATE_LIMITED     接口额度或频次超限。call 时：只影响这一次调用；start 时：整局所有调用都超限。
        HOT_UNAVAILABLE  热搜接口失败。call 时：下一次热搜调用报错；start 时：热搜接口正常返回但榜单为空。
        STATUS_DELETED   目标微博被作者删除（target = 微博 idstr，必须存在）。之后对它的任何操作都返回“微博不存在”。
        CONTENT_REJECTED 发布内容被拒。call 时：下一次发微博、转发、评论、回复被拒；start 时：整局都被拒。
        """
        kind = fault.type
        if kind != "STATUS_DELETED" and fault.target is not None:
            raise ValueError(f"{kind} takes no target")
        lasting = "always" if fault.at == "start" else "once"
        if kind == "RATE_LIMITED":
            world["flags"]["rateLimited"] = lasting
        elif kind == "HOT_UNAVAILABLE":
            world["flags"]["hotEmpty" if fault.at == "start" else "hotError"] = True
        elif kind == "CONTENT_REJECTED":
            world["flags"]["contentRejected"] = lasting
        elif kind == "STATUS_DELETED":
            status = next((s for s in world["statuses"] if s["idstr"] == fault.target), None)
            if status is None:
                raise ValueError(f"unknown status for STATUS_DELETED: {fault.target}")
            if status["deleted"]:
                raise ValueError(f"status already deleted: {fault.target}")
            status["deleted"] = True
        else:
            raise ValueError(f"unknown weibo fault: {kind}")

    def call(self, episode: Episode, tool: str, arguments: dict) -> Outcome:
        world = episode.world
        try:
            envelope = self._dispatch(world, tool, arguments if isinstance(arguments, dict) else {})
        except WeiboError as error:
            envelope = fail(tool, error)
        return Outcome(success=envelope["success"], result=deepcopy(envelope),
                       text=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _dispatch(world: dict, tool: str, arguments: dict) -> dict:
        flags = world["flags"]
        handler = HANDLERS.get(tool)
        if handler is None:
            raise WeiboError(PARAM_ERROR, f"未知工具: {tool}")
        if flags.get("rateLimited"):
            if flags["rateLimited"] == "once":
                del flags["rateLimited"]
            raise WeiboError(RATE_LIMITED, "用户请求频次超过上限")
        if tool in HOT_TOOLS:
            if flags.pop("hotError", False):
                raise WeiboError(HOT_UNAVAILABLE, "热搜服务暂时不可用")
            if flags.get("hotEmpty"):
                envelope = handler(world, arguments)
                envelope["result"]["hot_words"] = []
                return envelope
        if tool in CONTENT_TOOLS and flags.get("contentRejected"):
            if flags["contentRejected"] == "once":
                del flags["contentRejected"]
            raise WeiboError(CONTENT_REJECTED, "内容不符合发布规范")
        return handler(world, arguments)
