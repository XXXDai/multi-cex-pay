"""文档的机械检查：链接不能断，示例命令不能写错。

改标题会改变锚点，很容易漏掉别的文件里指向它的链接。人工核对不可靠，
所以钉在 CI 里。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$", re.M)
INLINE_ANCHOR = re.compile(r'<a\s+id="([^"]+)"', re.I)
FENCE = re.compile(r"```.*?```", re.S)


# 工具生成的目录里也有 .md（比如 .pytest_cache/README.md），
# 把它们算进来会让收集到的用例数随环境变化：本地跑过 pytest 就多 4 项，
# 干净的 CI 上没有。参数化的测试集合必须是确定的。
SKIP_DIRS = {"node_modules", "build", "dist", "site-packages"}


def md_files() -> list[Path]:
    def excluded(path: Path) -> bool:
        for part in path.relative_to(REPO).parts[:-1]:
            if part in SKIP_DIRS or part.endswith(".egg-info"):
                return True
            # 点开头的目录一律跳过（.venv / .git / .pytest_cache / .ruff_cache …），
            # .github 例外，那里可能放 issue 模板之类需要检查的文档
            if part.startswith(".") and part != ".github":
                return True
        return False

    return sorted(p for p in REPO.rglob("*.md") if not excluded(p))


def slugify(text: str) -> str:
    """按 GitHub 的规则把标题转成锚点：小写、空格转连字符、丢标点、保留中日韩字符。"""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\*\*?([^*]*)\*\*?", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    out = []
    for ch in text.strip().lower():
        if ch.isspace():
            out.append("-")
        elif ch in "-_" or ch.isalnum() or unicodedata.category(ch).startswith("L"):
            out.append(ch)
    return "".join(out)


def anchors_of(path: Path) -> set[str]:
    body = FENCE.sub("", path.read_text(encoding="utf-8"))
    return {slugify(m.group(2)) for m in HEADING.finditer(body)} | set(
        INLINE_ANCHOR.findall(body)
    )


@pytest.mark.parametrize("path", md_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_local_links_resolve(path: Path):
    """每条指向仓库内部的链接都要能落到真实的文件和锚点上。"""
    body = FENCE.sub("", path.read_text(encoding="utf-8"))
    broken = []

    for target in LINK.findall(body):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")

        if file_part:
            dest = (path.parent / file_part).resolve()
            if not dest.exists():
                broken.append(f"文件不存在: {target}")
            elif anchor and dest.suffix == ".md" and anchor not in anchors_of(dest):
                broken.append(f"锚点不存在: {target}")
        elif anchor and anchor not in anchors_of(path):
            broken.append(f"本文锚点不存在: #{anchor}")

    assert not broken, f"{path.relative_to(REPO)}:\n  " + "\n  ".join(broken)


def test_readme_does_not_overstate_test_count():
    """README 徽章不能虚报测试数量。

    只查"不高于实际"这一个方向：少报无害（文档写完之后又加了测试很正常），
    多报就是假数据。如果这里也强求接近实际，那每加一篇文档都会因为
    参数化用例变多而让 CI 变红，纯属给贡献者添麻烦。
    """
    import subprocess

    proc = subprocess.run(
        ["python", "-m", "pytest", "--collect-only", "-q", str(REPO / "tests")],
        capture_output=True, text=True, cwd=REPO,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if not match:
        pytest.skip("拿不到 collect 数量")
    actual = int(match.group(1))

    for name in ("README.md", "README.en.md"):
        text = (REPO / name).read_text(encoding="utf-8")
        claimed = re.search(r"tests-(\d+)%20passed", text)
        assert claimed, f"{name} 里找不到测试数徽章"
        # 允许有跳过的用例，所以只要求不高于实际收集数、且差距不大
        n = int(claimed.group(1))
        assert n <= actual, (
            f"{name} 声称 {n} 项测试，实际只收集到 {actual} 项。徽章不能虚报。"
        )


@pytest.mark.parametrize("path", md_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_placeholder_leftovers(path: Path):
    """别把待填的占位符发出去。"""
    body = path.read_text(encoding="utf-8")
    for marker in ("TODO", "FIXME", "XXX:", "你的网关域名/embed.js'"):
        assert marker not in body, f"{path.relative_to(REPO)} 里还留着 {marker}"

@pytest.mark.parametrize("path", md_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_tables_have_consistent_columns(path: Path):
    r"""表格每行的列数要一致，少一根竖线整张表就渲染错位。

    统计时先把行内代码换成占位符（``a|b`` 里的竖线不是列分隔），
    转义的 ``\|`` 也不计。行号按原文算，别在剥掉代码块的文本上数。
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    in_fence = False
    block: list[tuple[int, int, str]] = []
    problems = []

    def flush(rows):
        if len(rows) < 2:
            return
        counts = {c for _, c, _ in rows}
        if len(counts) == 1:
            return
        common = max(counts, key=lambda c: sum(1 for _, x, _ in rows if x == c))
        for no, count, raw in rows:
            if count != common:
                problems.append(
                    f"第 {no} 行有 {count} 根竖线，本表普遍 {common} 根: {raw.strip()[:80]}"
                )

    for no, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        masked = re.sub(r"`[^`]*`", "X", line).replace(r"\|", "")
        if masked.strip().startswith("|"):
            block.append((no, masked.count("|"), line))
        else:
            flush(block)
            block = []
    flush(block)

    assert not problems, f"{path.relative_to(REPO)}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("path", md_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_code_fences_are_balanced(path: Path):
    """代码块围栏必须成对，落单一个后面整篇都会被当成代码。"""
    fences = [
        no for no, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1)
        if line.lstrip().startswith("```")
    ]
    assert len(fences) % 2 == 0, (
        f"{path.relative_to(REPO)} 有 {len(fences)} 个 ``` 围栏，"
        f"最后一个在第 {fences[-1]} 行"
    )
