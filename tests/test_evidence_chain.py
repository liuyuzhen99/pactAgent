"""A7a / A7b：证据的哈希链、独立计数、篡改检测、幂等断言。

需求 R8 说这些记录**不可篡改**。写一份普通 JSON 不叫不可篡改——改一个数字谁也看不出来。
所以每条记录要带 `prev_hash` 串成**哈希链**，任何一处被改都会让链断掉。

A7a 的验收还要求**必须有篡改负例**：只测"正常时 chain_ok=true"是不够的，
因为一个永远返回 true 的硬编码实现也能过——这与被否掉的"tx_hash 为空"
是同一类"功能全坏也过"的空断言。
"""

from __future__ import annotations

import json

from pactpay.evidence import (
    GENESIS_HASH,
    append_record,
    compute_record_hash,
    read_chain,
    verify_chain,
)


def _chain(tmp_path, n=3):
    path = tmp_path / "chain.jsonl"
    for i in range(n):
        append_record(path, {"outcome": "paid" if i % 2 == 0 else "denied", "i": i}, kind="purchase")
    return path


def test_first_record_links_to_genesis(tmp_path):
    """第一条的 prev_hash 必须是创世哈希，不能是空或随意值。"""
    path = _chain(tmp_path, n=1)
    records = read_chain(path)

    assert records[0]["prev_hash"] == GENESIS_HASH
    assert records[0]["seq"] == 0


def test_records_chain_together_and_verify_ok(tmp_path):

    path = _chain(tmp_path)
    records = read_chain(path)

    ok, why = verify_chain(records)

    assert ok, why
    assert records[1]["prev_hash"] == records[0]["hash"]
    assert records[2]["prev_hash"] == records[1]["hash"]


def test_tampering_with_a_record_breaks_the_chain(tmp_path):
    """**A7a 的篡改负例**：改一条记录里的金额，verify 必须报 false。

    没有这条负例，`chain_ok=true` 就可能是一个硬编码的返回值。
    """

    path = _chain(tmp_path)
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["payload"]["outcome"] = "paid"  # 把一条 denied 偷偷改成 paid
    lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")

    ok, why = verify_chain(read_chain(path))

    assert not ok
    assert "哈希" in why or "hash" in why.lower()


def test_deleting_a_record_breaks_the_chain(tmp_path):
    """删掉中间一条也必须被发现——否则"删掉不利记录"就没人挡得住。"""

    path = _chain(tmp_path, n=3)
    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")

    ok, _ = verify_chain(read_chain(path))

    assert not ok


def test_reordering_records_breaks_the_chain(tmp_path):

    path = _chain(tmp_path, n=3)
    lines = path.read_text().splitlines()
    lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n")

    ok, _ = verify_chain(read_chain(path))

    assert not ok


def test_record_hash_covers_the_payload_not_just_the_link():
    """记录哈希必须把 payload 算进去，否则改内容不影响哈希。"""
    base = {"seq": 0, "kind": "purchase", "prev_hash": GENESIS_HASH, "payload": {"amount": "0.005"}}
    changed = {**base, "payload": {"amount": "0.5"}}

    assert compute_record_hash(base) != compute_record_hash(changed)


# ---------------------------------------------------------------------------
# 第 4 轮**自查变异**发现的洞：`verify_chain` 的三条判据**互相遮蔽**，
# 于是每一条单独拆掉都能存活（281 条测试全绿）。
#
#   把 `if record.get("prev_hash") != prev:` 改成 `if False:`  → 绿
#   把 `if record.get("seq") != index:`       改成 `if False:`  → 绿
#
# 原因：我原先的篡改测试都是"删掉中间一条"，而删记录会**同时**违反 seq 和 prev_hash
# —— 谁还在谁就抓住，另一个拆掉看不出来。要钉住每一条判据，
# 必须构造**只违反那一条**的记录。
#
# 下面用手工构造的记录做（自算 `hash`，让"自身哈希"那一半永远是对的），
# 这样每条判据都被**单独**逼到台前。
# ---------------------------------------------------------------------------

def _synthetic_chain(specs: list[dict]) -> list[dict]:
    """按 `specs` 造一串**自哈希正确**的记录（只有链接关系可能故意写坏）。"""
    from pactpay.evidence import GENESIS_HASH, compute_record_hash

    records: list[dict] = []
    prev = GENESIS_HASH
    for index, spec in enumerate(specs):
        record = {
            "seq": spec.get("seq", index),
            "kind": "purchase",
            "payload": {"outcome": "paid", "n": index},
            # 默认接得上；spec 可以用 `prev` 故意写坏
            "prev_hash": spec.get("prev", prev),
        }
        record["hash"] = compute_record_hash(record)  # ← 自哈希永远正确
        records.append(record)
        prev = record["hash"]
    return records


def test_a_broken_prev_hash_link_is_detected_on_its_own():
    """**只**违反 `prev_hash`（seq 连续、自哈希全对）⇒ 必须被发现。

    这条是单独钉 `prev_hash` 判据的：把所有记录的 `seq` 都设成正确的 0/1/2，
    只在第 2 条把 `prev_hash` 写成一个无关的值。
    拆掉 `prev_hash` 检查后，`seq` 检查**不会**接住它 —— 于是变异必然变红。
    """
    from pactpay.evidence import verify_chain

    records = _synthetic_chain([{}, {}, {"prev": "0" * 64}])

    ok, why = verify_chain(records)

    assert ok is False, "prev_hash 接不上却没被发现 —— 链的链接判据没有生效"
    assert "prev_hash" in why or "断" in why, f"报错原因没指向链接：{why}"


def test_a_non_consecutive_seq_is_detected_on_its_own():
    """**只**违反 `seq`（链接全对、自哈希全对）⇒ 必须被发现。

    构造：让 `prev_hash` 逐条正确接上，但把最后一条的 `seq` 重复成 1。
    拆掉 `seq` 检查后，`prev_hash` 检查**不会**接住它。
    """
    from pactpay.evidence import verify_chain

    records = _synthetic_chain([{}, {}, {"seq": 1}])

    ok, why = verify_chain(records)

    assert ok is False, "seq 不连续却没被发现 —— 序号判据没有生效"
    assert "seq" in why, f"报错原因没指向序号：{why}"


def test_deleting_a_middle_record_is_detected(tmp_path):
    """**删掉中间一条**（真实产物路径）⇒ 必须报错。

    删记录会同时违反 seq 与 prev_hash —— 两条判据任一条在就该抓住。
    """
    import json as _json

    from pactpay.evidence import append_record, read_chain, verify_chain

    path = tmp_path / "chain.jsonl"
    for i in range(4):
        append_record(path, {"outcome": "paid", "n": i}, kind="purchase")

    records = read_chain(path)
    del records[1]
    path.write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n")

    ok, why = verify_chain(read_chain(path))

    assert ok is False, "删掉一条记录后链仍然校验通过 —— 一次付款可以被整条抹掉"
    assert "断" in why or "prev_hash" in why or "seq" in why, f"报错原因不清楚：{why}"


def test_reordering_records_is_detected(tmp_path):
    """**换顺序** ⇒ 必须被发现。"""
    import json as _json

    from pactpay.evidence import append_record, read_chain, verify_chain

    path = tmp_path / "chain.jsonl"
    for i in range(3):
        append_record(path, {"outcome": "paid", "n": i}, kind="purchase")

    records = read_chain(path)
    records[0], records[1] = records[1], records[0]
    path.write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n")

    ok, _why = verify_chain(read_chain(path))

    assert ok is False, "调换记录顺序后链仍然校验通过 —— 顺序没有被约束"


def test_tail_truncation_is_caught_by_the_counter_not_the_chain(tmp_path):
    """**尾部截断**：`prev_hash` 唯一抓不到的篡改，靠 `counts.json` 兜住。

    这条测试的意义是**把局限写下来**，而不是假装它不存在：
    删掉最后一条不会破坏任何前向链接（没有后继指向它）。
    真正兜住它的是运行时**独立**记的那份计数 —— 两者对不上就是证据。
    """
    import json as _json

    from pactpay.evidence import (
        append_record,
        count_outcomes,
        read_chain,
        verify_chain,
        write_counts,
    )

    path = tmp_path / "chain.jsonl"
    payloads = [{"outcome": "paid"}, {"outcome": "denied"}, {"outcome": "paid"}]
    for payload in payloads:
        append_record(path, payload, kind="purchase")

    write_counts(tmp_path, count_outcomes(payloads))  # 运行时记的是三笔

    records = read_chain(path)[:2]  # 链里只剩两条
    path.write_text("\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n")

    chain_ok, _ = verify_chain(read_chain(path))
    on_chain = count_outcomes([r["payload"] for r in read_chain(path)])

    assert chain_ok is True, (
        "尾部截断按理不该被前向链接抓住 —— 如果这里变红，说明校验器变强了，"
        "把这条测试改成断言 ok is False 并更新说明"
    )
    assert on_chain != count_outcomes(payloads), (
        "尾部截断后链里的计数居然还对得上运行时的 —— counts.json 兜不住这个洞"
    )
