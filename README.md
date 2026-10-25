# camel-duo-forge

给**一个目的**，让模型设计出 2–5 位立场与分工不同的专家和他们要吵的议题，再让这些专家对谈出一份成果。

一句话流程：**目的 → 2–5 位专家 + 议题（蓝图）→ 对谈 → 统一意见 → 成果 + 一页速查**。

---

## 快速开始

需要 Python 3.10–3.14 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
cp .env.example .env      # 填 DEEPSEEK_API_KEY

uv run duo-forge forge --purpose "给刚上班、每天坐十小时的程序员写一份颈椎保养手册"
uv run duo-forge run   --blueprint blueprints/xxx.json
```

默认是两位专家；加 `--people 3`（最多 5）可以让模型设计一个分工互补的团队：

```bash
uv run duo-forge forge --purpose "给刚上班的程序员写一份颈椎保养手册" --people 3
```

跑完在 `outputs/` 下得到三份文件：最终成果、一页速查、完整对话实录。
加 `--mock` 可以离线试跑，不花钱、不联网（产出是占位内容）。

不想敲命令的话，用网页版：

```bash
./run.sh          # 目的 → 锻造蓝图 → 审阅 → 对谈
./run.sh --mock   # 页面默认模拟模式
```

网页里也可以把一份或多份 `.md` / `.txt` 直接拖进第一步：系统会先提炼出标题和目的，
再自动锻造蓝图并停在审阅。

---

## 它和什么有关

本项目是 CAMEL 框架的学习练习，骨架来自另一个项目 camel-fitness-nutrition-duo
（不在本仓库里）：那边的人格和议题是手写死的，这边换成按目的生成。

---

## 文档

- **[使用手册](docs/USER_GUIDE.md)**：全部命令与参数、自定义团队与人格、网页版、产物、
  提前收手、最终统一和常见问题。
- **[工程手册](docs/ENGINEERING.md)**：架构、模块职责、线程模型、测试，以及踩坑记录。

---

## 免责声明

生成的成果由 2–5 位 AI 人格对谈产出，**内容仅供参考，不构成专业意见**——涉及医疗、
法律、财务这类专业问题时，请找相应的人。
