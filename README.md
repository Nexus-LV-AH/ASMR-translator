# ASMR-translator

把 **音声 / 同人音声作品**（mp3 / wav / flac）自动做成 **日文字幕 → 中文 LRC** 的本地小工具：
anime-whisper 提取日文、官方台本可选精修、SakuraLLM（本地）或 DeepSeek（云端 API）翻译，
最后按作品整理成可以直接丢进播放器 / 上传到 asmr.one 的作品包。

## 先改 config.yaml（三处）

打开 `config.yaml`，**文件最上面**就是这三个字段（都带 ★）：

```yaml
translation:
  registry:
    sakura-14b-qwen3-v1.5:
      path: ""          # ★ 你的 Q6_K gguf，例如 ~/models/sakura-14b-qwen3-v1.5-q6k.gguf
    sakura-14b-qwen3-v1.5-iq4xs:
      path: ""          # ★ 你的 IQ4_XS gguf
    deepseek-api:
      api_key: ""       # ★ 云端 API key（不用云端就留空）
```

- 路径支持 `~` 和相对项目根的路径；**只走云端 API 的话，两个本地路径完全不用填**。
- key 也可以放 `.env`（`DEEPSEEK_API_KEY=sk-...`），`api_key` 留空即可。
- 没填就跑会明确提示"还没配置"，不会误触发模型下载。

- 三种任务：只提取字幕 / 完整运行 / 制作翻译
- 有台本就精修，没台本也能跑（不会中断）
- 中文 LRC 第一行就是作品标题（播放器里可见）
- 翻译来源（本地模型 / 云端 API）会写进 LRC 的 `[by:]`
- 提示词全部是 `prompts/` 下的独立文本，随时可改
- 整轮结束后自动归档，`input/` 清空，等下一个任务
- 额外维护一个「只进不出」的 `LRC成品库/`，按 RJ 号收好中文 LRC

## 目录

- [先改 config.yaml](#先改-configyaml三处)
- [项目简介](#项目简介)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [三种任务](#三种任务)
- [产物结构](#产物结构)
- [模型](#模型)
- [提示词定制](#提示词定制)
- [术语表](#术语表)
- [验收与抢救](#验收与抢救)
- [台本精修](#台本精修)
- [常见问题](#常见问题)
- [隐私与合规](#隐私与合规)
- [开发与贡献](#开发与贡献)
- [许可](#许可)
- [致谢](#致谢)

## 项目简介

基本上纯ai写的小工具，下面都是ai直接一键生成的，我大致描述一下本工具的构成：

input文件夹直接把你要翻译的asmr带着rj号放进去里面有无环境声都可以，有台本就剪贴到promptbook文件内，LLM会根据台本内容对ocr出来的日语文本做对照精修。

最后会把完整的成品放到output文件夹里面，LRC成品库文件夹是方便你压缩导入字幕到asmrone上面的，scripts是代码库，prompts里面是大模型的提示词。中间文件文件夹是运行时统一暂放字幕的地方（提取写入、翻译读取，里面再分日文字幕/中文字幕/LRC），跑完归档后会自动清空，不用理它。

可以通过start.sh直接运行，但是我更习惯通过subtitle.py运行本工具。在config.yaml文件里面填你本地llm路径和云端大模型的apikey，我推荐用云端主要是快，本地大模型我用的SakuraLLM，质量虽然可以但是一般个人设备跑起来太慢了然后还要防止电脑熄屏，一个完整的asmr成品差不多要跑一两个小时，云端快一点。

ds写的文档真的是一股味，很无力，雷霆大思考雷霆大修改然后我自己都不认识这到底是什么玩意了。
有任何不足或者可以改进的地方请一定要写issue告诉我。

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | macOS（Apple Silicon 推荐；Intel 会回退 CPU，较慢）。Linux 可自行适配，Windows 未测试（WSL 可能可用） |
| 内存 | 本地 14B 模型建议 ≥ 24GB；也可换 4bit 量化或云端 API |
| 磁盘 | 依赖 + ASR 模型约 3GB；本地翻译模型另需 7.6~11.3GB |
| 网络 | 首次运行需下载依赖/ASR 模型；之后可离线（云端模式除外） |
| Python | 不用管，`start.sh` 会把 uv + Python 3.12 装进项目内 |
| 本地推理 | `llama.cpp`（`brew install llama.cpp`）；只用云端 API 则不需要 |

## 快速开始

```bash
git clone <你的仓库地址> ASMR-translator && cd ASMR-translator
./start.sh                       # 首次自动装 uv、依赖、ASR 模型
```

> 配置见最上面[「先改 config.yaml」](#先改-configyaml三处)。
> 不带参数时：`extract` / `all` 只扫描 `input/`（为空会**立即提示并退出，不会加载模型**）；
> `translate` 扫描 `中间文件/日文字幕/`，不依赖 `input/` —— 已经有日文字幕时用它。

或者按我的习惯，直接用入口脚本（同样会进入 `.venv`）：

```bash
python3 subtitle.py            # 菜单选 1/2/3
```

把音频按作品放进 `input/`（推荐用 RJ 号命名文件夹）：

```
input/
└── RJ01012345/
    ├── track01.mp3
    ├── track02.mp3
    └── cover.jpg
```

跑完 `input/` 会被清空，产物在 `output/RJ01012345/`，中文 LRC 同时在 `LRC成品库/RJ01012345/`。

启动后依次询问：

1. **任务**：`1 只提取字幕` / `2 完整运行` / `3 制作翻译`
2. **翻译模型**：菜单只列出**当前可用**的项——本地模型没填路径/没下载就不会出现。
   正常是 `1` 本地 Q6_K（质量最好）/ `2` 本地 IQ4_XS（省内存）/ `3` DeepSeek API（不占内存）。

### 用云端 API（可选）

两种填 key 的方式（任选其一）：

```yaml
# 方式 A（最直接）：config.yaml → translation.registry.deepseek-api
api_key: sk-你的key
```

```bash
# 方式 B（更安全，key 不进 config）：在项目根目录新建 .env
# （.env 已在 .gitignore 里，不会入库）
printf 'DEEPSEEK_API_KEY=sk-你的key\n' > .env
```

然后菜单选 `3) DeepSeek API`。可用模型名以服务端为准（默认 `deepseek-flash`，另一个是
`deepseek-v4-pro`；可用 `.env` 的 `DEEPSEEK_MODEL` 覆盖）。API 模式不加载本地模型、不占内存，
按量计费；启动时先做一次 `/models` 预检，名字写错会立刻报错并列出可用模型。

> 如果你把 key 填在 `config.yaml` 里，注意**不要把它提交到公开仓库**（`config.yaml` 是跟踪文件）；
> 更安全的做法是用 `.env`（已忽略）。

## 三种任务

```bash
./start.sh                 # 菜单选择（不带参数）
./start.sh extract         # 1 只提取字幕：音频 → 日文字幕 → 台本精修（有台本时）
./start.sh all             # 2 完整运行：提取 + 精修 + 翻译 → 中文 LRC → 归档
./start.sh translate       # 3 制作翻译：已有日文字幕 → 中文 LRC → 归档
```

常用变体：

```bash
./start.sh extract input/RJ01012345     # 只处理某个作品
./start.sh extract --track 03           # 只处理某轨
./start.sh extract --no-model           # 精修只用规则（最快，不加载模型）
./start.sh translate --force            # 忽略缓存重译（改提示词/术语表后要用）
./start.sh translate --track 05         # 只重翻第 5 轨
./start.sh all --limit 20               # 只翻前 20 行，先看效果（不会覆盖已有更长的成品）
./start.sh all --no-consolidate         # 不归档，全部留在工作区
./start.sh --fetch-model anime-whisper  # 只下载 ASR 模型
```

## 产物结构

```
output/RJ01012345/
├── 字幕文件/
│   ├── 日文/           <轨道>.ja.txt / .timing.json / .meta.json / .ja.refined.txt
│   └── 中文/           <轨道>.zh.txt / .zh.cache.json / .zh.meta.json / <轨道>.lrc
├── 音频/              原始音频原样（wav 还是 wav、mp3 还是 mp3）+ 封面图
└── 台本/              台本（没有台本就不建）
```

目录名都能在 `config.yaml → bundle.dirs` 里改（`subs / ja / zh / audio / promptbook`）。

**中间目录**：`中间文件/` 是唯一的中间工作区，提取和翻译共用——

```
中间文件/
├── 日文字幕/       ← 提取的输出，也是翻译的输入（含 timing / meta / 精修版）
├── 中文字幕/       ← 翻译输出（.zh.txt / 缓存 / 元数据）
└── LRC/            ← LRC 暂存，归档时并入作品包的「字幕文件/中文」
```

跑完归档后 `中间文件/` 会被清空；目录名在 `config.yaml → paths.work_dir` 和 `paths.work_dirs` 里改。

LRC 长这样（`[ti:]` 元数据 + 第一行标题字幕 + 正文）：

```
[ti:雨声]
[by:Sakura 14B Q6_K]
[00:00.00]雨声
[00:10.80]果然在。
[00:23.00]别露出那种表情啊。
```

- `[by:]` 跟随实际使用的模型（本地 / 云端）；可在 registry 条目里用 `by_tag` 自定义。
- 标题行与 `[ti:]` 同时写入；若首条字幕从 0 秒开始，标题可能被它盖住
  （`lrc_title_at_ms` / `lrc_title_line` 可调，见 `config.yaml` 注释）。

### LRC 成品库（只进不出）

项目根目录的 `LRC成品库/` 是一个**只写入、不取出**的收集目录：

```
LRC成品库/
├── RJ01012345/
│   └── track01.lrc          ← 只有中文 LRC，不放别的东西
└── RJ01067890/
    └── track01.lrc
```

- 每次翻译任务结束（归档之后）自动把该作品的中文 LRC **复制**进来，按 input 文件夹名（RJ 号）分目录；
- 只复制 `.lrc`，不含 `.txt`/缓存/元数据/音频；同名文件覆盖为最新版本；
- 从不移动、不删除、不清理——只会随运行次数增加；
- 目录名在 `config.yaml → paths.collect_dir`（默认 `LRC成品库`），开关是 `bundle.collect_lrc`。

### 归档规则

- **整轮任务结束后**才归档，一次搬完；不会"翻一轨搬一轨"。
- 该作品只要还有轨道没有中文字幕，**本轮就不搬**（文件留在工作区，补完再跑），也不会进成品库。
- 归档是**目录级移动**：`input/<作品>/` 整份进 `音频/`，字幕按语言进 `字幕文件/`，台本进 `台本/`。
- 搬完清空工作目录（`input/`、`中间文件/日文字幕/`、`中间文件/中文字幕/`、`中间文件/LRC/`、`promptbook/` 里的对应作品），
  `.DS_Store` / `Thumbs.db` 会顺手清掉。
- 手动归档 / 预览：`.venv/bin/python scripts/bundle.py --dry-run`。

## 只翻译：字幕文件怎么放

`translate` 只吃**日文字幕**，没有音频也能跑（归档时再按作品目录找音频）。

**方式一（推荐，归档按 RJ 号走）**：放进统一中间目录的作品子文件夹，文件名随意：

```
中间文件/日文字幕/RJ01012345/
├── track01.srt          ← 外部字幕（.srt 最省事）
├── track02.lrc          ← 日文 LRC 也可以
└── track03.ja.txt       ← 本工具自己提取的格式（可带同名 .timing.json）
```

**方式二**：命令行直接给文件/目录：

```bash
./start.sh translate "中间文件/日文字幕/RJ01012345"   # 整个作品
./start.sh translate "我的字幕.srt"                   # 单个文件
./start.sh translate "output/RJ01012345/字幕文件/日文" # 从作品包重跑
```

按后缀识别格式，时间轴来源如下：

| 格式 | 时间轴 |
|---|---|
| `.ja.txt` / `.ja.refined.txt` | 同名 `.timing.json`；没有则解析行首 `[HH:MM:SS]` |
| `.srt` | 标准 SRT 时间轴（`00:00:01,000 --> 00:00:03,500`） |
| `.lrc` | `[mm:ss.xx]`，结束时间取下一行 |
| `.txt` | 行首 `[HH:MM:SS]` 或 `[mm:ss]`；**纯文本无时间轴不支持** |

- 输出是 `<同名>.zh.txt` + `<同名>.lrc`，时间轴沿用输入 —— **不用把外部字幕改名成 `.ja.txt`**；
- 归档时：原始字幕进 `字幕文件/日文`，译文与 LRC 进 `字幕文件/中文`；`LRC成品库/<RJ号>/` 只收 LRC；
- 音频匹配：先按轨道号/文件名找；找不到时按 `input/<RJ号>/` 整目录取音频，所以字幕与音频文件名不同也没关系；
- 无时间轴/读不出来的文件会**跳过并提示**，不会卡住归档；
- `.zh.txt`、缓存、`refine_summary.md` 不会被当成输入；
- 日文 `.lrc` 若放在 LRC 输出目录里会被拒绝（防止翻译时覆盖原文件）。

## 模型

### 本地模型（默认，走 llama.cpp）

前置：安装 [llama.cpp](https://github.com/ggerganov/llama.cpp)（`brew install llama.cpp`），
或在 `config.yaml → translation.server.llama_server_bin` 指向 `llama-server` 可执行文件。
也可以直接用已运行的服务（`llama-server` :8080 或 LM Studio :1234），程序会优先复用。

然后在 `config.yaml → translation.registry` 里登记你自己的 gguf（`path` 支持 `~` 或相对项目根的路径）。
两个条目对应菜单的 Q6_K（≈11.3GB）与 IQ4_XS（≈7.6GB）。翻译时自动启动 `llama-server`，
任务结束自动关闭。

内存参考（24GB 机器）：

| 配置 | 权重 | KV（8192, fp16） | 备注 |
|---|---|---|---|
| Q6_K | 11.3GB | ~1.25GB | 质量最好，长时间跑吃紧 |
| IQ4_XS | 7.6GB | ~1.25GB | 省约 3.7GB，长任务推荐 |
| IQ4_XS + `ctx_size: 4096` + `-ctk q8_0 -ctv q8_0` | 7.6GB | ~0.6GB | 总占用约 8.2GB |

`config.yaml → translation.server` 可调 `ctx_size`、`n_gpu_layers`、`extra_args`；
`n_parallel: 1`、`kv_unified: true` 能省显存；不要加 `--mlock`。

### 云端 API（DeepSeek，可选）

见上文「用云端 API」。相关文件：`.env`（key）、`config.yaml → translation.registry.deepseek-api`。

诊断：`.venv/bin/python scripts/api_probe.py`（只发 1 次请求，打印 `finish_reason` / 用量 / 正文）。

> V4 默认开启思考模式，思维链会吃掉 `max_tokens` 导致正文为空，因此本项目对翻译和标题都默认关闭思考
> （`thinking_param` 的 `off: {type: disabled}`）；云端仍有服务端审核，极露骨内容可能被
> `content_filter` 拦下并中止本轮。

## 提示词定制

所有提示词都是 `prompts/` 下的独立文本文件，改完立即生效
（**已翻译过的行要加 `--force` 才会重译**）：

| 文件 | 作用 | 改动风险 |
|---|---|---|
| `prompts/system.txt` | 翻译人设 / 总体要求 | 低 |
| `prompts/subtitle_rules.txt` | 逐行格式约定（`[编号. 译文]`） | **高**，改坏会直接解析失败 |
| `prompts/repair.txt` | 单行补译 | 中 |
| `prompts/title.txt` | 标题翻译 | 低 |
| `prompts/style_asmr.txt` | 风格规范（用词/拟声/语气），追加到 system 末尾 | 低（清空 = 不启用） |

文件名和目录在 `config.yaml` 的 `translation.prompt` 里配置：

```yaml
translation:
  prompt:
    dir: prompts
    system: system.txt
    rules: subtitle_rules.txt
    repair: repair.txt
    title: title.txt
    style_file: style_asmr.txt   # 留空 = 不启用
```

预览最终提示词（不调用模型、不写文件）：

```bash
.venv/bin/python scripts/translate.py <任意 .ja.txt> --dry-run
.venv/bin/python scripts/translate.py <任意 .ja.txt> --dry-run --no-style   # 对比
```

`--style <文件>` 可临时换一套风格，`--no-style` 关闭风格层。

## 术语表

`glossary.txt`，每行 `日文->中文 #备注`（`#` 后可写语气/人称说明），翻译时注入提示词：

```
お兄ちゃん->哥哥 #爱称，保持亲密语气
先生->老师
はぁ->哈…
んっ->嗯…
```

## 验收与抢救

```bash
# 产出体检：格式 / 时间单调 / 拟声保留 / 日文残留 / 编号残留 / LRC 行数
.venv/bin/python scripts/verify_output.py output/ --check-translation

# 用缓存在原作品包内重建 .zh.txt（不重新调用模型）
.venv/bin/python scripts/rebuild_outputs.py --config config.yaml --output-root output

# 台本与提取结果的对齐统计
.venv/bin/python scripts/promptbook_check.py

# 手动归档（只预览，不动文件）
.venv/bin/python scripts/bundle.py --dry-run

# API 连通性 / 思考模式诊断（1 次请求）
.venv/bin/python scripts/api_probe.py
```

## 台本精修

有官方台本时，用台本 + 提取时间戳生成更准的日文字幕（已并入「只提取字幕」任务）。算法是确定性的：

1. 台本发现：`中间文件/日文字幕/<作品>` ↔ `promptbook/<作品>`（目录名归一化 + 轨道号兜底配对）
2. 对齐：单调 DP，允许"多条 ASR 片段 → 一条台本"，打分用双向覆盖度
3. 重组：以台本行为字幕单位，时间取所辖 ASR 片段的起止
4. 中段置信度交给模型判断：把两边都译成中文再比相似度（不让 Sakura 做 1/0 判断）
5. 产出：`<轨道>.ja.refined.txt` / `.ja.refined.json` / `.refine.md` / `refine_summary.md`

没有台本时会打一行警告直接跳过，不影响后续步骤。

## 常见问题

**Q：菜单里为什么只有 DeepSeek API？**
本地模型的 `path` 还空着，或者文件不存在。打开 `config.yaml` 填
`translation.registry.<名字>.path`（菜单里也会明确提示"本地模型还没填路径"）。

**Q：内存越跑越少？**
本地 14B + 8192 上下文的 KV 常驻约 1.25GB，加上权重（Q6_K 11.3GB）在 24GB 机器上接近上限。
换 IQ4_XS、把 `ctx_size` 降到 4096、给 KV 加 `-ctk q8_0 -ctv q8_0`，或直接用云端 API（不占内存）。

**Q：模型报"只返回思考内容 / 正文为空"？**
云端 V4 的思考模式把 `max_tokens` 吃光了。本项目默认已关闭思考；若手动开了，请调大配额或在
`config.yaml` 里关掉 `thinking_param`。

**Q：`input/` 里还有文件？**
归档只处理"已有中文字幕"的作品；没翻完的作品会留在工作区并提示缺哪几轨，补齐再跑即可。
扁平摆放（音频直接丢在 `input/` 根下）时，根目录的说明类文件不会被搬走。

**Q：LRC 第一行的标题看不到？**
如果该轨第一条字幕从 0 秒开始，标题行会被它顶掉。可调 `lrc_title_at_ms`，或设
`lrc_title_line: false` 只保留 `[ti:]`。

**Q：改了提示词但译文没变？**
缓存键是日文原文，改提示词不会让缓存失效 —— 加 `--force`（或删对应 `.zh.cache.json`）。

**Q：支持 Windows 吗？**
脚本按 macOS / bash 写的，Windows 未测试（WSL 可能可用）。云端 API 模式不依赖本地推理硬件。

## 隐私与合规

- 本工具不收集、不上传任何数据；本地模式全程离线。
- 云端模式只把字幕文本发给你配置的 API 服务商（不发音频）。
- 本项目不含任何受版权保护的音频、台本、模型或译文；模型许可见各自仓库
  （如 Sakura 系模型为 `cc-by-nc-sa-4.0`，商用前请自行确认）。

## 开发与贡献

```
subtitle.py          三任务入口（extract / all / translate）
start.sh             环境准备 + 运行 + 验收
config.yaml          模型 / 分段 / 翻译 / 目录名（默认值，人人可改）
config.local.yaml    个人机器覆盖（可选、不入库；如模型路径）
.env                 API key（不入库，需自己新建）
prompts/             所有提示词（可自由改）
glossary.txt         术语表
scripts/             extract_ja / translate / refine_script / bundle / verify_output / rebuild_outputs …
requirements.txt     运行依赖（start.sh 会自动安装）
LICENSE              MIT
```

- Python 3.12；除 torch/transformers 等模型依赖外尽量保持轻量。
- 提交前请确认没有把 `.env`、音频、字幕、模型或 `对照试验/` 带进仓库（`.gitignore` 已覆盖）。
- 欢迎 issue / PR；提示词改动的效果对比也欢迎分享。

## 许可

代码以 [MIT](LICENSE) 发布（使用前请把 `LICENSE` 里的 `<YOUR NAME>` 换成你的名字）。
模型、音频、台本、译文不在此许可范围内，归各自权利人所有。

## 致谢

- [anime-whisper](https://huggingface.co/litagin/anime-whisper)（日语 ASR）
- [SakuraLLM](https://github.com/SakuraLLM/SakuraLLM)（日中翻译模型）
- [llama.cpp](https://github.com/ggerganov/llama.cpp)、[uv](https://github.com/astral-sh/uv)
- [DeepSeek API](https://platform.deepseek.com/)（云端翻译）
- PyAV / Silero VAD / Hugging Face Hub
