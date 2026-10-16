"""仓库整洁守卫：源码文件不能被 `.gitignore` 吞掉。

**为什么要有这个测试**（B1，第 7 步评审判 FAIL 的最严重一条）：
`.gitignore` 里原本写的是 `evidence/`（**没有前导斜杠**），它在 gitignore 语义里匹配
**任意层级**的同名目录 —— 于是把包目录 `pactpay/evidence/` 一起吞了，
新写的 `pactpay/evidence/verify.py` **根本没进提交**。
本地一切正常（文件就在磁盘上），**克隆出来却连测试都收集不起来**（0 条执行）。

这类缺陷的可怕之处在于：**所有本地绿灯都是真的，但交付物是坏的**。
所以不能只修一次了事，要有一条**能持续发现它**的守卫。
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def _require_git_repo() -> None:
    """不在 git 仓库里就 **skip**，而不是让测试报错。

    **为什么**（干净克隆模拟暴露）：本文件里有几条守卫要读 git 状态，而
    `git status` / `git ls-files` 在**没有 `.git`** 的目录里退出码是 128。
    源码 tarball、`git archive` 导出、或某些 CI 的打包场景都属于这种"有代码没仓库"，
    那时 `CalledProcessError` 会让三条守卫**假红**——守卫本身成了噪音源。
    真正该被它抓的（源码被 gitignore 吞掉）只在有仓库时才有意义，所以 skip 是对的。
    """
    try:
        _git("rev-parse", "--git-dir")
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("当前目录不是 git 仓库（如源码 tarball / git archive 导出），跳过 git 相关守卫")


def _tracked() -> set[str]:
    return {line for line in _git("ls-files").splitlines() if line}


def _ignored() -> set[str]:
    out = _git("status", "--ignored", "--porcelain")
    return {
        line[3:].strip().rstrip("/")
        for line in out.splitlines()
        if line.startswith("!!")
    }


def test_no_source_file_is_gitignored():
    """`pactpay/` 与 `tests/` 下的每个 `.py` 都必须被 git 跟踪。

    被忽略的源码不会进提交，克隆出来就是残缺的 —— 而本地测试**照样全绿**。
    """
    _require_git_repo()
    ignored = _ignored()
    on_disk = {
        p.relative_to(REPO).as_posix()
        for root in ("pactpay", "tests")
        for p in (REPO / root).rglob("*.py")
        if "__pycache__" not in p.parts
    }

    swallowed = sorted(
        f for f in on_disk
        if any(f == ig or f.startswith(ig + "/") for ig in ignored)
    )

    assert not swallowed, f"这些源码文件被 .gitignore 吞掉了，不会进提交：{swallowed}"


def test_every_source_file_is_actually_committed():
    """光"没被忽略"还不够：没 `git add` 一样不在提交里。

    这是 B1 的另一半 —— 文件既没被忽略、也没被提交，`git status` 只会显示成
    未跟踪，很容易被 "工作区干净" 的错觉盖过去。
    """
    _require_git_repo()
    tracked = _tracked()
    on_disk = {
        p.relative_to(REPO).as_posix()
        for root in ("pactpay", "tests")
        for p in (REPO / root).rglob("*.py")
        if "__pycache__" not in p.parts
    }

    missing = sorted(on_disk - tracked)

    assert not missing, f"这些源码文件没有被提交（`git add` 漏了）：{missing}"


def test_gitignore_does_not_swallow_a_source_package():
    """`.gitignore` 里的规则**不许**匹配到交付源码（B1 的根因守卫）。

    B1 的真相是：`.gitignore` 写了 `evidence/`（**没有前导斜杠**），
    这条规则在任何层级都匹配 —— 于是 `pactpay/evidence/` 整个包被吞掉，
    `verify.py` 从没被提交过。而"已提交文件列表"那条守卫**抓不到它**：
    文件一旦 tracked，加不加 ignore 都不影响 `git ls-files`。
    必须直接检查**规则与源码路径的匹配关系**，才能防住复发。
    """
    import fnmatch

    rules = [
        line.strip()
        for line in (REPO / ".gitignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    sources = [
        p.relative_to(REPO).as_posix()
        for root in ("pactpay", "tests")
        for p in (REPO / root).rglob("*.py")
        if "__pycache__" not in p.parts
    ]

    swallowed: list[str] = []
    for rel in sources:
        parts = rel.split("/")
        for rule in rules:
            r = rule.lstrip("/").rstrip("/")
            if not r:
                continue
            # 无前导斜杠的规则在任何层级都匹配；有前导斜杠的只匹配仓库根
            if rule.startswith("/"):
                hit = fnmatch.fnmatch(rel, r) or rel == r
            else:
                hit = any(fnmatch.fnmatch(seg, r) for seg in parts) or fnmatch.fnmatch(rel, r)
            if hit:
                swallowed.append(f"{rel} 被规则 {rule!r} 匹配")

    assert not swallowed, (
        "`.gitignore` 规则会吞掉交付源码（B1 复发的形态）：\n" + "\n".join(swallowed)
    )


def test_clone_can_run_the_test_suite():
    """端到端守卫：**真的克隆一份**，在克隆里跑一遍测试收集。

    前两条测的是"规则对不对"，这条测的是"交付物到底能不能用" —— B1 只有在克隆里才会现形。
    """
    _require_git_repo()
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    try:
        clone = tmp / "clone"
        subprocess.run(
            ["git", "clone", "-q", "--no-hardlinks", str(REPO), str(clone)],
            check=True, capture_output=True, text=True,
        )
        # **用当前解释器，不许硬编码 `.venv/bin/python`**（评审测试质量 #5）：
        # 真正的干净 clone 里没有 `.venv/`（它是 gitignore 的），硬编码路径会让
        # 这条"克隆守卫"自己 FileNotFoundError —— 一条依赖宿主环境的守卫，
        # 恰恰违背了 B1 那条教训（交付物必须自证可用）。
        import sys as _sys

        proc = subprocess.run(
            [_sys.executable, "-m", "pytest", "tests/", "-q",
             "--collect-only", "-p", "no:cacheprovider"],
            cwd=clone, capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            "克隆出来的仓库跑不了测试 —— 说明有源码文件没进提交：\n"
            f"{proc.stdout[-3000:]}\n{proc.stderr[-1500:]}"
        )
        # 只认 pytest 的**收集失败标记**。不要用 `"error" in stdout` 这种子串判断——
        # 测试名里就带 "errors"（`test_other_errors_are_not_counted_as_invalidation`），
        # 会把正常输出误判成失败（本守卫第一版就踩了这个）。
        for marker in ("errors during collection", "Interrupted: ", "ERROR "):
            assert marker not in proc.stdout, (
                f"克隆出来的仓库测试收集失败（命中 {marker!r}）：\n{proc.stdout[-3000:]}"
            )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_duplicate_string_keys_in_dict_literals():
    """字典字面量里不许有重复的字符串键。

    **为什么要有这条**（B7，第 7 步评审阻断项）：`demo.py` 里 A5c 证据块把 `policy_id`
    写了两遍（探针拿到的那个 + 审计行那个），Python 让**后者静默覆盖前者** ——
    于是"A5a 的三方交叉核对"在产物里被销毁，而且 `git diff` 和类型检查都看不出来。
    这类缺陷编译器不报、运行时也不报，只能靠静态检查挡。
    """
    import ast

    duplicates: list[str] = []
    for root in ("pactpay", "tests"):
        for path in (REPO / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    keys = [
                        k.value for k in node.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    ]
                    repeated = sorted({k for k in keys if keys.count(k) > 1})
                    if repeated:
                        rel = path.relative_to(REPO).as_posix()
                        duplicates.append(f"{rel}:{node.lineno} 重复键 {repeated}")

    assert not duplicates, "字典字面量里有重复字符串键（后者会静默覆盖前者）：\n" + "\n".join(duplicates)


def test_production_modules_do_not_import_from_the_spike_package():
    """生产路径不许依赖 `pactpay/spike/`。

    **为什么要有这条**（B9，第 7 步评审阻断项）：原先 `demo.py` / `pact_check.py` /
    `complete_check.py` / `cumulative_check.py` 都从 `spike/t0_probe.py` 取配置，
    连 `build_x402_pact_spec` 都住在 `spike/a0e_probe.py` 里 —— 依赖方向是反的。
    探针的意义就是"可以随时删掉、随便重写"；一旦生产命令 import 它，它就删不动了，
    而且读代码的人会以为"正式流程要跑探针"。真正的共享底座是 `config` / `pact_spec`。
    """
    offenders: list[str] = []
    for path in (REPO / "pactpay").rglob("*.py"):
        if "__pycache__" in path.parts or "spike" in path.relative_to(REPO / "pactpay").parts:
            continue
        rel = path.relative_to(REPO).as_posix()
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if "pactpay.spike" in line and line.lstrip().startswith(("import ", "from ")):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, "生产模块依赖了 spike 包（依赖方向反了）：\n" + "\n".join(offenders)


def test_readme_lists_every_module_under_pactpay():
    """README 的目录结构必须列全 `pactpay/` 下的模块。

    **为什么要有这条**（B10）：README 漏列了 5 个模块、独立验收入口也漏了新增的 4 个，
    因为 A11b 那条"按 README 走一遍"被跳过了，**没有人真的照 README 做过**。
    文档漂移靠人盯是盯不住的，得让它变成会红的测试。
    """
    import re

    readme = (REPO / "README.md").read_text()
    # **必须按"目录树里的一个条目"来比对，不能只查子串**（评审 M29）：
    # 原来用 `path.name not in readme`，把 README 里的行改成 `config.py.bak`
    # 仍然通过 —— 名字还在，但它已经不是那个文件了。子串存在性冒充不了结构检查。
    #
    # **第 8 步改成比相对路径**：`pactpay/` 按职责分了子包以后，光比文件名会撞车
    # （`payee/app.py` 与 `dashboard/app.py` 都叫 `app.py`），而且"列出了 app.py"
    # 完全可能是列在**别的目录**下面。所以现在把条目所在的目录一起解析出来，
    # 拼成相对路径再比 —— **目录层次本身就是被检查的东西**。
    listed: set[str] = set()
    current_dir: list[str] = []
    for line in readme.splitlines():
        dir_match = re.match(r"^[\s│├└─|]*([A-Za-z_]\w*)/\s", line)
        if dir_match:
            current_dir = [dir_match.group(1)]
            continue
        # 文件名允许带斜杠：嵌套更深时 README 里直接写 `demo/payee/app.py`
        file_match = re.match(r"^[\s│├└─|]*([A-Za-z_][\w./]*\.py)\s", line)
        if file_match and current_dir:
            entry = file_match.group(1)
            listed.add(entry if "/" in entry else "/".join(current_dir + [entry]))

    expected = {
        str(path.relative_to(REPO / "pactpay"))
        for path in (REPO / "pactpay").rglob("*.py")
        if path.name != "__init__.py"
    }
    missing = sorted(expected - listed)

    assert not missing, f"README 目录结构漏列这些模块：{missing}（已列出：{sorted(listed)}）"


def test_readme_does_not_reference_make_targets_that_do_not_exist():
    """README / .env.example 里提到的 `make <目标>` 必须真实存在。

    `.env.example` 曾让用户跑 `make t0-provision` —— 这个目标**根本不存在**，
    照着文档做的人第一步就断了。
    """
    import re

    makefile = (REPO / "Makefile").read_text()
    defined = set(re.findall(r"^([a-zA-Z][\w-]*):", makefile, re.M))

    referenced: list[str] = []
    for name in ("README.md", ".env.example"):
        path = REPO / name
        if not path.exists():
            continue
        for target in re.findall(r"make ([a-zA-Z][\w-]*)", path.read_text()):
            if target not in defined:
                referenced.append(f"{name}: make {target}")

    assert not referenced, "文档提到不存在的 make 目标：\n" + "\n".join(sorted(set(referenced)))


def test_boundary_check_reads_its_lexicon_from_the_makefile():
    """词表必须**从 Makefile 真读**，不能在测试里另抄一份。

    上一版这条测试把正则写死在自己内部（`re.compile(r"\\b(defi|...)\\b")`），
    **从不读 Makefile 的实际 pattern** —— 于是把 Makefile 里的 `\\b` 删掉
    （变异 M20）它照样绿。**在被守护对象之外重写一遍规则再断言自己**，
    这不是行为测试，是"变更检测器的赝品"。
    """
    import re

    makefile = (REPO / "Makefile").read_text()
    match = re.search(r'grep -rniE "([^"]+)"', makefile)

    assert match, "Makefile 里找不到边界检查用的 grep pattern"
    pattern = match.group(1)
    assert r"\b" in pattern, f"词表缺词边界（`defi` 会匹配到 `defined`）：{pattern}"

    lexicon = re.compile(pattern, re.I)
    assert not lexicon.search("defined = set()"), "`defined` 被误判成越界词汇"
    assert not lexicon.search("# definition of the term"), "`definition` 被误判成越界词汇"
    assert lexicon.search("a DeFi strategy"), "真越界词 DeFi 反而没被命中"
    assert lexicon.search("an agent market"), "真越界词 agent market 反而没被命中"


def test_boundary_check_actually_fails_on_a_real_violation():
    """**阳性对照**：往交付代码里塞一个真越界词，`make boundary-check` 必须变红。

    没有这一条，前面那条"词表看起来对"的断言毫无意义 ——
    实测已经吃过一次：检查恒红（守卫测试自己含越界词）而守卫绿着，
    两边各说各话。**一个检查必须先证明它抓得住真的违规，才配谈它抓得住什么。**
    """
    import subprocess

    # **在被扫描的东西上做实验，而不是在仓库里**（评审第 4 轮 F12）：
    # 原先这里往 `pactpay/_boundary_probe.py` 写文件，跑一次 pytest 就**临时改动工作区**
    # —— 一个测试不该有副作用，更不该往被审查的交付树里写东西。
    # 现在把"被扫描的树"指向临时目录：同一个检查、同样的违规词，仓库纹丝不动。
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "pactpay"
        target.mkdir()
        (target / "_boundary_probe.py").write_text(
            "# temporary probe for the boundary check\nSTRATEGY = 'a DeFi yield strategy'\n"
        )
        proc = subprocess.run(
            ["make", "boundary-check", f"BOUNDARY_TARGET={target}"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    assert proc.returncode != 0, (
        "塞进真实的越界词后 `make boundary-check` 仍然通过 —— 这个检查是假的。\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "_boundary_probe" in proc.stdout or "_boundary_probe" in proc.stderr


def test_boundary_check_is_green_on_the_real_tree():
    """干净树上也必须是绿的 —— 否则它就是个恒红的噪音源（N1 的形态）。"""
    import subprocess

    proc = subprocess.run(["make", "boundary-check"], cwd=REPO, capture_output=True, text=True)

    assert proc.returncode == 0, (
        "`make boundary-check` 在干净工作区上失败 —— 恒红的检查等于没有检查。\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_demo_does_not_short_circuit_the_paid_guard():
    """**接线处**也要守：A4 守卫的**调用点**不许加短路条件（评审 M4b）。

    上一轮我只测了守卫**函数**（`judge_paid_requires_onchain`），
    于是把 demo 里的调用改回
    `if not a4["ok"] and outcome.settlement_tx: raise ...`
    ——**209 个用例全绿**。函数测了，接线没测，逃逸口就还在。

    demo 的 `_run` 要联网、要花钱，没法在单测里跑；但它是一段**我们自己的源码**，
    可以用结构断言守住：不许出现"用 settlement_tx 给这层判断做短路"的写法。
    """
    import re

    src = (REPO / "pactpay/demo/run.py").read_text()

    # **只看代码行，跳过注释。** demo 里那段注释**必须**写出这个错误写法
    # （它解释了为什么不能这么写）；把注释也算成违规，就等于逼着人删掉解释。
    offenders = []
    for n, line in enumerate(src.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if re.search(r'a4\["ok"\]\s+and\s+.*settlement_tx', code) or re.search(
            r'outcome\.outcome\s*==\s*"paid"\s+and\s+.*settlement_tx', code
        ):
            offenders.append(f"demo.py:{n}: {line.strip()}")

    assert not offenders, (
        "A4 守卫的调用点被短路了（'记成 paid 但没哈希'会被静默跳过）：\n" + "\n".join(offenders)
    )


def test_boundary_check_scans_only_the_shipped_package():
    """R12 的检查必须**只扫交付代码**，否则这个目标会恒红（评审 N1）。

    我上一轮踩过：守卫测试里必须**字面写出**越界词才能测词表，
    一旦这个目标把 `tests/` 也扫进去，它就永远失败 —— 而我在同一份账本里
    刚写过"一个永远失败的检查等于没有检查"。
    """
    mk = (REPO / "Makefile").read_text()
    recipe = mk.split("boundary-check:", 1)[1].split("\n\n", 1)[0]
    # **只留真正执行的命令行，丢掉 `@#` 注释。** 那段注释正是在解释
    # "为什么不扫 tests/"——把注释算进去就成了"解释自己被判违规"。
    commands = "\n".join(
        line for line in recipe.splitlines()
        if line.strip() and not line.lstrip().lstrip("@").lstrip().startswith("#")
    )

    # 扫描目标现在是 `$(BOUNDARY_TARGET)`（默认 `pactpay`，让阳性对照能指向临时目录）。
    assert "$(BOUNDARY_TARGET)/" in commands or "pactpay/" in commands, (
        "边界检查没有扫交付代码目录"
    )
    assert "BOUNDARY_TARGET ?= pactpay" in (REPO / "Makefile").read_text(), (
        "BOUNDARY_TARGET 的默认值不是 pactpay —— 检查可能扫错地方"
    )
    assert "--include=*.py" in commands, "边界检查没有限定文件类型"
    assert "tests/" not in commands, (
        "边界检查把 tests/ 也扫了 —— 守卫测试里必须字面写出越界词，"
        "扫它会让这个目标恒红（N1 复发）"
    )
    assert "\\b" in commands, "缺少词边界，`defi` 会匹配 `defined`/`definition`"


def test_pactpay_keeps_its_responsibility_subpackages():
    """`pactpay/` 必须保持按职责分的子包 —— 不许再摊回一个大平层。

    **为什么要钉住**（第 8 步）：26 个模块平铺在一个目录里时，光看文件名分不出
    谁是谁、谁是底层谁是展示层；README 里那种"按职责分组"的目录树是**画出来**的，
    仓库里并不存在。现在分组是真的了，就得让它**不能悄悄退化**：
    新模块如果直接扔在 `pactpay/` 顶层，这条会红。

    **顶层只允许放包**，不放实现模块。
    """
    top_level_files = sorted(
        p.name
        for p in (REPO / "pactpay").glob("*.py")
        if "__pycache__" not in p.parts
    )
    assert top_level_files == ["__init__.py"], (
        f"`pactpay/` 顶层只应有 `__init__.py`，但发现：{top_level_files}\n"
        "新模块请放进对应职责的子包（platform/policy/agent/evidence/"
        "governance/checks/demo/spike）。"
    )

    expected = {
        "platform": {"cobo.py", "config.py", "onchain.py"},
        "policy": {"spec.py", "check.py"},
        "agent": {"loop.py", "tools.py", "planner.py", "llm_planner.py",
                  "run.py", "pay.py", "preconditions.py"},
        "evidence": {"__init__.py", "verify.py", "block.py"},
        "governance": {"revoke.py", "events.py"},
        "checks": {"complete.py", "cumulative.py"},
        "demo": {"run.py", "payee_lifecycle.py"},
    }
    for pkg, want in expected.items():
        got = {p.name for p in (REPO / "pactpay" / pkg).glob("*.py")}
        assert want <= got, f"`pactpay/{pkg}/` 少了这些模块：{sorted(want - got)}（实际：{sorted(got)}）"
