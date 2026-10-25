"""用户给的「目的」。

整个项目只有一个核心输入：一段自由文本。人格、议题、成果物标题全都由它推出来。

网页端还允许拖入 `.md` / `.txt` 材料，由 `authoring.distill_material` 先提炼成
一段目的再进入这里——材料本身不直接进 system message，所以可以比目的长得多。

刻意**只收一段文本**，不收「领域」「受众」「风格」那一堆结构化字段：
那些东西写进文本里效果一样，而多一个字段就多一个要校验、要解释、要在
网页上画成输入框的东西。真需要时再说。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: 目的长度的上下限。下限挡的是「帮我写点东西」这种推不出任何东西的输入：
#: 提示词再强也变不出人格来，不如当场告诉他写清楚点，别浪费一次模型调用。
MIN_PURPOSE = 8

#: 上限挡的是「顺手粘一整篇文章进去」——目的每一轮都要进 system message，
#: 撑大了成本是成倍翻的，而且会挤掉真正有用的上下文。
MAX_PURPOSE = 2000

MAX_TITLE = 60

#: 网页拖入材料的总长度上限。材料只用于「提炼目的」这一次调用，不会随每个议题、
#: 每一轮进入 system message，所以可以比 MAX_PURPOSE 大得多；但也不能无限大，
#: 否则一次提炼就会把上下文撑爆。
MAX_MATERIAL = 30000

#: 一次最多合并多少个文件。真正的限制是总字数，这个只是防止一次拖进上百个文件。
MAX_MATERIAL_FILES = 20


class InvalidPurpose(ValueError):
    """目的不合法。消息是给人看的一句话，直接打到终端上的。"""


@dataclass(frozen=True)
class Purpose:
    """一段目的，外加一个可选的标题。

    `title` 留空是常态：标题该由模型读完目的之后起（`forge` 就是干这个的），
    这里留一个字段只是给「我就要叫这个名字」的情况用。
    """

    text: str
    title: str = ""

    def summary(self) -> str:
        """终端上那一行。目的可能有好几段，摘第一句就够看了。"""
        first = self.text.strip().splitlines()[0].strip()
        if len(first) > 40:
            first = first[:40] + "…"
        return f"{self.title or '（标题待生成）'} · {first}"


def make_purpose(text: str, title: str = "") -> Purpose:
    """把裸输入变成一个合法的 Purpose，不合法就抛 InvalidPurpose。"""
    if not isinstance(text, str):
        raise InvalidPurpose("目的必须是文本。")

    text = text.strip()
    title = (title or "").strip()

    if not text:
        raise InvalidPurpose("没有写目的。用 --purpose 给一句话说明你想要什么。")
    if len(text) < MIN_PURPOSE:
        raise InvalidPurpose(
            f"目的太短了（{len(text)} 字）：至少 {MIN_PURPOSE} 字，"
            "要说清「给谁、要解决什么、想要什么形态的成果」，否则推不出人格和议题。"
        )
    if len(text) > MAX_PURPOSE:
        raise InvalidPurpose(
            f"目的太长了：上限 {MAX_PURPOSE} 字，你给了 {len(text)} 字。"
            "它会整段进各位专家的 system message，写长了成本是成倍翻的。"
        )
    if len(title) > MAX_TITLE:
        raise InvalidPurpose(f"标题太长了：上限 {MAX_TITLE} 字，你给了 {len(title)} 字。")

    return Purpose(text=text, title=title)


def read_purpose_file(path: Path | str) -> Purpose:
    """从文件读目的。文件里的第一行如果写成 `# 标题`，就当成标题。"""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidPurpose(f"读不出目的文件 {path}：{exc}") from None

    lines = raw.splitlines()
    title = ""
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip().lstrip("#").strip()
        raw = "\n".join(lines[1:])
    return make_purpose(raw, title)
