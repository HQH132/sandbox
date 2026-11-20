"""微博工具的处理逻辑。

返回格式（信封）：
    成功：{"success": true,  "result": <接口数据>}
    失败：{"success": false, "error": {"error_code": <整数>, "error": <说明>, "request": <接口路径>}}

依据和不确定的地方：
- 成功信封来自 Iris 源码 AgentToolkitFactory.compactWeiboSearch：它读取 payload.success 和
  payload.result.statuses / total_number，说明真实 search_statuses_limited 返回的就是这个外层结构。
- 其他工具的 result 内部字段、失败信封的结构、错误码，都是按微博开放平台接口文档推断的，
  需要用真实录制校准（见 docs/design/weibo-domain.md 第 6 节）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from sandbox.domains.weibo.world import parse_time, weibo_time

LONG_TEXT_LIMIT = 140        # 列表接口里正文超过这个长度时，text 截断，全文放在 longText
MAX_STATUS_LENGTH = 2000
MAX_COMMENT_LENGTH = 140
STATUS_ID_BASE = 5200000000100000
COMMENT_ID_BASE = 4900000000100000

READ_TOOLS = frozenset({
    "search_hot_word_biz", "search_hot_word_cate_biz", "search_statuses_limited",
    "users_show_biz", "statuses_user_timeline_biz", "statuses_mentions_biz", "comments_show_biz",
})
HOT_TOOLS = frozenset({"search_hot_word_biz", "search_hot_word_cate_biz"})
CONTENT_TOOLS = frozenset({"statuses_update", "statuses_repost", "comments_create", "comments_reply"})


class WeiboError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# 错误码：前四个和微博开放平台公开文档一致；其余是沙箱自定的，待真实录制校准。
PARAM_ERROR = 10008            # 参数错误
RATE_LIMITED = 10023           # 用户请求频次超过上限
CONTENT_NULL = 20008           # 内容为空
STATUS_NOT_FOUND = 20101       # 微博不存在
CONTENT_REJECTED = 20021       # 内容不合规（待校准）
NOT_OWNER = 20112              # 不能删除别人的微博（待校准）
COMMENT_NOT_FOUND = 20201      # 评论不存在（待校准）
TEXT_TOO_LONG = 20012          # 内容超长（待校准）
HOT_UNAVAILABLE = 10001        # 系统错误，用于“热搜服务暂时不可用”（待校准）


def ok(result: Any) -> dict:
    return {"success": True, "result": result}


def fail(tool: str, error: WeiboError) -> dict:
    return {"success": False, "error": {"error_code": error.code, "error": error.message,
                                        "request": "/2/" + tool.replace("_", "/") + ".json"}}


# ---- 参数 ----

def _int(args: dict, key: str, default: int, low: int, high: int | None = None) -> int:
    value = args.get(key, default)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise WeiboError(PARAM_ERROR, f"参数 {key} 必须是整数")
    if value < low or (high is not None and value > high):
        raise WeiboError(PARAM_ERROR, f"参数 {key} 超出范围")
    return value


def _text(args: dict, key: str, limit: int, required: bool = True) -> str:
    value = args.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise WeiboError(PARAM_ERROR, f"缺少参数 {key}")
    value = value.strip()
    if required and not value:
        raise WeiboError(CONTENT_NULL, "内容为空")
    if len(value) > limit:
        raise WeiboError(TEXT_TOO_LONG, f"内容超过 {limit} 字")
    return value


def _id(args: dict, key: str = "id") -> str:
    value = args.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not value.strip().isdigit():
        raise WeiboError(PARAM_ERROR, f"参数 {key} 必须是数字 ID")
    return value.strip()


def _page(rows: list, args: dict, default_count: int = 10) -> list:
    count = _int(args, "count", default_count, 1, 50)
    page = _int(args, "page", 1, 1)
    return rows[(page - 1) * count: page * count]


# ---- 世界查询 ----

def _status(world: dict, idstr: str) -> dict:
    for s in world["statuses"]:
        if s["idstr"] == idstr and not s["deleted"]:
            return s
    raise WeiboError(STATUS_NOT_FOUND, "微博不存在")


def _visible(world: dict) -> list[dict]:
    rows = [s for s in world["statuses"] if not s["deleted"]]
    return sorted(rows, key=lambda s: (s["created_at"], s["idstr"]), reverse=True)


def _now(world: dict) -> str:
    """写操作的发生时间：场景时钟 + 已发生写操作的秒数，保证同一种子下完全可复现。"""
    return (parse_time(world["clock"]) + timedelta(seconds=world["writeSeq"])).isoformat()


def _next(world: dict, base: int) -> str:
    world["writeSeq"] += 1
    return str(base + world["writeSeq"])


# ---- 渲染成接口格式 ----

def render_user(world: dict, uid: str) -> dict:
    u = world["users"][uid]
    return {"id": int(uid), "idstr": uid, "screen_name": u["screen_name"]}


def render_status(world: dict, s: dict, nested: bool = True) -> dict:
    text = s["text"]
    long_text = len(text) > LONG_TEXT_LIMIT
    out = {
        "id": int(s["idstr"]), "idstr": s["idstr"], "mid": s["idstr"],
        "created_at": weibo_time(s["created_at"]),
        "text": text[:LONG_TEXT_LIMIT] + "...全文" if long_text else text,
        "isLongText": long_text,
        "user": render_user(world, s["uid"]),
        "reposts_count": s["reposts_count"], "comments_count": s["comments_count"],
        "attitudes_count": s["attitudes_count"],
    }
    if long_text:
        out["longText"] = {"longTextContent": text}
    if nested and s.get("retweeted_id"):
        original = next((x for x in world["statuses"] if x["idstr"] == s["retweeted_id"]), None)
        if original is None or original["deleted"]:
            out["retweeted_status"] = {"idstr": s["retweeted_id"], "deleted": True, "text": "抱歉，此微博已被作者删除。"}
        else:
            out["retweeted_status"] = render_status(world, original, nested=False)
    return out


def render_comment(world: dict, c: dict) -> dict:
    out = {"id": int(c["idstr"]), "idstr": c["idstr"], "created_at": weibo_time(c["created_at"]),
           "text": c["text"], "user": render_user(world, c["uid"]), "status_id": c["status_id"]}
    if c.get("reply_to"):
        out["reply_comment_id"] = c["reply_to"]
    return out


# ---- 读工具 ----

def hot_words(world: dict, args: dict) -> dict:
    count = _int(args, "count", 50, 1, 50)
    return ok({"hot_words": _ranked(world["hotWords"])[:count], "updated_at": weibo_time(world["clock"])})


def hot_words_by_category(world: dict, args: dict) -> dict:
    category = args.get("category")
    if not isinstance(category, str) or category not in world["hotCategories"]:
        raise WeiboError(PARAM_ERROR, "分类不存在，可选：" + "、".join(world["hotCategories"]))
    count = _int(args, "count", 20, 1, 50)
    rows = _ranked([r for r in world["hotWords"] if r["category"] == category])[:count]
    return ok({"category": category, "hot_words": rows, "updated_at": weibo_time(world["clock"])})


def _ranked(rows: list[dict]) -> list[dict]:
    ordered = sorted(rows, key=lambda r: (-r["num"], r["word"]))
    return [{"rank": i, "word": r["word"], "num": r["num"], "category": r["category"], "label": r["label"]}
            for i, r in enumerate(ordered, 1)]


SORT_KEYS: dict[str, Callable[[dict], Any]] = {
    "time": lambda s: (s["created_at"], s["idstr"]),
    "hot": lambda s: (s["attitudes_count"], s["idstr"]),
    "fwnum": lambda s: (s["reposts_count"], s["idstr"]),
    "cmtnum": lambda s: (s["comments_count"], s["idstr"]),
}


def search_statuses(world: dict, args: dict) -> dict:
    q = args.get("q")
    if not isinstance(q, str) or not q.strip():
        raise WeiboError(PARAM_ERROR, "缺少参数 q")
    sort = args.get("sort", "time")
    if sort not in SORT_KEYS:
        raise WeiboError(PARAM_ERROR, "sort 只能是 time、hot、fwnum、cmtnum")
    start = _int(args, "starttime", 0, 0)
    end = _int(args, "endtime", 0, 0)
    words = q.strip().lower().split()
    rows = [s for s in world["statuses"] if not s["deleted"] and all(w in s["text"].lower() for w in words)]
    if sort == "hot":
        rows = [s for s in rows if s["featured"]]      # 文档：sort=hot 只返回精选微博
    if start:
        rows = [s for s in rows if parse_time(s["created_at"]).timestamp() >= start]
    if end:
        rows = [s for s in rows if parse_time(s["created_at"]).timestamp() <= end]
    rows.sort(key=SORT_KEYS[sort], reverse=True)
    return ok({"statuses": [render_status(world, s) for s in _page(rows, args)], "total_number": len(rows)})


def me(world: dict, args: dict) -> dict:
    uid = world["meUid"]
    u = world["users"][uid]
    count = sum(1 for s in world["statuses"] if s["uid"] == uid and not s["deleted"])
    return ok({"id": int(uid), "idstr": uid, "screen_name": u["screen_name"], "description": u["description"],
               "followers_count": u["followers_count"], "friends_count": u["friends_count"],
               "statuses_count": count})


def user_timeline(world: dict, args: dict) -> dict:
    rows = [s for s in _visible(world) if s["uid"] == world["meUid"]]
    return ok({"statuses": [render_status(world, s) for s in _page(rows, args)], "total_number": len(rows)})


def mentions(world: dict, args: dict) -> dict:
    tag = "@" + world["users"][world["meUid"]]["screen_name"]
    rows = [s for s in _visible(world) if s["uid"] != world["meUid"] and tag in s["text"]]
    return ok({"statuses": [render_status(world, s) for s in _page(rows, args)], "total_number": len(rows)})


def comments_show(world: dict, args: dict) -> dict:
    status = _status(world, _id(args))
    rows = sorted((c for c in world["comments"] if c["status_id"] == status["idstr"]),
                  key=lambda c: (c["created_at"], c["idstr"]), reverse=True)
    return ok({"comments": [render_comment(world, c) for c in _page(rows, args, 20)], "total_number": len(rows)})


# ---- 写工具 ----

def update(world: dict, args: dict) -> dict:
    text = _text(args, "status", MAX_STATUS_LENGTH)
    _reject_duplicate(world, text)
    created = _now(world)
    status = {"idstr": _next(world, STATUS_ID_BASE), "uid": world["meUid"], "created_at": created, "text": text,
              "reposts_count": 0, "comments_count": 0, "attitudes_count": 0, "featured": False,
              "deleted": False, "retweeted_id": None}
    world["statuses"].append(status)
    return ok(render_status(world, status))


def repost(world: dict, args: dict) -> dict:
    original = _status(world, _id(args))
    text = _text(args, "status", MAX_STATUS_LENGTH, required=False) or "转发微博"
    created = _now(world)
    status = {"idstr": _next(world, STATUS_ID_BASE), "uid": world["meUid"], "created_at": created, "text": text,
              "reposts_count": 0, "comments_count": 0, "attitudes_count": 0, "featured": False,
              "deleted": False, "retweeted_id": original["idstr"]}
    original["reposts_count"] += 1
    world["statuses"].append(status)
    return ok(render_status(world, status))


def destroy(world: dict, args: dict) -> dict:
    status = _status(world, _id(args))
    if status["uid"] != world["meUid"]:
        raise WeiboError(NOT_OWNER, "只能删除自己发布的微博")
    rendered = render_status(world, status)
    status["deleted"] = True
    return ok(rendered)


def comment_create(world: dict, args: dict) -> dict:
    status = _status(world, _id(args))
    text = _text(args, "comment", MAX_COMMENT_LENGTH)
    return ok(_add_comment(world, status, text, None))


def comment_reply(world: dict, args: dict) -> dict:
    status = _status(world, _id(args))
    cid = _id(args, "cid")
    target = next((c for c in world["comments"] if c["idstr"] == cid), None)
    if target is None or target["status_id"] != status["idstr"]:
        raise WeiboError(COMMENT_NOT_FOUND, "评论不存在或不属于这条微博")
    text = _text(args, "comment", MAX_COMMENT_LENGTH)
    return ok(_add_comment(world, status, text, cid))


def _add_comment(world: dict, status: dict, text: str, reply_to: str | None) -> dict:
    created = _now(world)
    comment = {"idstr": _next(world, COMMENT_ID_BASE), "status_id": status["idstr"], "uid": world["meUid"],
               "created_at": created, "text": text, "reply_to": reply_to}
    world["comments"].append(comment)
    status["comments_count"] += 1
    return render_comment(world, comment)


def attitude_create(world: dict, args: dict) -> dict:
    status = _status(world, _id(args))
    existing = next((a for a in world["attitudes"]
                     if a["status_id"] == status["idstr"] and a["uid"] == world["meUid"]), None)
    if existing is None:
        # 重复点赞按幂等处理（不报错、不重复计数）。真实接口是否报错，待校准。
        existing = {"status_id": status["idstr"], "uid": world["meUid"], "attitude": "heart",
                    "created_at": _now(world)}
        world["writeSeq"] += 1
        world["attitudes"].append(existing)
        status["attitudes_count"] += 1
    return ok({"status": render_status(world, status, nested=False), "attitude": existing["attitude"],
               "created_at": weibo_time(existing["created_at"])})


def _reject_duplicate(world: dict, text: str) -> None:
    for s in world["statuses"]:
        if s["uid"] == world["meUid"] and not s["deleted"] and s["text"] == text:
            raise WeiboError(20019, "不能重复发布相同内容的微博")    # 20019 与公开文档一致


HANDLERS: dict[str, Callable[[dict, dict], dict]] = {
    "search_hot_word_biz": hot_words,
    "search_hot_word_cate_biz": hot_words_by_category,
    "search_statuses_limited": search_statuses,
    "users_show_biz": me,
    "statuses_user_timeline_biz": user_timeline,
    "statuses_mentions_biz": mentions,
    "comments_show_biz": comments_show,
    "statuses_update": update,
    "statuses_repost": repost,
    "statuses_destroy": destroy,
    "comments_create": comment_create,
    "comments_reply": comment_reply,
    "attitudes_create": attitude_create,
}
