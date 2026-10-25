"""蓝图：`forge` 的产物，也是 `run` 的输入。

两段流程之间唯一的接口就是这个文件。它存在的理由有两个：

1. **钱**。`forge` 要花几次模型调用。议题不满意时，改这个 JSON 再跑就行，
   不用把人格和议题重新生成一遍。
2. **可读**。它是给人看的——`to_markdown` 打出来的那份，把每位参与者最终拿到的
   system message 一字不差地摊开（包括由代码追加的发言纪律）。想确认「模型到底
   被要求了什么」，看这份，不用去猜提示词。

刻意存成 JSON 而不是 pickle/自定义格式：它是要被人**动手改**的。

## format 2 与旧文件

format 2 的 `personas` 是 2–5 位的数组，每位多一个 `role`（AI 起的短角色名）。
format 1 的旧蓝图仍然能读：它必须是两位，角色按位置补成「主提出方 / 挑战者」。
人数不单独存字段，由 `len(personas)` 推导，避免两处不一致。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .personas import (
    MAX_ROLE,
    MAX_PEOPLE,
    MIN_PEOPLE,
    Persona,
    default_role,
    full_system_message,
)
from .purpose import Purpose
from .topics import CLOSING_KEY, Topic, closing_topic

#: 蓝图文件的格式版本。format 2 起 personas 支持 2–5 位并带 role。
FORMAT = 2

#: 仍然能读的旧版本。读进来之后统一按 format 2 的 `Blueprint` 表示。
LEGACY_FORMAT = 1


class BlueprintError(RuntimeError):
    """蓝图文件读不出来或者内容不对。消息直接打到终端上。"""


@dataclass(frozen=True)
class Blueprint:
    purpose: Purpose
    title: str
    slug: str
    #: 2–5 位。顺序有语义：第 1 位是主提出方（开场），其余是回应者。
    personas: tuple[Persona, ...]
    #: 正文议题。收尾议题不在这里——它由 `all_topics()` 在末尾补上。
    topics: tuple[Topic, ...]
    #: "mock" / "model"，只用于展示。不参与任何判断。
    origin: str = ""

    def __post_init__(self) -> None:
        count = len(self.personas)
        if not (MIN_PEOPLE <= count <= MAX_PEOPLE):
            raise BlueprintError(
                f"蓝图必须要有 {MIN_PEOPLE}–{MAX_PEOPLE} 位专家，收到 {count} 位。"
            )
        names = [p.name.strip() for p in self.personas]
        if len(set(names)) != len(names):
            raise BlueprintError(
                "蓝图里有重名的专家："
                + "、".join(sorted({n for n in names if names.count(n) > 1}))
                + "。称呼必须唯一，否则发言记录分不清谁是谁。"
            )
        for index, persona in enumerate(self.personas, 1):
            if persona.role and len(persona.role) > MAX_ROLE:
                raise BlueprintError(
                    f"第 {index} 位专家的角色名太长：上限 {MAX_ROLE} 字，"
                    f"给了 {len(persona.role)} 字。"
                )

    @property
    def proposer(self) -> Persona:
        """第 1 位，结构上的主提出方。保留这个名字是为了少改老调用点。"""
        return self.personas[0]

    @property
    def challenger(self) -> Persona:
        """第 2 位。两人模式下是挑战者；多人模式下只是第一位回应者。"""
        return self.personas[1]

    @property
    def people(self) -> int:
        return len(self.personas)

    def closing(self) -> Topic:
        return closing_topic(len(self.topics), self.title)

    def all_topics(self) -> list[Topic]:
        return [*self.topics, self.closing()]

    def select(self, only: str = "") -> list[Topic]:
        """`--only a,b` 的语义：只留指定的议题，收尾议题始终在最后。

        未知 key 当场报错并列出可选的——这个项目的 key 是模型生成的，
        拼错一个字母的可能性比手写议题表时大得多。
        """
        if not only:
            return self.all_topics()

        by_key = {t.key: t for t in self.topics}
        unknown = [k.strip() for k in only.split(",") if k.strip() not in by_key]
        if unknown:
            valid = "、".join(t.key for t in self.topics)
            raise BlueprintError(
                f"蓝图《{self.title}》里没有这几个议题：{', '.join(unknown)}。\n"
                f"  可选：{valid}"
            )
        selected = [by_key[k.strip()] for k in only.split(",") if k.strip()]
        # 收尾议题的开场白要说「我们前面谈了 N 个议题」，这个 N 必须是**本次选中**的
        # 数量，不是蓝图里的总数——`--only` 只跑一个议题时，说成全部会说谎。
        return selected + [closing_topic(len(selected), self.title)]

    # ---- 序列化 ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "origin": self.origin,
            "title": self.title,
            "slug": self.slug,
            "purpose": {"text": self.purpose.text, "title": self.purpose.title},
            "personas": [
                {
                    "role": p.role,
                    "name": p.name,
                    "title": p.title,
                    "stance": p.stance,
                    "system_message": p.system_message,
                }
                for p in self.personas
            ],
            "topics": [
                {
                    "key": t.key,
                    "section_title": t.section_title,
                    "brief": t.brief,
                    "goal": t.goal,
                    "opening": t.opening,
                }
                for t in self.topics
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"

    def to_markdown(self) -> str:
        """给人看的那份：全部参与者的完整提示词 + 议题表。"""
        count = len(self.personas)
        heading = "两位专家" if count == 2 else f"{count} 位专家"
        lines = [
            f"# 蓝图 · {self.title}",
            "",
            f"> 由目的生成（{self.origin or '未知来源'}），可以手改。",
            f"> 改完用 `duo-forge run --blueprint <本文件的 .json>` 跑。",
            "",
            "## 目的",
            "",
            self.purpose.text.strip(),
            "",
            f"## {heading}",
            "",
        ]
        for index, persona in enumerate(self.personas, 1):
            if index == 1:
                position = "第 1 位 · 主提出方（开场）"
            else:
                position = f"第 {index} 位 · 回应者"
            lines += [
                f"- **{position}**：{persona.name}（{persona.title}），"
                f"角色「{persona.role or '专家'}」，负责「{persona.stance}」"
            ]
        lines += [
            "",
            "下面这几段，就是他们每一轮实际拿到的 system message 的前半部分"
            "（后面还会拼上【本次议题】和【本轮目标】）。",
            "",
        ]
        for index, persona in enumerate(self.personas, 1):
            message = full_system_message(persona, opener=(index == 1))
            # 用户手写的人格可能自带代码围栏；固定三反引号会被它提前闭合。
            fence = _fence_for(message)
            position = "主提出方" if index == 1 else f"第 {index} 位"
            display = persona.role or persona.title
            heading = (
                f"### {persona.name}（{position}）"
                if display == position
                else f"### {persona.name} · {display}（{position}）"
            )
            lines += [
                heading,
                "",
                f"{fence}text",
                message,
                fence,
                "",
            ]

        lines += [f"## 议题（{len(self.topics)} 个正文 + 1 个收尾）", ""]
        closing = self.closing()
        for index, topic in enumerate([*self.topics, closing], 1):
            tag = "（收尾，代码生成）" if topic.key == CLOSING_KEY else ""
            lines += [
                f"### {index}. [{topic.key}] {topic.section_title}{tag}",
                "",
                f"- **争的是哪一点**：{topic.brief}",
                f"- **要收敛出什么**：{topic.goal}",
                "",
                "**开场发言**（第 1 位 · 主提出方先开口）：",
                "",
                *[f"> {line}" if line else ">" for line in topic.opening.splitlines()],
                "",
            ]
        return "\n".join(lines)

    def save(self, out_dir: Path, *, stamp: str = "") -> Path:
        """把蓝图落盘成 JSON，返回路径。"""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"{self.slug}-{stamp}.json"
        # 人格都手写时 forge 只跑一次议题调用，连续两次很可能落在同一秒。
        # 不覆盖别人刚写出来的蓝图。
        if path.exists():
            suffix = 2
            while True:
                candidate = out_dir / f"{self.slug}-{stamp}-{suffix}.json"
                if not candidate.exists():
                    path = candidate
                    break
                suffix += 1
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    def save_bundle(self, out_dir: Path, *, stamp: str = "") -> tuple[Path, Path]:
        """JSON + 同名可读版一起落盘，返回两个路径。

        CLI 和网页启动器都要这一对文件；规则只留这一份，免得一边写了 .md、
        另一边忘了。
        """
        json_path = self.save(out_dir, stamp=stamp)
        md_path = json_path.with_suffix(".md")
        md_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, md_path


def _fence_for(text: str) -> str:
    """生成一段比正文里最长反引号串更长（至少三个）的围栏。"""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def load(path: Path | str) -> Blueprint:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BlueprintError(f"读不出蓝图文件 {path}：{exc}") from None
    except json.JSONDecodeError as exc:
        raise BlueprintError(
            f"蓝图文件不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"
        ) from None
    return from_dict(raw, source=str(path))


def from_dict(raw: object, *, source: str = "蓝图") -> Blueprint:
    if not isinstance(raw, dict):
        raise BlueprintError(f"{source} 的顶层必须是一个 JSON 对象。")

    version = raw.get("format", FORMAT)
    if version not in (LEGACY_FORMAT, FORMAT):
        raise BlueprintError(
            f"{source} 是格式版本 {version} 的蓝图，本程序只认 "
            f"{LEGACY_FORMAT}（旧版）和 {FORMAT}。"
        )

    title = _text(raw, "title", source)
    slug = _text(raw, "slug", source) or "duo"

    purpose_raw = raw.get("purpose")
    if not isinstance(purpose_raw, dict):
        raise BlueprintError(f"{source} 里缺少 purpose。")
    purpose = Purpose(
        text=_text(purpose_raw, "text", source, where="purpose"),
        title=str(purpose_raw.get("title") or "").strip(),
    )

    personas_raw = raw.get("personas")
    if not isinstance(personas_raw, list):
        raise BlueprintError(f"{source} 里的 personas 必须是一个数组。")
    if version == LEGACY_FORMAT and len(personas_raw) != 2:
        raise BlueprintError(
            f"{source} 是格式版本 1 的蓝图，personas 必须是恰好两位。"
        )
    if not (MIN_PEOPLE <= len(personas_raw) <= MAX_PEOPLE):
        raise BlueprintError(
            f"{source} 里的 personas 必须有 {MIN_PEOPLE}–{MAX_PEOPLE} 位，"
            f"收到 {len(personas_raw)} 位。"
        )
    personas = tuple(
        _persona(item, source, i, legacy=(version == LEGACY_FORMAT))
        for i, item in enumerate(personas_raw, 1)
    )

    topics_raw = raw.get("topics")
    if not isinstance(topics_raw, list) or not topics_raw:
        raise BlueprintError(f"{source} 里一个议题都没有。")
    topics = tuple(_topic(item, source, i) for i, item in enumerate(topics_raw, 1))

    try:
        return Blueprint(
            purpose=purpose,
            title=title,
            slug=slug,
            personas=personas,
            topics=topics,
            origin=str(raw.get("origin") or ""),
        )
    except BlueprintError as exc:
        raise BlueprintError(f"{source}：{exc}") from None


def _text(raw: dict, name: str, source: str, *, where: str = "") -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        at = f"{source} 的 {where}" if where else source
        raise BlueprintError(f"{at} 里缺少 {name}。")
    return value.strip()


def _optional_line(value: object, label: str, limit: int) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise BlueprintError(f"{label}必须是一段文本。")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise BlueprintError(f"{label}必须是单行。")
    if len(text) > limit:
        raise BlueprintError(
            f"{label}太长：上限 {limit} 字，给了 {len(text)} 字。"
        )
    return text


def _persona(
    raw: object, source: str, index: int, *, legacy: bool = False
) -> Persona:
    if not isinstance(raw, dict):
        raise BlueprintError(f"{source} 的第 {index} 位专家不是 JSON 对象。")
    where = f"第 {index} 位专家"
    role = _optional_line(raw.get("role"), f"{source} 的{where}角色名", MAX_ROLE)
    if not role and legacy:
        role = default_role(index, 2)
    return Persona(
        name=_text(raw, "name", source, where=where),
        title=_text(raw, "title", source, where=where),
        stance=_text(raw, "stance", source, where=where),
        system_message=_text(raw, "system_message", source, where=where),
        role=role,
    )


def _topic(raw: object, source: str, index: int) -> Topic:
    if not isinstance(raw, dict):
        raise BlueprintError(f"{source} 的第 {index} 个议题不是 JSON 对象。")
    where = f"第 {index} 个议题"
    key = _text(raw, "key", source, where=where)
    if key == CLOSING_KEY:
        raise BlueprintError(
            f"{source} 的第 {index} 个议题用了保留的 key「{CLOSING_KEY}」"
            "——收尾议题由代码生成，蓝图里不要放。"
        )
    return Topic(
        key=key,
        section_title=_text(raw, "section_title", source, where=where),
        brief=_text(raw, "brief", source, where=where),
        goal=_text(raw, "goal", source, where=where),
        opening=_text(raw, "opening", source, where=where),
    )
