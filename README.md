<div align="center">

<h1>camel-duo-forge</h1>

<p><strong>给一个目的，让 2–5 位立场与分工不同的 AI 专家先吵出自己的议题，再对谈出一份能照做的成果。</strong></p>

<p>
  <a href="https://www.python.org/"><img alt="Python 3.10–3.14" src="https://img.shields.io/badge/Python-3.10--3.14-3776AB?logo=python&logoColor=white"></a>
  <a href="https://docs.astral.sh/uv/"><img alt="managed by uv" src="https://img.shields.io/badge/managed%20by-uv-261230"></a>
  <a href="https://github.com/camel-ai/camel"><img alt="built with CAMEL" src="https://img.shields.io/badge/built%20with-CAMEL-F97316"></a>
</p>

<p>
  <a href="#快速开始">快速开始</a> ·
  <a href="#常用命令">常用命令</a> ·
  <a href="#网页版">网页版</a> ·
  <a href="#产物">产物</a> ·
  <a href="#文档">文档</a>
</p>

</div>

---

## 这是什么

`camel-duo-forge` 只接受一个核心输入：**目的**（一段自由文本），或者一份材料加上你自己写的目的。

它会：

1. 按目的设计 **2–5 位立场与分工不同的专家**；
2. 设计一组这些专家**天然会吵起来**的议题，连同人格一起存成可读、可改的**蓝图**；
3. 让你先审阅蓝图——不满意可以只重生成议题，或只重生成人格；
4. 让专家逐议题对谈、整理成文：所有回应者都没新意见时提前收手，全部议题结束后仍有分歧就补一轮最终统一；
5. 产出**完整成果 + 一页速查 + 完整对话实录**；
6. 读完成果后还可以**继续追问**，把新的一节追加进同一份手册，可反复追加。

一句话流程：

```text
目的
  └─ forge  → 2–5 位专家 + 议题（蓝图）
       └─ run → 逐议题对谈 → 统一意见 → 成果 + 一页速查
            └─ followup（可反复）→ 追加讨论 N
```

---

## 快速开始

### 环境

- Python **3.10–3.14**
- [uv](https://docs.astral.sh/uv/)

### 安装

```bash
git clone https://github.com/lixiang8888/camel-duo-forge.git
cd camel-duo-forge
uv sync
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
```

### 先跑一次

两段式：先锻造蓝图，审一眼再跑。

```bash
uv run duo-forge forge --purpose "给刚上班、每天坐十小时的程序员写一份颈椎保养手册"
uv run duo-forge run   --blueprint blueprints/xxx.json
```

也可以一步跑完（跳过中间的审阅）：

```bash
uv run duo-forge run --purpose "给刚上班、每天坐十小时的程序员写一份颈椎保养手册"
```

没有 API key 或不想花钱时，加 `--mock` 离线试跑；产出是占位内容：

```bash
uv run duo-forge forge --purpose "给刚上班的程序员写一份颈椎保养手册" --mock
uv run duo-forge run   --blueprint blueprints/xxx.json --mock
```

默认是两位专家；`--people 3`（最多 5）会让模型设计一个分工互补的团队：

```bash
uv run duo-forge forge --purpose "给刚上班的程序员写一份颈椎保养手册" --people 3
```

---

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `forge` | 目的 → 蓝图（2–5 位专家 + 议题），落盘、可读、可改 |
| `reforge` | 只重生成议题，或只重生成人格（连带重生成议题） |
| `run` | 蓝图 → 逐议题对谈 → 整理 → 成果 |
| `followup` | 读完成果后继续追问，把新的一节追加进同一份手册 |
| `show` | 摊开看一份蓝图，不调用模型 |

```bash
# 只换一批议题，并给这次一个方向
uv run duo-forge reforge --blueprint blueprints/xxx.json --what topics \
  --hint "议题更偏执行、少谈理论"

# 换一批专家（连带重生成议题）
uv run duo-forge reforge --blueprint blueprints/xxx.json --what personas --people 3 \
  --hint "换一位能算成本的把关人"

# 只跑指定议题（key 见 forge 输出或 show）
uv run duo-forge run --blueprint blueprints/xxx.json --only baseline,reality

# 读完成果后继续追问，可反复追加
uv run duo-forge followup --blueprint blueprints/xxx.json \
  --handbook outputs/handbook-xxx.md --question "连续开会时这套动作怎么执行？"

# 不调用模型，只看蓝图
uv run duo-forge show --blueprint blueprints/xxx.json
```

全部参数、提前收手、最终统一、人格与议题的自定义方式见
[使用手册](docs/USER_GUIDE.md)。

---

## 网页版

不想敲命令的话，用网页启动器：

```bash
./run.sh          # 启动并自动打开浏览器
./run.sh --mock   # 页面默认勾上模拟模式，不花钱
./run.sh --port 9000
```

页面流程：**目的 → 锻造蓝图 → 审阅 / 重生成 → 开始对谈 → 读完成果继续追问**。

- 第 2 步可以**重生成议题**或**重生成人格 + 议题**，并在按钮上方写一句这次的要求；
  最下面还有**手动编辑蓝图（高级）**，可以直接改议题 / 人格 JSON 并应用；
- 拖入 `.md` / `.txt` 材料时，先选材料用途：
  - **就是一段目的**：AI 从材料提炼标题和目的；
  - **只是参考**：你先写一句目的，AI 以它为主、用材料补细节，把它具体化。
- 材料上限：默认合并后 5 万字、一次 50 份、单份 10 MB，可用 `.env` 里的
  `DUO_FORGE_MAX_MATERIAL*` 调整。

WSL 里想建桌面快捷方式（之后双击启动）：

```bash
uv run python make_shortcut.py
```

---

## 产物

跑完一轮后，`outputs/` 下有三份文件：

| 文件 | 内容 |
| --- | --- |
| `handbook-*.md` | **最终成果**：各议题正文、附录、分歧备忘、最终统一意见 |
| `quick-*.md` | **一页速查版**，从完整版压缩出来，不新增说法 |
| `transcript-*.md` | 全部专家的完整对话实录 |

读完成果后继续追问时，这三份文件会**原地更新**：手册多一节「追加讨论 N」，
实录追加一段对话，速查按更新后的全文重压。

模拟模式产出的文件带 `.mock` 后缀、开头有醒目警告——那份是占位内容，没有参考价值。

---

## 它是怎么工作的

```text
目的 / 材料 + 目的
        │
        │  forge：设计专家人格 + 议题
        ▼
蓝图 blueprints/*.json  ← 可手改；也可用 reforge 只换议题或人格
        │
        │  run：逐议题对谈 → 整理成一节
        ▼
仍有分歧？ ── 是 ──→ 补一轮最终统一（附录 D）
        │
        ▼
handbook.md + quick.md + transcript.md
        │
        │  followup（可反复）：同一批专家再谈一轮
        ▼
追加讨论 N：……
```

两段之间只通过蓝图通信：`forge` 要花几次模型调用，议题不满意时改蓝图再 `run`
就行，不用把人格和议题重新生成一遍。

<details>
<summary>一次真实运行大概要花多少次模型调用？</summary>

| 阶段 | 调用次数（上限） |
| --- | --- |
| `forge`，2 人 | 2（人格 + 议题） |
| `forge`，3–5 人 | 3（团队骨架 + 人格 + 议题） |
| `forge`，全部手写人格 | 1（议题） |
| 每个议题的 `run` | 轮数 × 人数 + 1（整理） |
| 全部议题结束后的最终统一 | 人数（有分歧时才跑） |
| 一页速查 | 1 |
| `followup` | 1（追问设计）+ 轮数 × 人数 + 1（整理）+ 人数（可选统一）+ 1（速查） |

提前收手会让实际次数少于上限；模拟模式不调用真实模型。

</details>

---

## 和什么有关

本项目是 [CAMEL](https://github.com/camel-ai/camel) 框架的学习练习，骨架来自另一个项目
camel-fitness-nutrition-duo（不在本仓库里）：那边的人格和议题是手写死的，这边换成
按目的生成。

---

## 文档

- **[使用手册](docs/USER_GUIDE.md)**：全部命令与参数、自定义团队与人格、网页版、材料、
  产物、提前收手、最终统一、重生成与追问。
- **[工程手册](docs/ENGINEERING.md)**：架构、模块职责、线程模型、测试与踩坑记录。
- **[知识挂载设计稿](docs/KNOWLEDGE_MOUNT.md)**：材料原文保留、按议题寻址注入与引用校验
  的设计（设计稿，尚未实现）。

---

## 开发

```bash
uv run pytest
```

测试全部离线，使用模拟后端，不花钱、不联网。

```text
src/duo_forge/     核心：生成、蓝图、对谈、整理、追加讨论
launcher.py        网页启动器（只用标准库）
tests/             离线测试
docs/              使用 / 工程 / 设计文档
blueprints/        生成出来的蓝图
outputs/           跑出来的成果、速查、实录
```

`duo_forge` 里的任何模块都不 import `launcher`，所以命令行在没有浏览器、没有 Windows
的机器上照样能跑。

---

## 免责声明

生成的成果由 2–5 位 AI 人格对谈产出，**内容仅供参考，不构成专业意见**——涉及医疗、
法律、财务这类专业问题时，请找相应的人。
