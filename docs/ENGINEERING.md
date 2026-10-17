# 工程手册

面向要读代码、改代码的人。项目是什么、怎么跑起来，见 [README](../README.md)。

- [一、两段流程](#一两段流程)
- [二、和 camel-fitness-nutrition-duo 的对应关系](#二和-camel-fitness-nutrition-duo-的对应关系)
- [三、生成那一步：提示词、解析、重试](#三生成那一步提示词解析重试)
- [四、为什么发言纪律由代码追加](#四为什么发言纪律由代码追加)
- [五、离线模拟怎么保证解析链路也被走到](#五离线模拟怎么保证解析链路也被走到)
- [六、提前收手](#六提前收手)
- [七、换模型](#七换模型)
- [八、踩坑记录（搬运时一并带过来的）](#八踩坑记录搬运时一并带过来的)
- [九、测试](#九测试)
- [十、网页启动器](#十网页启动器)

---

## 一、两段流程

```
       目的（一段自由文本）
              │
              ▼
      ┌───────────────┐
      │  forge        │  两次模型调用
      │  功能一 + 功能二 │
      └───────────────┘
              │
              ▼
        蓝图（blueprints/*.json）  ← 可以手改，改完不用重新生成
              │
              ▼
      ┌───────────────┐
      │  run          │  每个议题：对谈 N 轮上限 → 整理成一节
      └───────────────┘
              │
              ▼
   transcript-*.md   handbook-*.md   quick-*.md
```

两段之间**只通过蓝图通信**。这一刀切下去的理由是钱：`forge` 要花两次模型调用，
议题不满意时改 JSON 再跑就行，不用把人格和议题重新生成一遍。

### 模块职责

```
src/duo_forge/
├── purpose.py     目的：整个项目唯一的输入
├── authoring.py   ★ 功能一 + 功能二：目的 → 两位专家 + 一组议题
├── blueprint.py   ★ 蓝图：落盘、读回、给人看的版本
├── personas.py    Persona 的形状、发言纪律、自定义人格的解析与校验
├── topics.py      Topic 的形状 + 长度上限 + 收尾议题
├── society.py     ★ CAMEL RolePlaying 编排
├── digest.py      对话 → 正文/附录 → 一页速查
├── pipeline.py    ★ 完整流程编排
├── backends.py    离线模拟后端（含给生成环节用的那个）
├── config.py      模型工厂
└── cli.py         命令行入口
```

**`authoring` 是唯一一个会让「领域」进来的地方**，其余模块全都不认识健身、颈椎、
睡眠这些词——它们只认识 Persona / Topic / Blueprint。这条界线是泛化能成立的唯一原因。

`pipeline.py` 是编排的唯一出处：`cli.py` 把它的回调接到 `print` 上。
`authoring` 在 `cli.py` 里是**函数内 import** 的：它要拖进 camel，而
`duo-forge show` 只读一个 JSON 文件，不该为 camel 的导入付钱。同理
`DEFAULT_TOPIC_COUNT` 放在不拖 camel 的 `topics.py` 里。

---

## 二、和 camel-fitness-nutrition-duo 的对应关系

| 那边 | 这边 | 说明 |
|---|---|---|
| `personas.py` 里手写的营养师 × 教练 | `authoring.forge_personas` + `personas.py` | 人格改成生成的；`Persona` 多了 `stance` 字段 |
| `topics.py` 里手写的五个议题 | `authoring.forge_topics` + `topics.py` | 议题改成生成的；`Topic` 字段原样 |
| `society.py` | `society.py` | 照搬，去掉 fooddata 工具依赖，加 `personas` 参数 |
| `digest.py` | `digest.py` | 照搬，写死「健身饮食手册」的地方换成传入的标题 |
| `pipeline.py` | `pipeline.py` | 照搬，标题/专家/议题改从蓝图取，去掉读者档案 |
| `backends.py` | `backends.py` | 照搬，多一个 `ScriptedAuthorBackend` |
| `config.py` | `config.py` | 照搬，多一个 `build_author_backend`（低温） |
| `profile.py`（读者档案） | —— | **没搬**。它是健身场景的产物（身高体重换算 BMI、蛋白质克数） |
| `fooddata.py` + CSV（成分表） | —— | **没搬**。同上，领域专用 |
| `launcher.py`（网页版） | `launcher.py` | 搬了，但改成两段式：forge → 审蓝图 → run（见第十节） |
| `tools/build_food_table.py` | —— | **没搬**。建的是上面那张表 |

没搬的三样都不是「来不及」，而是它们本来就绑在健身饮食上。要做的话，
合适的形态是「按目的挂载领域工具」——`society.debate_topic` 已经留了 `tools`
参数当扩展点，默认不装任何工具。

---

## 三、生成那一步：提示词、解析、重试

`authoring.py` 是这个项目的全部增量，它的三段提示词（`PERSONA_SYSTEM`、
`TOPICS_SYSTEM`，以及各自带上下文的 user prompt）是要认真读的部分。这里只说
工程上的几个决定。

### 解析要宽松

模型很爱在 JSON 外面套一层 ``` 或者写一句「好的，以下是设计」，所以
`_extract_json` 不直接 `json.loads` 全文，而是：

1. 有 ``` 代码块就先剥掉；
2. 找到第一个 `{`，**按括号配对**找它对应的 `}`（字符串里的花括号会跳过，
   转义引号也处理了）；
3. 只解析中间那一段。

这样做比正则稳，也比「让模型保证只输出 JSON」这种指望靠谱。

### 校验要严

宽松只到「把 JSON 掏出来」为止。掏出来之后每个字段一个字都不放过：

- 缺失、空白、类型不对 → 报错，并**同时给出中文标签和 JSON 字段名**
  （`第一位专家（proposer）的人格设定缺失，或者不是非空文本。（JSON 字段：system_message）`）。
  给字段名是因为**这句话会原样回到重试提示里**，模型要照着它改，只说
  「人格设定」它不知道该动哪个键。
- 超长 → 报错。上限定义在 `topics.py` / `authoring.py` 里，都是成本相关的：
  `brief` 和 `goal` 每个议题、每一轮都要原样发给两位专家，写长了是
  **乘上轮数再乘上议题数**地翻。
- 议题的 `key` 会被 slug 化（`Sleep Debt! 周末` → `sleep-debt`），撞车了自动加后缀，
  并且避开保留的 `closing`。

### 重试：带着错误信息再问一次

格式错一次就让整个流程作废太浪费。`_ask_json` 在解析失败时会把**具体的错**
拼回提示词，用**同一个 agent** 再要一次——同一个 agent 才看得到自己刚写的那份，
改起来有依据；把原提示词重发一遍的话，它多半会原样再错一次。

两次都错才抛 `AuthoringError`。重试次数是 2，不是无限：格式错是偶发的，
两次都错说明是提示词本身的问题，再试也是白花钱。

### 一个刻意的不对称

`forge_topics` 只生成**正文议题**，收尾议题由 `topics.closing_topic` 按代码造。
理由不是偷懒：收尾议题的输入是前面**全部**对话（它要写「你们哪些地方没谈拢」），
形状和普通议题本来就不一样。交给模型生成，只会得到一个和普通议题重复的东西。

### 用户自定义人格

`personas.py` 里有一层不依赖 camel 的解析：`PersonaSpec` / `PersonaPlan` /
`parse_persona_plan`。CLI 和网页启动器共用它，两边不会各有一套校验。

每位专家独立选 `auto` / `hint` / `manual`。生成时：

- **保持完整的人格 JSON schema**（提示词里仍有 `"proposer"`，`ScriptedAuthorBackend`
  靠它识别请求）。只解析模型该生成的那几位；`manual` 侧直接取用户给的 `Persona`，
  模型漏写/写坏它也不会触发重试。
- 提示词里只放 `manual` 侧的 `name/title/stance` 摘要，**不**注入完整
  `system_message`。那段可能有上万字，塞进去既贵，也扩大提示词注入面。
- 两位都 `manual` 时**一次人格调用都不发**，标题取 `purpose.title`，没有就
  「对谈成果」/ slug `duo`；议题照常生成。
- 手写上限 `MAX_MANUAL_SYSTEM_MESSAGE = 12000`，和模型生成的 4000 是两回事。
  蓝图加载器仍然宽松（蓝图是给人手改的），这个上限只在创建时把关。
- 纪律检查扩到了生成结果：模型或 hint 即使把【发言纪律】【收手纪律】写进人格，
  也会被拒绝并带着具体错误重试。

「人格记忆」没有独立字段，就是人格设定里的一段长期背景与案例——最简单直接的注入口。
它只在创建时受 12000 字限制，并且会随每个议题、每一轮进入模型调用，成本随长度线性上升。

---

## 四、为什么发言纪律由代码追加

生成人格的提示词明确要求模型**只写**【人格】【专业立场】【你的盲区】三块，
`personas.SPEECH_DISCIPLINE` 和 `CHALLENGER_CONTRACT` 由代码拼上去。

因为纪律里有两条是**机器契约**：

| 纪律 | 谁依赖它 | 模型漏写会怎样 |
|---|---|---|
| 「没有新的反对意见时只回【无异议】」 | `society` 的提前收手 | **不会有任何报错**，只是每个议题都闷头跑满轮数——慢、贵，而且看不出来 |
| 「每轮 250 字以内」 | 成本、上下文预算 | 上下文被挤掉，同样不报错 |

把它交给模型，等于把一条静默失效的路径引进流程里。所以这两条按代码拼装，
一字不差，`CHALLENGER_CONTRACT` 只追加给挑战者（第二位）：
挑刺的人说没刺可挑了，这场辩论才算到头。

副作用是好的：人格那三块因此可以完全交给模型自由发挥，不用在提示词里
反复叮嘱格式，生成质量反而更高。

---

## 五、离线模拟怎么保证解析链路也被走到

`ScriptedAuthorBackend` 吐的是**合法 JSON**，不是把人格和议题硬编码成 dataclass。

区别在于：硬编码的话，「解析 → 校验 → 建 dataclass」这条链路上有 bug 就永远
发现不了（模拟模式永远走不到那些代码）；让它吐 JSON，模拟模式就把这条链路
真的走一遍。

它靠提示词里出现的 schema 键名分辨这次要的是哪一份（`"proposer"` 还是
`"topics"`）。这不是什么优雅的协议，但胜在**不用往真实提示词里塞暗号**——
那些键名本来就在提示词的 schema 里写着，真实模型看到的提示词一个字没变。

认不出来时它返回一句「模拟后端不认识这个提示词」，让 `authoring` 抛解析失败，
而不是悄悄产出一份假蓝图——那才是最坏的结果。

注意 `pipeline.run_pipeline` 里 `early_stop = converge and not mock` 这条：
收手在 mock 下**恒关**。mock 的职责就是证明「循环确实跑满了 N 轮」，
让它在轮数上变来变去会让那条保证失效。`pipeline` 是唯一把「mock」翻译成
「关掉收手」的地方，`society` 只认 `early_stop`，它不认识 mock。

---

## 六、提前收手

和 camel-fitness-nutrition-duo 一模一样，包括那几个坑：

**camel 在这件事上什么都没规定**——`RolePlaying.step()` 是**一轮一调**，内部不含循环、
也没有 `max_turns`，跑几轮完全由调用方决定。所以「跑满 N 轮」是
`society.debate_topic` 自己那个 `for` 循环，收手也在这里。

判据是让挑战者自己表态（见第四节）。`society._is_concession` 看到那句话就收手，
**判定用完全相等，不是包含**——模型要是写了「【无异议】不过还有两点」，那说明它
其实还有话说，这里就当没看见、让辩论继续。两个方向的错代价差得很远：漏判只是
多花一轮的钱，误判会把一整段发言从成果里抹掉，所以宁可漏判。

三道保底：

1. `society.MIN_ROUNDS = 2`——第 1 轮是「挑战者挑刺 + 提出方回应」，是结构上最小
   的有效交锋，不许砍。
2. 收手那轮的发言**先记录再判断**，一个字都不丢。另外 `RolePlaying.step()` 一轮
   往返中途掐不断，所以收手总落在**轮次边界**上，记录里每一轮都是完整对称的。
3. `--no-converge` 一键回到死板跑满。

> 顺带修了一个和收尾议题有关的旧 bug：`Blueprint.select` 以前在 `--only` 只选部分
> 议题时，收尾议题仍按蓝图里**全部**议题数生成开场白（「我们前面谈了 N 个」）。
> 现在按本次选中的数量算，网页的议题勾选和命令行的 `--only` 都受益。

> 顺带记一笔：`society.py` 里 `if assistant_response.terminated or ...: break` 这行
> **在我们这套配置下永远不会触发**。camel 的 `ChatAgent` 默认不装任何
> `response_terminator`，那个 `terminated` 只在「上下文超限」和「外部 stop_event」
> 两种情况下为真。它现在是一条保留的安全网，不是收手机制。

---

## 七、换模型

走的是 OpenAI 兼容协议，换 Kimi / 通义 / 本地 Ollama 只需改 `.env` 里两行：

```ini
DEEPSEEK_BASE_URL=https://api.moonshot.cn/v1
DEEPSEEK_MODEL=moonshot-v1-8k
```

两个温度是分开的（`config.py`）：

- 对谈 `DEBATE_TEMPERATURE = 0.75`：要吵出有信息量的东西，需要一点发散。
- 生成 `AUTHOR_TEMPERATURE = 0.3`：这一步的成品**要被 `json.loads` 解析**，
  温度一高模型就爱在 JSON 前后加一句「好的，以下是」。

换模型时要注意：生成那一步对 JSON 的服从度比较吃模型能力。用小模型时
`_ask_json` 的重试会更常被触发——那是它在干活，不是坏了。

---

## 八、踩坑记录（搬运时一并带过来的）

前四条在 camel-fitness-nutrition-duo 里踩过并修掉了，这边的代码**原样沿用了那些
规避写法**，所以把记录一并带过来。改动这些写法之前先读一遍。

**1. `mcp` 版本过新会让 `import camel` 直接崩**

camel-ai 0.2.90 只声明了 `mcp>=1.3.0`，没有上界，于是装到 mcp 2.x。
而 mcp 2.x 移除了 `mcp.server.FastMCP`，camel 内部还在 `from mcp.server import FastMCP`：

```
ImportError: cannot import name 'FastMCP' from 'mcp.server'
```

`pyproject.toml` 里已锁 `mcp>=1.3.0,<2`，别删。

**2. `ChatAgent` 已经没有 `role_name` 参数了**

```
ChatAgent(system_message, model, memory, ...)   # 0.2.90
ChatAgent(role_name, system_message, model...)  # 旧教程里的写法
```

**3. `RolePlaying` 会覆盖你写的人格**

默认情况下 `RolePlaying` 会根据 `assistant_role_name` 自己生成 system message，
辛苦写的人格设定会被丢掉。必须把构建好的 agent 显式传进去：

```python
RolePlaying(
    assistant_role_name=proposer.name,
    user_role_name=challenger.name,
    assistant_agent=assistant_agent,   # ← 传了才用你的 system_message
    user_agent=user_agent,
    with_task_specify=False,           # ← 否则内部还会再建 agent
    with_task_planner=False,
    with_critic_in_the_loop=False,
)
```

**这条在这个项目里比在原来更危险**：原来人格是手写的，覆盖掉了一眼能看出来；
现在人格是模型生成的，被覆盖掉之后「生成得不好」和「根本没被用上」长得一模一样。

**4. camel 默认的 token 计数器要联网下载词表，在国内会无限期挂住**

> 症状：**计时器在走，但一句发言都不出来**，也不报错；终端里一条日志都没有。

`ChatAgent` 构造时会向模型后端要 token 计数器，camel 默认去建
`OpenAITokenCounter`，而它要用 `tiktoken` 加载 `o200k_base` 词表。本地没有词表时，
tiktoken 会去 `openaipublic.blob.core.windows.net` **下载**——那个域名在国内连不上，
`requests` 又没设超时，于是**卡死在建 agent 那一步**。

更阴的是 tiktoken 把词表缓存在 `/tmp/data-gym-cache`（不是 `~/.cache`），
重启一次就没了，所以它的表现是「昨天还能跑，今天就不行」；而且**模拟模式一切正常**
（离线后端自带计数器），很容易被误判成业务代码的 bug。排查时用 `faulthandler`
打线程栈最快——栈顶会直接指到 `tiktoken/load.py:17 read_file`。

修法是给真实后端也注入一个不联网的计数器，顺带设上超时（`config._real_backend`）。

---

## 九、测试

```bash
uv run pytest
```

189 条，全部离线（用的是模拟后端，不花钱、不联网）。八个文件各管一层：

| 文件 | 管什么 |
|---|---|
| `tests/test_authoring.py` | JSON 抽取的各种形态、字段校验、重试、自定义人格、`forge` 合起来 |
| `tests/test_blueprint.py` | JSON 往返、给人看的那份、`--only` 语义、长人格、围栏、重名 |
| `tests/test_personas.py` | 自定义人格的解析矩阵：mode、必填、长度、纪律标记、结构错误 |
| `tests/test_purpose.py` | 目的的长度边界、从文件读、标题行 |
| `tests/test_society.py` | 轮次与顺序、提前收手的全套判据、整理与拼装 |
| `tests/test_pipeline_run.py` | 端到端跑一遍：落盘、钩子、收手开关 |
| `tests/test_launcher.py` | 广播、HTTP 路由、两段式准入、keep-alive、停止不写文件 |
| `tests/test_cli.py` | 三个子命令跑得通，参数给错时报的是人话 |

几条值得知道的：

- **`test_challenger_gets_the_concession_contract_and_proposer_does_not`**
  钉住了第四节那个不对称：收手纪律必须只追加给挑战者。将来谁把 `challenger=`
  参数传错，这条会先红。
- **`test_the_quick_version_is_compressed_from_the_finished_handbook`**
  钉住速查版的核心不变量：它的输入必须是拼好的完整版，不是对话。
- **`test_mock_always_runs_the_full_rounds`** 特意用一个**会**说「无异议」的后端：
  如果 mock 没把收手关掉，这条会红。
- **提前收手那组测试全靠「奇数调用都是挑战者」这条规律**：开场白不花模型调用，
  之后每轮先挑战者后提出方。`ConcedingBackend` 据此让挑战者在第 2 轮说「无异议」。
  `test_scripted_backend_never_trips_the_concession` 是一条表征测试——
  将来谁把 mock 的文案改成含「无异议」，它会先红。
- `test_authoring.py` 里 `FlakyBackend` 第一次回一段没用的东西、第二次回合法 JSON，
  用来验证重试真的会发生、且**重试提示里带着具体的错**。

---

## 十、网页启动器

`launcher.py` 在仓库根目录，是给「不想敲命令」的人用的本地网页界面。它**不是第二套
业务逻辑**：forge 调 `authoring`，run 调 `pipeline`，和 CLI 完全相同。

### 为什么是两段式

`forge` 要花两次模型调用，而蓝图本来就是给人审、给人改的。所以页面有明确的两步：
**锻造蓝图 → 审阅/勾选议题 → 开始对谈**。不做「一个按钮从头跑到尾」——议题不满意时
重跑一整轮的钱白花了。

网页**不加载** `blueprints/` 下的历史蓝图：复用蓝图是命令行的用法
（`duo-forge run --blueprint`）。页面只跑当次 forge 出来的那一份，状态简单得多。

### 分层红线

`launcher.py` 可以 import `duo_forge.*`，但 `duo_forge` 里的任何模块都不 import
launcher。所以没有浏览器、没有 Windows 的机器上，`duo-forge run` 照样能跑。
根目录的 launcher.py 也不进 wheel（`pyproject.toml` 只打包 `src/duo_forge`）。

### 线程模型与准入锁

```
HTTP 线程池（ThreadingHTTPServer）
   ├─ GET  /events   → 阻塞在订阅队列上，逐条 SSE 写出
   └─ POST /stop     → job.request_stop()

daemon worker 线程：同步调用 authoring / pipeline
   └─ 各 hook → bus.publish(...)
```

- worker 线程里**一个 HTTP 调用都不发**，只往广播里放事件。
- worker 是 daemon 且**永不 join**：关页面不该等模型把这一轮跑完。
- **准入必须加锁**。`server._lock` 保护「检查是否有任务在跑 → 构造 job → 赋值」这
  三步；否则两个 `/forge`（或 `/forge` 撞 `/run`）会同时通过检查，起两个付费 worker。
  `job.start()` 在锁外调用。锁序单向：`server._lock` → `_Bus._lock`。
- **终态事件先于 `finished.set()`**。反过来的话，新 job 的 `bus.reset()` 可能夹在
  中间，旧 job 的 `done` 会落进新 job 的历史里。
- forge 没有 stop 钩子：`/stop` 只置 `cancelled`，worker 在调用返回后检查
  「是否被取消 / 是否已被新 job 取代」，命中就丢弃结果、不落盘、不赋值。
  页面上明说「无法掐断正在进行的调用」。

### 两段之间的交接

只有两样东西：`server.blueprint` 和一次 `bus.reset()`。

- forge worker 在**锁内**把蓝图赋给 `server.blueprint`，**先赋值再广播** `forge_done`
  ——页面收到事件后可能立刻 POST `/run`。
- run 在准入时把 `(blueprint, topics)` 快照进 job，worker 不再回头读
  `server.blueprint`。否则一次新的 forge 会把正在跑的这一轮换掉。
- 议题选择复用 `Blueprint.select`。注意 `select("")` 的语义是「全部」，所以空选择
  必须在路由里先拒绝；`run_pipeline(topics=[])` 也会把空列表当成全部。

### 几个从命令行带过来的硬约束

- **HTTP/1.1 keep-alive**：每个 POST 在任何提前返回前都要把请求体读干净。没读完的
  字节会粘到下一请求的请求行上（表现为 `501 Unsupported method ('{...}GET')`）。
- **状态行必须 ASCII**：`send_error` 的 reason 按 latin-1 编码，中文会直接抛
  `UnicodeEncodeError`。
- **SSE 连接永不主动关闭**：`done` 之后断开会让 EventSource 重连、拿到历史、再断开，
  变成无限重连。前端用单调递增的 `seq` 去重。
- **单实例**：`/health` 报 `_APP_ID`，启动时扫 11 个端口；重复双击只把浏览器指过去。
  默认端口 8785，和前作的 8775、autogen 的 8765 错开。
- **mock 下收手恒关**：这仍然是 `pipeline.run_pipeline` 的职责，launcher 不重复判断。

### 一键启动的两个入口

- `run.sh`：无子命令（或只给选项）时起网页版；`forge`/`run`/`show` 原样透传给 CLI。
  启动器模式没有 API key 只警告不退出——用户还能在页面里勾 mock。
- `make_shortcut.py`：在 Windows 桌面建 `.lnk`，目标
  `wsl.exe -d <distro> --cd <项目> -- .venv/bin/python launcher.py`。
  用直接解释器而不是 `uv run`（秒起、不解析环境）；`.lnk` 内嵌本机发行版名和绝对
  路径，所以不入库（`*.lnk`）。
