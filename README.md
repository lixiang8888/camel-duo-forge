# camel-duo-forge

给**一个目的**，让模型设计出 2–5 位立场与分工不同的专家和他们要吵的议题，再让这些专家对谈出一份成果。

一句话流程：**目的 → 2–5 位专家 + 议题（蓝图）→（可只重生成议题或人格）→ 对谈 →
统一意见 → 成果 + 一页速查 →（可选，可反复）针对成果继续追问，追加新的一节**。

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
读完成果后如果还有想问的，可以继续追问：同一批专家会再谈一轮，把新的一节
追加进同一份手册，速查和实录也跟着更新；可以反复追加。

```bash
uv run duo-forge followup --blueprint blueprints/xxx.json \
  --handbook outputs/handbook-xxx.md --question "连续开会时这套动作怎么执行？"
```

对生成出来的议题或专家不满意，也可以只重生成其中一部分：

```bash
uv run duo-forge reforge --blueprint blueprints/xxx.json --what topics \
  --hint "议题更偏执行、少谈理论"
uv run duo-forge reforge --blueprint blueprints/xxx.json --what personas --people 3 \
  --hint "换一位能算成本的把关人"
```

网页端拖入材料时，如果文件不是一段目的（参考文献、笔记、手册），先在上面写一句
目的，再选「材料只是参考」：AI 会以你的目的为主、用材料补细节，把它具体化。

加 `--mock` 可以离线试跑，不花钱、不联网（产出是占位内容）。

不想敲命令的话，用网页版：

```bash
./run.sh          # 目的 → 锻造蓝图 → 审阅 → 对谈
./run.sh --mock   # 页面默认模拟模式
```

网页里也可以把一份或多份 `.md` / `.txt` 拖进第一步。文件本身不是目的时（参考文献、
笔记、手册），先在上面写一句目的并选「材料只是参考」，AI 会结合材料把它具体化；
否则让 AI 直接从材料提炼。读入后选好用途，再点「锻造蓝图」。

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
