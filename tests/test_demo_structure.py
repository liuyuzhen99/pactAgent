"""`demo.py` 的**结构守卫**——用 AST，不用正则。

## 为什么需要这一层

`demo.py` 的 `_run` 要联网、要花测试币，没法在单测里整条跑通。
于是"守卫的**接线处**有没有被改坏"就成了一个没有任何测试覆盖的死角：
第 2 轮评审的 M4（把 A4 守卫改回 `if ... and outcome.settlement_tx:`）**全绿逃逸**。

## 为什么上一版的守卫不够（第 3 轮评审 #4）

上一版是在**源码行**上跑正则，还配了一句 `line.split("#", 1)[0]` 想跳过注释。
评审用两种写法证明了它可被绕过：

1. **换行写**（black 格式化后很自然就会这样）：
   ```python
   if (
       not a4["ok"]
       and outcome.settlement_tx
   ):
   ```
   任何单行正则都匹配不到。
2. **同一行里有字符串字面量含 `#`**：
   ```python
   if not a4["ok"] and outcome.settlement_tx:  # noqa
   ```
   本身没问题；但只要违规行里 `#` 出现在字符串里，
   `split("#", 1)[0]` 就会**在 `#` 处把代码截断**，把违规片段切掉。

**文本匹配在"代码的结构"这件事上永远追不上语言的语法。** 换 AST：
让 `ast` 去处理换行、括号、字符串、注释——它本来就该干这个。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "pactpay" / "demo.py"


@pytest.fixture(scope="module")
def demo_tree() -> ast.Module:
    return ast.parse(DEMO.read_text(), filename=str(DEMO))



def _is_constant(node: ast.AST) -> bool:
    """这个表达式是不是一个**常量**（`True`/`False`/字面量/常量折叠的结果）。

    用来识别 `if False and X:` 这种"形状还在、但永远不执行"的写法。
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.BoolOp):
        return all(_is_constant(v) for v in node.values)
    if isinstance(node, ast.UnaryOp):
        return _is_constant(node.operand)
    return False


def _has_dead_branch(node: ast.AST) -> bool:
    """表达式里有没有**恒不成立**的分支。

    覆盖本轮被抓到的三种"形状还在、语义没了"的写法：

    * `if False and X:`            —— `BoolOp` 里出现常量 `False`
    * `X if True else Y`           —— `IfExp` 的测试是常量
    * `None if False else X`       —— 同上，只是把死代码放在 body
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.BoolOp):
            if any(_is_constant(v) for v in sub.values):
                return True
        if isinstance(sub, ast.IfExp) and _is_constant(sub.test):
            return True
    return False


def _iter_ifs(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            yield node


def _source_line(node: ast.AST, src_lines: list[str]) -> str:
    return src_lines[node.lineno - 1].strip()


def _names_in(node: ast.AST) -> set[str]:
    """收集一个表达式里出现的所有名字 / 属性末段 / 字符串常量。

    用"出现过哪些名字"来判定语义，而不是匹配源码文本 ——
    这样换行、括号、别名都影响不了它。
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            found.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            found.add(sub.attr)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            found.add(sub.value)
        elif isinstance(sub, ast.Subscript):
            # 形如 a4["ok"] / trace["ok"]：把键也收进来
            sl = sub.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                found.add(sl.value)
    return found


# ---------------------------------------------------------------------------
# #4：A4 守卫的调用点不许用 settlement_tx 做短路（AST 版，不可绕过）
# ---------------------------------------------------------------------------

def _short_circuits_on(node: ast.AST, name: str) -> bool:
    """`node` 是不是**用 `name` 当了短路条件**。

    第 4 轮评审 F7 的教训：上一版只查 `BoolOp`，于是下面这些写法全部穿过 ——

    * `... if outcome.settlement_tx else {ok: True, checked: False}`  （`IfExp`）
    * `getattr(outcome, "settlement_tx", None) and ...`               （`getattr` 间接取）

    判据改成"**这个表达式在拿 `settlement_tx` 的取值决定走哪条路**"，
    覆盖 `BoolOp`（and/or）、`IfExp`（三目）、以及把取值包在条件里的 `if`。
    """
    for sub in ast.walk(node):
        if isinstance(sub, (ast.BoolOp,)) and name in _names_in(sub):
            return True
        if isinstance(sub, ast.IfExp) and name in _names_in(sub.test):
            return True
        if isinstance(sub, ast.If) and name in _names_in(sub.test):
            return True
        # `getattr(outcome, "settlement_tx", None)` 这种间接取值
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "getattr":
            if any(isinstance(a, ast.Constant) and a.value == name for a in sub.args):
                return True
    return False


def test_no_short_circuit_on_settlement_tx(demo_tree: ast.Module):
    """**不许拿 `settlement_tx` 当条件决定走哪条路。**

    语义理由：`settlement_tx` 是**结果**（付款成功才有哈希），不是**条件**。
    拿它去短路"这笔到底成没成功"的判断，等于"没哈希就当没发生"——
    正好把"记成 paid 但拿不到哈希"这种**最该报警**的异常静默吞掉。

    **连 `getattr` 间接取值也一起查**（评审 F7）：`getattr(outcome, "settlement_tx", None)`
    在名字层面完全看不见 `settlement_tx`，但语义上一模一样。
    """
    src_lines = DEMO.read_text().splitlines()

    offenders = []
    # ① 任何布尔/三目表达式里拿它当条件
    for node in ast.walk(demo_tree):
        if isinstance(node, (ast.BoolOp, ast.IfExp)) and _short_circuits_on(node, "settlement_tx"):
            offenders.append(f"demo.py:{node.lineno}: {_source_line(node, src_lines)}")
    # ② 任何 if 语句拿它当条件
    for node in ast.walk(demo_tree):
        if isinstance(node, ast.If) and _short_circuits_on(node.test, "settlement_tx"):
            offenders.append(f"demo.py:{node.lineno}: {_source_line(node.test, src_lines)}")
    # ③ 任何地方用 getattr 间接取它
    for node in ast.walk(demo_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if any(isinstance(a, ast.Constant) and a.value == "settlement_tx" for a in node.args):
                offenders.append(
                    f"demo.py:{node.lineno}: {_source_line(node, src_lines)}  ← getattr 间接取值"
                )

    assert not offenders, (
        "有表达式把 settlement_tx 当成条件了 —— "
        "这会让「记成 paid 但没有哈希」被静默跳过（M4/M04/N18 的形态）：\n"
        + "\n".join(offenders)
    )


def test_the_ast_guard_actually_detects_the_forbidden_shapes():
    """**阳性对照**：所有已知的绕过写法都必须被抓到。

    没有这一条，上面那条测试有可能只是"永远返回空列表"——
    那就又是一个恒真的绿灯（这个项目已经被抓过好几次同一种病）。

    评审 F7 演示了两种能穿过旧守卫的写法，这里把它们连同换行/注释变体一起钉住。
    """
    shapes = {
        "换行 and": (
            "if (\n"
            '    not a4["ok"]\n'
            "    and outcome.settlement_tx\n"
            "):\n"
            "    raise RuntimeError('x')\n"
        ),
        "单行含 # 字符串": (
            'if not a4["ok"] and outcome.settlement_tx:  # note: "a#b"\n'
            '    raise RuntimeError("x")\n'
        ),
        "三目 IfExp": (
            'a4 = check(tx) if outcome.settlement_tx else {"ok": True, "checked": False}\n'
        ),
        "getattr 间接取值": (
            'a4 = check() if getattr(outcome, "settlement_tx", None) else {"ok": True}\n'
        ),
    }

    for label, snippet in shapes.items():
        tree = ast.parse(snippet)
        hits = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.BoolOp, ast.IfExp)) and _short_circuits_on(n, "settlement_tx")
        ]
        getattr_hits = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "getattr"
            and any(isinstance(a, ast.Constant) and a.value == "settlement_tx" for a in n.args)
        ]
        assert hits or getattr_hits, f"{label} 竟然没被 AST 检测到 —— 守卫是瞎的"


def test_the_probe_trace_is_actually_measured(demo_tree: ast.Module):
    """**命中规则那笔的 A5c③ 必须真的去测**（第 4 轮评审 F3b）。

    评审的 N01 变异把探针侧的判据换成 `(None if True else check_no_onchain_transfer(...))`
    —— 判据根本没跑，A5c③ 直接拿到 `None`，**254 条测试全绿**。
    宽化它的窗口（`probe_block_before - 5`）同样全绿（W3 存活）。

    这条用 AST 守住两件事：`matched_trace` 由 `check_no_onchain_transfer` 赋值而来，
    且它的 `window` 是**原样的采样区间**（不许加算术）。
    """
    src_lines = DEMO.read_text().splitlines()

    # ① matched_trace 必须来自 check_no_onchain_transfer 的调用
    measured = False
    for node in ast.walk(demo_tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "matched_trace" for t in node.targets
        ):
            continue
        for sub in ast.walk(node.value):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "check_no_onchain_transfer"
            ):
                measured = True
    assert measured, (
        "`matched_trace` 不是由 `check_no_onchain_transfer(...)` 算出来的 —— "
        "探针那笔的「无链上痕迹」没有真的被测（N01 的形态）"
    )

    # **光"存在这个调用"不够**：`None if True else check_no_onchain_transfer(...)`
    # 里那个调用照样在 AST 上，但永远不执行。评审的 N01 变异就是这样逃过第一版的。
    # 所以还要拒绝常量折叠出来的死分支。
    dead = []
    for node in ast.walk(demo_tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "matched_trace" for t in node.targets):
            continue
        if _has_dead_branch(node.value):
            dead.append(f"demo.py:{node.lineno}: {src_lines[node.lineno - 1].strip()}")
    assert not dead, (
        "`matched_trace` 的赋值里有恒不成立的分支（判据被短路掉，形状还在但永不执行）：\n"
        + "\n".join(dead)
    )

    # ② 传给判据的 window 必须是原样采样区间，不许加算术
    widened = []
    for node in ast.walk(demo_tree):
        if not (isinstance(node, ast.keyword) and node.arg == "window"):
            continue
        value = node.value
        if not (isinstance(value, ast.Tuple) and len(value.elts) == 2):
            continue
        for elt in value.elts:
            if isinstance(elt, ast.BinOp):
                widened.append(f"demo.py:{node.lineno}: {src_lines[node.lineno - 1].strip()}")
    assert not widened, (
        "判定窗口被算术放宽/收窄了 —— 结论会建立在一个与真实尝试不符的区间上（W3 的形态）：\n"
        + "\n".join(widened)
    )


# （原先这里有两条**读 AST** 的测试：`test_the_demo_aborts_when_a5c_does_not_hold`
#   与 `test_the_abort_covers_both_paths`。第 5 轮自查变异证明它们**钉不住行为**：
#   把真正的中止改成 `if False:` 时它们照样绿 —— 因为它们找的是"某个 if 里有
#   `a5c_failures` 和 `raise`"，而真实的中止逻辑与它们匹配的那处**不是同一段代码**。
#   已删除，由下面**调函数看抛不抛**的行为测试取代。留下这段说明是为了让后来者
#   知道：这里曾经有过一个"看起来在守门"的测试，而它守的是形状不是行为。）


# ---------------------------------------------------------------------------
# 第 5 轮**自查变异**：把 A5c③ 的中止判据改成 `if False:` 时 **292 条测试全绿**。
#
# 为什么上面那条 AST 测试没抓住？因为它找的是"某个 `if` 里有 `a5c_failures` 和 `raise`"
# —— 而它找到的是**聚合那一处**，真正在采购循环里那句 `if not trace["ok"]: raise`
# 从来没被行为测试碰过。**读 AST 的测试只钉住写法，钉不住行为。**
#
# 于是改成：把两处中止都抽成**纯函数**（`guard_a5c` / `require_a5c_holds`），
# 下面**调它们看抛不抛**。
#
# 这两条判据是本项目诚实性的最后一道闸：A5c③ 是"钱没动"的唯一证据。
# 它不成立却继续跑完、照样落盘、照样 exit 0，就等于**把"没证明"当成"证明了"**。
# ---------------------------------------------------------------------------

def test_require_a5c_holds_aborts_when_the_x402_trace_is_false():
    """A5c③ 对 x402 那笔**明确不成立** ⇒ 必须抛。"""
    from pactpay.demo import require_a5c_holds

    x402 = {"structured_denial": True, "no_chain_trace": False}
    matched = {"structured_denial": True, "no_chain_trace": True}

    with pytest.raises(RuntimeError) as excinfo:
        require_a5c_holds(x402=x402, matched=matched)

    assert "A5c" in str(excinfo.value)
    assert "x402" in str(excinfo.value), "报错没指出是哪一条判据失败"


def test_require_a5c_holds_aborts_when_the_matched_rule_trace_is_false():
    """A5c③ 对**命中规则那笔**不成立 ⇒ 也必须抛（只守一条路径是 F3c 的原始形态）。"""
    from pactpay.demo import require_a5c_holds

    x402 = {"structured_denial": True, "no_chain_trace": True}
    matched = {"structured_denial": True, "no_chain_trace": False}

    with pytest.raises(RuntimeError):
        require_a5c_holds(x402=x402, matched=matched)


def test_require_a5c_holds_aborts_when_the_denial_is_not_structured():
    """A5c①（拒绝必须是结构化的）不成立 ⇒ 同样中止。"""
    from pactpay.demo import require_a5c_holds

    x402 = {"structured_denial": False, "no_chain_trace": True}
    matched = {"structured_denial": True, "no_chain_trace": True}

    with pytest.raises(RuntimeError):
        require_a5c_holds(x402=x402, matched=matched)


def test_require_a5c_holds_passes_when_everything_holds():
    """**阳性对照**：两条路径都成立时不许抛。

    没有这一条，上面三条可能只是"永远抛"。
    """
    from pactpay.demo import require_a5c_holds

    both = {"structured_denial": True, "no_chain_trace": True}

    require_a5c_holds(x402=both, matched=both)   # 不该抛


def test_require_a5c_holds_does_not_treat_unmeasured_as_failure():
    """`None`（没测）**不等于** `False`（测了没通过）。

    把"没测"当成"没通过"会假报警；反过来把"没通过"当成"没测"就是**把没证明当成证明了**。
    这里钉住的是前者：判据缺失时不该中止（那种情况由 `verify` 的结论去说"没测"）。
    """
    from pactpay.demo import require_a5c_holds

    unmeasured = {"structured_denial": None, "no_chain_trace": None}

    require_a5c_holds(x402=unmeasured, matched=unmeasured)   # 不该抛


def test_guard_a5c_aborts_on_a_failed_trace():
    """采购循环里那道早中止：`ok=False` ⇒ 抛。"""
    from pactpay.demo import guard_a5c

    with pytest.raises(RuntimeError) as excinfo:
        guard_a5c({"ok": False, "why": "阳性对照没数到转账"}, label="x402 被拒那笔")

    assert "阳性对照没数到转账" in str(excinfo.value), "报错没有带上判据给的原因"
    assert "x402" in str(excinfo.value)


def test_guard_a5c_passes_a_good_trace_through():
    """**阳性对照**：`ok=True` 时原样返回（返回值还要被下游用）。"""
    from pactpay.demo import guard_a5c

    trace = {"ok": True, "verdict": "no_trace", "count": 0, "control_count": 1}

    assert guard_a5c(trace, label="命中规则那笔") is trace


def test_guard_a5c_does_not_pass_an_empty_trace():
    """空 trace（没有 `ok` 键）**不许**被当成通过 —— 缺判据 ≠ 判据成立。"""
    from pactpay.demo import guard_a5c

    with pytest.raises(RuntimeError):
        guard_a5c({}, label="x402 被拒那笔")


# ---------------------------------------------------------------------------
# G4（第 5 轮评审，重大）：**判据与它的产物分了家。**
#
# `demo.py` 原先写成"上面一个 `if not a4['ok']: raise`，下面一个
# `if a4['checked']: print "A4 链上复核 ✅ …"`"。评审把上面弱化成
# `if not a4["ok"] and not a4["transfers"]:` —— 复核**失败**时照样打出
# `A4 链上复核 ✅ … 0xdead → 0xbeef` 并把记录记成 paid，292 条测试全绿。
#
# 修法：把闸门挪进**产出 ✅ 的那个函数**里（`render_a4_ok`），
# 于是想打出这个 ✅ 就必须先过 `a4["ok"]`，没有第二条路。
# ---------------------------------------------------------------------------

def test_a4_checkmark_is_impossible_to_render_when_verification_failed():
    """**G4 的核心回归测试**：复核失败 ⇒ 渲染函数必须抛，绝不打 ✅。"""
    import pytest

    from pactpay.demo import render_a4_ok

    failed = {
        "ok": False, "checked": True, "why": "链上查不到这笔交易",
        "transfers": [{"from": "0xdead", "to": "0xbeef", "value": 5000}],
        "block_number": 1,
    }
    with pytest.raises(RuntimeError, match="未通过"):
        render_a4_ok(failed)


def test_a4_checkmark_requires_having_actually_checked():
    """`checked=False`（压根没去链上查）⇒ 也不许渲染 ✅：没查过就没有"通过"可言。"""
    import pytest

    from pactpay.demo import render_a4_ok

    with pytest.raises(RuntimeError, match="没有真的去链上查过"):
        render_a4_ok({"ok": True, "checked": False, "why": ""})


def test_a4_checkmark_requires_transfer_details():
    """判了 ok 却拿不出转账明细 ⇒ 不渲染（否则 ✅ 里没有可核对的事实）。"""
    import pytest

    from pactpay.demo import render_a4_ok

    with pytest.raises(RuntimeError, match="转账明细"):
        render_a4_ok({"ok": True, "checked": True, "transfers": [], "block_number": 9})


def test_a4_checkmark_renders_the_measured_fields():
    """**阳性对照**：真的通过时，那一行里的区块/金额/from/to 全部来自测量值。

    没有这条，上面几条可能只是"永远抛异常"。
    """
    from pactpay.demo import render_a4_ok

    line = render_a4_ok({
        "ok": True, "checked": True, "block_number": 48012189,
        "transfers": [{"from": "0xAAA", "to": "0xBBB", "value": 5000}],
    })

    assert "✅" in line
    assert "48012189" in line and "5000" in line
    assert "0xAAA" in line and "0xBBB" in line


# ---------------------------------------------------------------------------
# G4：A4 的"闸门与 ✅ 在同一处"，且**调用点**也被钉住
# ---------------------------------------------------------------------------


def _outcome(kind: str):
    from pactpay.agent import PurchaseOutcome

    return PurchaseOutcome(
        outcome=kind, resource="/api/x", amount_usd="0.005", description="d",
        data=None, denial={}, http_status=200, settlement_tx=None,
    )


def test_a4_line_is_rendered_only_for_a_paid_purchase():
    """**被拒的那笔没有链上交易可核**，绝不能渲染"A4 链上复核"。

    评审 G4 把 `if outcome.outcome != "paid": return None` 改成 `if False:` 无人变红 ——
    被拒的那笔会去渲染链上复核，而它压根没有链上交易。
    """
    from pactpay.demo import a4_line_for, purchase_trace_lines

    verified = {"ok": True, "checked": True, "block_number": 48014861,
                "transfers": [{"from": "0xa", "to": "0xb", "value": 5000}]}
    denied = {"ok": True, "checked": False, "block_number": None, "transfers": []}

    assert a4_line_for(_outcome("paid"), verified) is not None
    assert a4_line_for(_outcome("denied"), denied) is None, "被拒的那笔不该渲染链上复核"
    assert a4_line_for(_outcome("failed"), denied) is None

    lines, block = purchase_trace_lines(_outcome("paid"), verified)
    assert len(lines) == 1 and block == 48014861

    lines, block = purchase_trace_lines(_outcome("denied"), denied)
    assert lines == [] and block is None, "被拒的那笔不该产生任何链上复核行，也不该给出对照坐标"


def test_a4_render_refuses_when_the_verification_did_not_pass():
    """**没有通过复核就不许打出 ✅** —— 闸门与产物在同一处。

    评审 G4 把"上面 raise、下面 print"的结构弱化成
    `if not a4["ok"] and not a4["transfers"]:`，于是在**复核失败**的情况下
    照样打出 `A4 链上复核 ✅ … 0xdead → 0xbeef`。现在 `render_a4_ok` 自己先验再渲染。
    """
    import pytest

    from pactpay.demo import a4_line_for

    failed = {"ok": False, "checked": True, "block_number": 48014861,
              "transfers": [{"from": "0xdead", "to": "0xbeef", "value": 1}],
              "why": "链上对不上"}
    with pytest.raises(Exception):
        a4_line_for(_outcome("paid"), failed)

    # 连"没核过"也不行 —— 想打 ✅ 就必须真的核过
    with pytest.raises(Exception):
        a4_line_for(_outcome("paid"), {"ok": True, "checked": False, "block_number": None,
                                       "transfers": [], "why": "没核"})
