# camel-duo-forge

给**一个目的**，先让模型锻造出两位立场对立的专家和他们要吵的议题，再让这两位对谈出一份成果。

```bash
uv run duo-forge forge --purpose "给刚上班、每天坐十小时的程序员写一份颈椎保养手册"
uv run duo-forge run   --blueprint blueprints/neck-care-20261013-101500.json
```

它和 [camel-fitness-nutrition-duo](../camel-fitness-nutrition-duo) 是同一套骨架：那边的人格和议题是**手写死的**（营养师 × 教练、五个健身议题），这边把它们换成**按目的生成**。骨架、编排、整理环节、离线模拟都是搬过来的。

---

## 它做两件事

| | 命令 | 干什么 |
|---|---|---|
| **功能一** | `forge` | 按目的调用模型，生成**两位对谈智能体的人格提示词** |
| **功能二** | `forge` | 按目的调用模型，**自动生成相关议题** |
| 顺带 | `run` | 用生成的人格和议题跑完整流程：对谈 → 整理 → 出成果 + 一页速查 |

`forge` 的产物是一份**蓝图**（`blueprints/*.json`），旁边配一份 `.md`：

- **JSON 那份是给机器、也给人改的**。议题不满意就改它，再 `run`，不用重新生成一遍（生成要花两次模型调用）。
- **Markdown 那份是给人看的**：两位专家最终拿到的 system message 一字不差地摊开在里面——包括由代码追加的发言纪律。想知道「模型到底被要求了什么」，看这份，不用去猜提示词。

---

## 两位专家是怎么来的

`forge` 会要求模型设计出**立场真的对立**的两个人：一位提方案（`proposer`），一位挑刺（`challenger`）。最常用的分歧轴是「什么是对的」对「现实中做得到吗」，但轴本身按目的决定。

每人要写清三块：【人格】【专业立场】【你的盲区，不要掩饰】。最后一块是对话有没有张力的关键——挑刺的那位戳到盲区时，提方案的那位得接得住，而不是绕开。

**发言纪律不归模型写**，由代码统一追加。因为其中两条是机器契约，不能指望模型每次都写对：

- 「没有新的反对意见时只回【无异议】」——提前收手全靠它。模型漏写一次**不会有任何报错**，只会让每个议题都闷头跑满轮数（慢、贵，而且看不出来）。
- 「每轮 250 字以内」——直接关系到成本。

议题同理：生成出来的只是**正文议题**，收尾议题（分歧备忘 · 红线 · 自检清单）由代码追加——它要把前面所有对话一起读进去才写得出来，形状和普通议题本来就不一样。

---

## 快速开始

需要 Python 3.10–3.14 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                                       # 1. 装依赖
cp .env.example .env                          # 2. 填 DEEPSEEK_API_KEY
uv run duo-forge forge --purpose "..." --mock # 3. 离线试跑（不花钱，出的是占位内容）
uv run duo-forge forge --purpose "..."        # 4. 正式生成
uv run duo-forge run --blueprint blueprints/xxx.json
```

跑完在 `outputs/` 下得到三个文件：

| 文件 | 内容 |
|---|---|
| `handbook-<时间戳>-<slug>.md` | **最终成果** |
| `quick-<时间戳>-<slug>.md` | **一页速查版**，从上面那份压出来的 |
| `transcript-<时间戳>-<slug>.md` | 两位专家的完整对话实录 |

模拟模式产出的文件带 `.mock` 后缀、开头有醒目警告——**那份是占位内容，没有参考价值**，用途只是让你确认代码链路是通的。

---

## 其他入口

```bash
uv run duo-forge forge --purpose-file my.txt    # 目的写在文件里（首行 `# 标题` 可当标题）
uv run duo-forge forge --purpose "..." --topics 7 --title 我的手册
uv run duo-forge show --blueprint blueprints/xxx.json   # 摊开看一份蓝图（不调用模型）
uv run duo-forge run --blueprint xxx.json --only sleep-debt,reality
uv run duo-forge run --blueprint xxx.json --rounds 5 --no-converge
```

`--rounds` 是**上限**：挑刺那位提不出新的反对意见时，那个议题就提前收手（终端和实录里都会写明第几轮收的）。想要每个议题都跑满，加 `--no-converge`。

议题的 `key` 由模型生成，用 `show` 或 `forge` 的输出可以查到。

---

## 深入

架构与流程、模块职责、为什么纪律由代码追加、离线模拟怎么保证解析链路也被走到——见 **[docs/ENGINEERING.md](docs/ENGINEERING.md)**。

---

## 免责声明

本项目是 CAMEL 框架的学习练习。生成的成果由两个 AI 人格对谈产出，**内容仅供参考，不构成专业意见**——涉及医疗、法律、财务这类专业问题时，请找相应的人。
