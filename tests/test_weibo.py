"""微博领域测试：直接驱动框架的 Episode，不经过 HTTP。

覆盖：接口完整性、各工具的返回格式和规则、写操作在 changes 里留下的证据、
4 种业务异常、框架的超时异常、种子变体。
"""
from __future__ import annotations

import json

import pytest

from sandbox.domains.weibo import Weibo
from sandbox.domains.weibo.handlers import HANDLERS, READ_TOOLS
from sandbox.episodes import Episode, Fault

DOMAIN = Weibo()
ALICE_POST = "5200000000000101"     # alice 自己的微博（梧桐山）
OTHERS_POST = "5200000000000004"    # 别人的微博（大模型）
ALICE_COMMENT = "4900000000000101"  # alice 那条微博下别人的评论


def episode(*faults: Fault, account: str = "alice", seed: int = 0) -> Episode:
    return Episode(DOMAIN, "daily", seed, list(faults), account=account)


def call(ep: Episode, tool: str, **arguments):
    event, _ = ep.invoke(tool, arguments, ep.seq + 1, None)
    return event["business"]["result"], event


def changed(event, collection):
    return [c for c in event["changes"] if c["collection"] == collection]


# ---- 接口 ----

def test_domain_implements_framework_interface():
    for attr in ("name", "tools", "business_faults"):
        assert hasattr(DOMAIN, attr)
    for method in ("scenes", "accounts", "open", "state", "fault", "call"):
        assert callable(getattr(DOMAIN, method))
    assert DOMAIN.scenes() == ["daily"]
    assert DOMAIN.accounts() == ["alice", "bob"]


def test_tools_match_handlers_and_carry_read_only_hints():
    names = [t["name"] for t in DOMAIN.tools]
    assert len(names) == len(set(names)) == len(HANDLERS)
    assert set(names) == set(HANDLERS)
    for tool in DOMAIN.tools:
        assert tool["annotations"]["readOnlyHint"] is (tool["name"] in READ_TOOLS)
        assert tool["inputSchema"]["type"] == "object"


def test_text_is_the_json_envelope():
    ep = episode()
    result, event = call(ep, "users_show_biz")
    assert json.loads(event["business"]["text"]) == result
    assert result["success"] is True and result["result"]["screen_name"] == "Alice在深圳"


# ---- 读 ----

def test_search_returns_fields_iris_compacts():
    """Iris 的 compactWeiboSearch 读 success、result.statuses、total_number 和这些字段。"""
    ep = episode()
    result, _ = call(ep, "search_statuses_limited", q="深圳降温", type=1)
    assert result["success"] is True
    body = result["result"]
    assert body["total_number"] == 3 and len(body["statuses"]) == 3
    for s in body["statuses"]:
        for field in ("idstr", "mid", "created_at", "reposts_count", "comments_count", "attitudes_count"):
            assert field in s
        assert s["user"]["screen_name"]
    times = [s["created_at"] for s in body["statuses"]]
    assert times[0].startswith("Wed Nov 18 09:12")          # 时间倒序


def test_long_status_keeps_full_text_in_long_text():
    ep = episode()
    result, _ = call(ep, "search_statuses_limited", q="深圳本周天气提示")
    status = result["result"]["statuses"][0]
    assert status["isLongText"] is True and status["text"].endswith("...全文")
    assert len(status["longText"]["longTextContent"]) > 500    # Iris 会截到 500 字并标 text_truncated


def test_hot_sort_returns_only_featured_posts():
    ep = episode()
    hot, _ = call(ep, "search_statuses_limited", q="深圳降温", sort="hot")
    assert [s["idstr"] for s in hot["result"]["statuses"]] == ["5200000000000002"]
    assert hot["result"]["total_number"] == 1


def test_search_pages_and_validates():
    ep = episode()
    page1, _ = call(ep, "search_statuses_limited", q="大模型", count=1)
    page2, _ = call(ep, "search_statuses_limited", q="大模型", count=1, page=2)
    assert page1["result"]["total_number"] == 2
    assert page1["result"]["statuses"][0]["idstr"] != page2["result"]["statuses"][0]["idstr"]
    bad, _ = call(ep, "search_statuses_limited", q="大模型", sort="best")
    assert bad["success"] is False and bad["error"]["error_code"] == 10008
    missing, _ = call(ep, "search_statuses_limited")
    assert missing["success"] is False


def test_hot_board_is_ranked_and_filtered_by_category():
    ep = episode()
    board, _ = call(ep, "search_hot_word_biz")
    words = board["result"]["hot_words"]
    assert [w["rank"] for w in words] == list(range(1, 16))
    assert words[0]["word"] == "深圳降温"
    assert all(a["num"] >= b["num"] for a, b in zip(words, words[1:]))
    tech, _ = call(ep, "search_hot_word_cate_biz", category="科技")
    assert [w["word"] for w in tech["result"]["hot_words"]] == ["开源大模型新版本", "电动车冬季续航", "端侧小模型"]
    unknown, _ = call(ep, "search_hot_word_cate_biz", category="军事")
    assert unknown["success"] is False and "可选" in unknown["error"]["error"]


def test_timeline_mentions_and_comments_for_alice():
    ep = episode()
    timeline, _ = call(ep, "statuses_user_timeline_biz")
    assert [s["idstr"] for s in timeline["result"]["statuses"]] == ["5200000000000102", ALICE_POST]
    mentions, _ = call(ep, "statuses_mentions_biz")
    assert [s["idstr"] for s in mentions["result"]["statuses"]] == ["5200000000000103"]
    comments, _ = call(ep, "comments_show_biz", id=ALICE_POST)
    assert comments["result"]["total_number"] == 2


def test_bob_is_an_empty_account():
    ep = episode(account="bob")
    timeline, _ = call(ep, "statuses_user_timeline_biz")
    mentions, _ = call(ep, "statuses_mentions_biz")
    me, _ = call(ep, "users_show_biz")
    assert timeline["result"] == {"statuses": [], "total_number": 0}
    assert mentions["result"]["total_number"] == 0
    assert me["result"]["statuses_count"] == 0


def test_reads_leave_no_changes():
    ep = episode()
    for tool, args in [("search_hot_word_biz", {}), ("search_statuses_limited", {"q": "快递"}),
                       ("users_show_biz", {}), ("comments_show_biz", {"id": ALICE_POST})]:
        _, event = call(ep, tool, **args)
        assert event["changes"] == [], tool


# ---- 写 ----

def test_update_creates_a_status_visible_in_changes_and_timeline():
    ep = episode()
    result, event = call(ep, "statuses_update", status="今天降温，记得加衣服")
    assert result["success"] is True
    new_id = result["result"]["idstr"]
    created = changed(event, "statuses")
    assert len(created) == 1 and created[0]["id"] == new_id and created[0]["before"] is None
    assert created[0]["after"]["uid"] == "2001"
    timeline, _ = call(ep, "statuses_user_timeline_biz")
    assert timeline["result"]["statuses"][0]["idstr"] == new_id


def test_update_rejects_empty_and_duplicate_text():
    ep = episode()
    empty, event = call(ep, "statuses_update", status="   ")
    assert empty["error"]["error_code"] == 20008 and event["changes"] == []
    call(ep, "statuses_update", status="同一句话")
    again, event = call(ep, "statuses_update", status="同一句话")
    assert again["error"]["error_code"] == 20019 and event["changes"] == []


def test_repost_links_original_and_counts():
    ep = episode()
    result, event = call(ep, "statuses_repost", id=OTHERS_POST, status="收藏一下")
    assert result["result"]["retweeted_status"]["idstr"] == OTHERS_POST
    ids = {c["id"] for c in changed(event, "statuses")}
    assert ids == {OTHERS_POST, result["result"]["idstr"]}


def test_destroy_only_own_status():
    ep = episode()
    others, event = call(ep, "statuses_destroy", id=OTHERS_POST)
    assert others["success"] is False and event["changes"] == []
    mine, event = call(ep, "statuses_destroy", id=ALICE_POST)
    assert mine["success"] is True
    [change] = changed(event, "statuses")
    assert change["before"]["deleted"] is False and change["after"]["deleted"] is True
    again, _ = call(ep, "statuses_destroy", id=ALICE_POST)
    assert again["error"]["error_code"] == 20101


def test_comment_and_reply():
    ep = episode()
    created, event = call(ep, "comments_create", id=OTHERS_POST, comment="期待对比数据")
    assert created["success"] is True and len(changed(event, "comments")) == 1
    reply, _ = call(ep, "comments_reply", id=ALICE_POST, cid=ALICE_COMMENT, comment="好呀")
    assert reply["result"]["reply_comment_id"] == ALICE_COMMENT
    wrong, _ = call(ep, "comments_reply", id=OTHERS_POST, cid=ALICE_COMMENT, comment="好呀")
    assert wrong["success"] is False


def test_like_is_idempotent():
    ep = episode()
    first, event = call(ep, "attitudes_create", id=OTHERS_POST)
    assert first["success"] is True and len(changed(event, "attitudes")) == 1
    second, event = call(ep, "attitudes_create", id=OTHERS_POST)
    assert second["success"] is True and event["changes"] == []


def test_new_ids_are_reproducible():
    a, b = episode(), episode()
    ra, _ = call(a, "statuses_update", status="可复现")
    rb, _ = call(b, "statuses_update", status="可复现")
    assert ra["result"]["idstr"] == rb["result"]["idstr"]
    assert ra["result"]["created_at"] == rb["result"]["created_at"]


# ---- 业务异常 ----

def test_rate_limited_once_at_call():
    ep = episode(Fault(type="RATE_LIMITED", tool="search_statuses_limited", nth=1))
    first, _ = call(ep, "search_statuses_limited", q="快递")
    second, _ = call(ep, "search_statuses_limited", q="快递")
    assert first["error"]["error_code"] == 10023 and second["success"] is True


def test_rate_limited_from_start_blocks_everything():
    ep = episode(Fault(type="RATE_LIMITED", at="start"))
    for tool, args in [("users_show_biz", {}), ("search_hot_word_biz", {}), ("statuses_update", {"status": "x"})]:
        result, event = call(ep, tool, **args)
        assert result["error"]["error_code"] == 10023, tool
        assert not changed(event, "statuses")


def test_hot_unavailable_at_call_then_recovers():
    ep = episode(Fault(type="HOT_UNAVAILABLE", tool="search_hot_word_biz", nth=1))
    first, _ = call(ep, "search_hot_word_biz")
    second, _ = call(ep, "search_hot_word_biz")
    assert first["success"] is False and second["success"] is True and second["result"]["hot_words"]


def test_hot_unavailable_from_start_returns_empty_board():
    ep = episode(Fault(type="HOT_UNAVAILABLE", at="start"))
    board, _ = call(ep, "search_hot_word_biz")
    cate, _ = call(ep, "search_hot_word_cate_biz", category="科技")
    assert board["success"] is True and board["result"]["hot_words"] == []
    assert cate["result"]["hot_words"] == []
    search, _ = call(ep, "search_statuses_limited", q="深圳降温")   # 只影响热搜
    assert search["result"]["total_number"] == 3


def test_status_deleted_validates_target_and_hides_status():
    with pytest.raises(ValueError):
        episode(Fault(type="STATUS_DELETED", at="start", target="5200000000099999"))
    ep = episode(Fault(type="STATUS_DELETED", tool="comments_create", nth=1, target=OTHERS_POST))
    result, _ = call(ep, "comments_create", id=OTHERS_POST, comment="还在吗")
    assert result["error"]["error_code"] == 20101
    search, _ = call(ep, "search_statuses_limited", q="开源大模型")
    assert search["result"]["total_number"] == 0


def test_content_rejected_once():
    ep = episode(Fault(type="CONTENT_REJECTED", tool="statuses_update", nth=1))
    first, event = call(ep, "statuses_update", status="第一次")
    assert first["error"]["error_code"] == 20021 and not changed(event, "statuses")
    second, _ = call(ep, "statuses_update", status="第一次")
    assert second["success"] is True


def test_faults_with_targets_are_validated():
    with pytest.raises(ValueError):
        episode(Fault(type="RATE_LIMITED", at="start", target="x"))


# ---- 框架的传输异常 ----

def test_timeout_committed_on_update_still_creates_status():
    """TIMEOUT_COMMITTED：沙箱照常发布，Iris 收到 504。模型应先查本人微博，不能重复发布。"""
    ep = episode(Fault(type="TIMEOUT_COMMITTED", tool="statuses_update", nth=1))
    event, override = ep.invoke("statuses_update", {"status": "超时也发出去了"}, 1, None)
    assert override is not None and event["business"]["executed"] is True
    assert len(changed(event, "statuses")) == 1
    timeline, _ = call(ep, "statuses_user_timeline_biz")
    assert timeline["result"]["statuses"][0]["text"] == "超时也发出去了"
    duplicate, _ = call(ep, "statuses_update", status="超时也发出去了")
    assert duplicate["error"]["error_code"] == 20019


# ---- 种子变体 ----

def test_seed_changes_numbers_but_not_ids_or_text():
    base, other = episode(), episode(seed=7)
    b, _ = call(base, "search_hot_word_biz")
    o, _ = call(other, "search_hot_word_biz")
    assert sorted(w["word"] for w in b["result"]["hot_words"]) == sorted(w["word"] for w in o["result"]["hot_words"])
    assert [w["num"] for w in b["result"]["hot_words"]] != [w["num"] for w in o["result"]["hot_words"]]
    again, _ = call(episode(seed=7), "search_hot_word_biz")
    assert again == o
    s, _ = call(other, "comments_show_biz", id=ALICE_POST)
    assert s["success"] is True


# ---- 场景、账户和隔离 ----

def test_unknown_scene_or_account_is_rejected():
    from sandbox.domains.weibo.world import SceneError
    from sandbox.domains.weibo.world.accounts import AccountError
    with pytest.raises(SceneError):
        DOMAIN.open("missing", 0, "alice")
    with pytest.raises(AccountError):
        DOMAIN.open("daily", 0, "nobody")


def test_merge_rejects_dangling_references():
    from sandbox.domains.weibo.world import SceneError, load_scene, merge
    from sandbox.domains.weibo.world.accounts import load_account
    account = load_account("alice")
    account["comments"].append({"idstr": "4900000000009999", "status_id": "5200000000009999",
                                "uid": "1004", "created_at": "2026-11-18T09:00:00+08:00", "text": "x"})
    with pytest.raises(SceneError):
        merge(load_scene("daily"), account)


def test_episodes_do_not_share_state():
    a, b = episode(), episode()
    call(a, "statuses_update", status="只在 a 里")
    timeline, _ = call(b, "statuses_user_timeline_biz")
    assert all(s["text"] != "只在 a 里" for s in timeline["result"]["statuses"])


def test_unknown_tool_returns_error_envelope():
    ep = episode()
    result, event = call(ep, "statuses_update_biz", status="旧名字")
    assert result["success"] is False and result["error"]["error_code"] == 10008
    assert event["changes"] == []
