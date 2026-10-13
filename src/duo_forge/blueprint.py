"""蓝图：`forge` 的产物，也是 `run` 的输入。

两段流程之间唯一的接口就是这个文件。它存在的理由有两个：

1. **钱**。`forge` 要花两次模型调用。议题不满意时，改这个 JSON 再跑就行，
   不用把人格和议题重新生成一遍。
2. **可读**。它是给人看的——`to_markdown` 打出来的那份，把两位专家最终拿到的
   system message 一字不差地摊开（包括由代码追加的发言纪律）。想确认「模型到底
   被要求了什么」，看这份，不用去猜提示词。

刻意存成 JSON 而不是 pickle/自定义格式：它是要被人**动手改**的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .personas import Persona, full_system_message
from .purpose import Purpose
from .topics import CLOSING_KEY, Topic, closing_topic

#: 蓝图文件的格式版本。将来字段变了，靠它认出旧文件并给人话提示。
FORMAT = 1


class BlueprintError(RuntimeError):
    """蓝图文件读不出来或者内容不对。消息直接打到终端上。"""


@dataclass(frozen=True)
class Blueprint:
    purpose: Purpose
    title: str
    slug: str
    #: 恰好两位：方案提出方、挑战者。顺序有语义，不要调换。
    personas: tuple[Persona, Persona]
    #: 正文议题。收尾议题不在这里——它由 `all_topics()` 在末尾补上。
    topics: tuple[Topic, ...]
    #: "mock" / "model"，只用于展示。不参与任何判断。
    origin: str = ""

    @property
    def proposer(self) -> Persona:
        return self.personas[0]

    @property
    def challenger(self) -> Persona:
        return self.personas[1]

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
        return [by_key[k.strip()] for k in only.split(",") if k.strip()] + [self.closing()]

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
        """给人看的那份：两位专家的完整提示词 + 议题表。"""
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
            "## 两位专家",
            "",
            "| | 方案提出方（先开口） | 挑战者（负责挑刺） |",
            "|---|---|---|",
            f"| 称呼 | {self.proposer.name} | {self.challenger.name} |",
            f"| 身份 | {self.proposer.title} | {self.challenger.title} |",
            f"| 负责 | {self.proposer.stance} | {self.challenger.stance} |",
            "",
            "下面这两段，就是他们每一轮实际拿到的 system message 的前半部分"
            "（后面还会拼上【本次议题】和【本轮目标】）。",
            "",
        ]
        for persona, challenger in ((self.proposer, False), (self.challenger, True)):
            lines += [
                f"### {persona.name} · {persona.title}",
                "",
                "```text",
                full_system_message(persona, challenger=challenger),
                "```",
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
                "**开场发言**（方案提出方先开口）：",
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
        path.write_text(self.to_json(), encoding="utf-8")
        return path


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
    if version != FORMAT:
        raise BlueprintError(
            f"{source} 是格式版本 {version} 的蓝图，本程序只认 {FORMAT}。"
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
    if not isinstance(personas_raw, list) or len(personas_raw) != 2:
        raise BlueprintError(
            f"{source} 里的 personas 必须是恰好两位（方案提出方、挑战者）。"
        )
    personas = tuple(_persona(item, source, i) for i, item in enumerate(personas_raw, 1))

    topics_raw = raw.get("topics")
    if not isinstance(topics_raw, list) or not topics_raw:
        raise BlueprintError(f"{source} 里一个议题都没有。")
    topics = tuple(_topic(item, source, i) for i, item in enumerate(topics_raw, 1))

    return Blueprint(
        purpose=purpose,
        title=title,
        slug=slug,
        personas=personas,  # type: ignore[arg-type]
        topics=topics,
        origin=str(raw.get("origin") or ""),
    )


def _text(raw: dict, name: str, source: str, *, where: str = "") -> str:
    value = raw.get(name)
    if not isinstance(value, str) or not value.strip():
        at = f"{source} 的 {where}" if where else source
        raise BlueprintError(f"{at} 里缺少 {name}。")
    return value.strip()


def _persona(raw: object, source: str, index: int) -> Persona:
    if not isinstance(raw, dict):
        raise BlueprintError(f"{source} 的第 {index} 位专家不是 JSON 对象。")
    return Persona(
        name=_text(raw, "name", source, where=f"第 {index} 位专家"),
        title=_text(raw, "title", source, where=f"第 {index} 位专家"),
        stance=_text(raw, "stance", source, where=f"第 {index} 位专家"),
        system_message=_text(raw, "system_message", source, where=f"第 {index} 位专家"),
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
