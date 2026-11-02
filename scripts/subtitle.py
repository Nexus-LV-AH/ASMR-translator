#!/usr/bin/env python3
"""统一入口：三种运行方式（提取里已包含台本精修，没有台本会自动跳过）

    python scripts/subtitle.py extract   [音频...]   # 1 只提取字幕：音频 → 日文字幕 → 台本精修
    python scripts/subtitle.py all       [音频...]   # 2 完整运行：提取 + 台本精修 + 翻译 + 归档
    python scripts/subtitle.py translate [字幕...]   # 3 制作翻译：已有日文字幕 → 中文 LRC + 归档

为什么分阶段：
    音频里没有文字。日文提取靠 ASR 模型（anime-whisper，把声音变成带时间戳的日文），
    中文翻译靠翻译模型（Sakura 14B 或云端 API，只吃文本）。两个模型各管一段，所以物理上是两阶段。
    拆开的好处是 ASR 只需跑一次，之后换术语表/换翻译模型/调风格都不必重听音频。
    台本精修夹在提取里：有台本时用台本校正日文（时间戳仍来自 ASR），没有台本就跳过。

不带文件参数时：extract/all 只扫 input/（为空立即提示退出，不加载模型）；translate 扫 subtitles_ja/。

产物归档（只在出中文字幕的模式 2/3 生效；模式 1 不搬动任何文件）：
    整轮任务结束后一次性把工作区收进 output/<作品>/：
        字幕文件/日文/   日文字幕（ASR + 精修版 + timing/meta）
        字幕文件/中文/   中文字幕文本 + LRC（LRC 第一行是音频标题）
        音频/            原始音频原样（wav 仍是 wav、mp3 仍是 mp3）+ 封面图
        台本/            台本（没有台本就不建）
    搬完后清空工作目录，input/ 里不留文件，下个任务直接把新音频丢进 input/ 就能开跑。
    该作品只要还有轨道没有中文字幕，就**一轮都不搬**（文件留在工作区，补完再跑）。
    只移动、不删除；`--no-consolidate` 可关。

示例：
    ./start.sh                                    # 等价于 all（input/ 全部音频 → 中文 LRC → 归档）
    ./start.sh extract input/                     # 只出日文字幕（+台本精修）
    ./start.sh translate --track 03               # 只重翻第 3 轨
    ./start.sh translate "output/RJ001/字幕文件/日文/xx.ja.txt"  # 重跑已归档作品（写回同一作品包）
    ./start.sh all --limit 20                     # 只翻前 20 行试效果
"""
from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
PYTHON = sys.executable

logger = logging.getLogger("subtitle")


def default_audio_inputs(config_path: str | None = None) -> list[str]:
    """不给文件参数时：递归扫描 config 里的 input 目录（支持按作品建子文件夹）。

    只扫 input/，不再回退到项目根目录——否则 input/ 为空时会把 output/ 里已归档的音频、
    对照试验里的测试音频也当成待处理文件。
    """
    media_ext = (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg")
    input_dir = load_paths(Path(config_path) if config_path else None)["input"]
    media = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in media_ext) \
        if input_dir.is_dir() else []
    if media:
        logger.info("未指定文件，使用 %s 中的 %d 个音频", input_dir, len(media))
        return [str(p) for p in media]
    logger.warning("input/ 下没有音频文件（%s）", input_dir)
    return []


def build_extract_cmd(args: argparse.Namespace, targets: list[str] | None = None) -> list[str]:
    cmd = [PYTHON, str(SCRIPT_DIR / "extract_ja.py")] + _cfg_args(args)
    cmd += targets if targets is not None else (args.inputs or default_audio_inputs(args.config))
    if args.force or args.force_extract:
        cmd.append("--force")
    if args.asr_model:
        cmd += ["--model", args.asr_model]
    if args.device:
        cmd += ["--device", args.device]
    if args.segment:
        cmd += ["--segment", args.segment]
    if args.window_ms:
        cmd += ["--window-ms", str(args.window_ms)]
    if args.silence_gate_db is not None:
        cmd += ["--silence-gate-db", str(args.silence_gate_db)]
    if args.split_lines:
        cmd += ["--split-lines", args.split_lines]
    if args.output_dir:
        cmd += ["--output-dir", args.output_dir]
    return cmd


def build_translate_cmd(args: argparse.Namespace, targets: list[str]) -> list[str]:
    """翻译阶段的参数。

    刻意不传 --output-dir：让中文字幕写在各自 .ja.txt 的同目录，
    这样 output/作品A/xxx.ja.txt → output/作品A/xxx.lrc，作品文件夹结构自动保持。
    """
    cmd = [PYTHON, str(SCRIPT_DIR / "translate.py")] + _cfg_args(args)
    cmd += targets
    if args.force or args.force_translate:
        cmd.append("--force")
    if args.mt_model:
        cmd += ["--model", args.mt_model]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.bilingual:
        cmd.append("--bilingual")
    if args.glossary:
        cmd += ["--glossary", args.glossary]
    if args.batch_lines:
        cmd += ["--batch-lines", str(args.batch_lines)]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.no_auto_server:
        cmd.append("--no-auto-server")
    if args.lrc_flat:
        cmd.append("--lrc-flat")
    if args.no_lrc:
        cmd.append("--no-lrc")
    if args.no_consolidate:
        cmd.append("--no-consolidate")
    if getattr(args, "style", None):
        cmd += ["--style", str(args.style)]
    if getattr(args, "no_style", False):
        cmd.append("--no-style")
    if args.output_dir:
        # subtitle.py 的 --output-dir 指"日文字幕目录"，翻译阶段据此定位 ja.txt（中文/LRC 目录仍走 config）
        cmd += ["--subtitles-dir", args.output_dir]
    return cmd


def run(cmd: list[str]) -> int:
    logger.info("→ %s", " ".join(str(c) for c in cmd))
    return subprocess.call(cmd)


def load_cfg(config_path: Path | None = None) -> dict:
    """读配置（含同目录 config.local.yaml 的递归覆盖），失败返回空字典。"""
    cfg_path = config_path or (PROJECT_ROOT / "config.yaml")
    try:
        import translate as _T

        return _T.load_config(cfg_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 %s 失败（%s），使用默认值", cfg_path, exc)
        return {}


def load_paths(config_path: Path | None = None) -> dict[str, Path]:
    """读取 config.yaml 的 paths，返回三个根目录的绝对路径。"""
    cfg = load_cfg(config_path)
    paths = cfg.get("paths") or {}

    def _abs(key: str, default: str) -> Path:
        p = Path(paths.get(key) or default).expanduser()
        return p if p.is_absolute() else PROJECT_ROOT / p

    return {

        "input": _abs("input_dir", "input"),
        "subtitles": _abs("subtitles_dir", "subtitles_ja"),
        "promptbook": _abs("promptbook_dir", "promptbook"),
        "output": _abs("output_dir", "output"),
        "lrc": _abs("lrc_dir", "lrc_work"),
        "collect": _abs("collect_dir", "LRC成品库"),
    }


def ensure_workspace(paths: dict[str, Path]) -> None:
    """启动时就把工作区目录建好，让结构一眼可见（成品库等第一次跑完才出现容易让人困惑）。"""
    for key, label in (("input", "音频"), ("subtitles", "日文字幕"), ("output", "中文"),
                       ("lrc", "LRC 中转"), ("promptbook", "台本"), ("collect", "LRC 成品库")):
        d = paths.get(key)
        if not d:
            continue
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建%s目录失败：%s（%s）", label, d, exc)


def _cfg_args(args: argparse.Namespace) -> list[str]:
    """把 --config 透传给子进程（否则子进程会用默认 config.yaml）。"""
    return ["--config", str(args.config)] if getattr(args, "config", None) else []


def run_refine(args: argparse.Namespace, paths: dict[str, Path], *, skip_if_empty: bool) -> int:
    """台本精修阶段：台本目录不存在时直接跳过（不再报错中断整条流水线）。"""
    pb = paths["promptbook"]
    if skip_if_empty:
        if not pb.is_dir():
            logger.warning("没有台本目录 %s → 跳过台本精修，继续后面的流程", pb)
            return 0
        if not any(pb.rglob("*.txt")):
            logger.warning("没有台本（%s 下没有 .txt）→ 跳过台本精修，继续后面的流程", pb)
            return 0
    cmd = [PYTHON, str(SCRIPT_DIR / "refine_script.py")] + _cfg_args(args)
    if getattr(args, "mt_model", None):
        cmd += ["--model", str(args.mt_model)]
    if args.inputs:
        cmd += ["--work", args.inputs[0]]
    if args.track:
        cmd += ["--track", args.track]
    if args.no_model:
        cmd.append("--no-model")
    if skip_if_empty:
        cmd.append("--skip-if-empty")
    return run(cmd)


TRACK_RE = re.compile(r"^(?:track|tr|トラック|第)?[\s._-]*(\d{1,3})(?!\d)", re.I)


def track_number(name: str) -> str | None:
    """从文件名取轨道号（与 refine_script/bundle 同一套规则）。"""
    m = TRACK_RE.match(Path(name).name)
    return m.group(1).zfill(2) if m else None


LAST_MODEL_FILE = PROJECT_ROOT / ".models" / ".last_model"


def load_model_registry(config_path: Path | None = None) -> tuple[str, dict]:
    """读 config 里的翻译模型注册表，返回 (默认模型名, {名字: 条目})。

    默认值的优先级：环境变量 SUBTITLE_MODEL > config translation.model > translation.default
    """
    cfg = load_cfg(config_path)
    t = cfg.get("translation") or {}
    import os

    default = os.environ.get("SUBTITLE_MODEL") or t.get("model") or t.get("default") or ""
    return str(default), dict(t.get("registry") or {})


def remember_model(key: str) -> None:
    """把本次选择的模型记下来，下次（含非交互运行）默认用它。"""
    try:
        LAST_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_MODEL_FILE.write_text(key + "\n", encoding="utf-8")
    except OSError:
        pass


def recall_model() -> str | None:
    try:
        key = LAST_MODEL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def _model_size_hint(entry: dict) -> str:
    """优先用 config 里的 vram_gb；没有就按 gguf 文件大小算（只 stat，不加载）。

    本地没有的模型标注"需下载"，避免选完才发现要下好几个 GB。
    """
    p = entry.get("path")
    if p:
        q = Path(str(p)).expanduser()
        if q.exists():
            return f"{q.stat().st_size / 1024 ** 3:.1f}GB"
        return f"⚠️ 本地缺失（{q}）"
    if entry.get("gguf"):
        return f"需下载 {entry.get('gguf')}"
    if entry.get("vram_gb"):
        return f"{float(entry['vram_gb']):.1f}GB"
    return "大小未知"


def model_is_local(entry: dict) -> bool:
    """这个条目当前能不能用：本地 gguf 存在，或 API 条目（key 可选填，缺了会明确报错）。"""
    if entry.get("api"):
        return True
    if entry.get("path"):
        return Path(str(entry["path"])).expanduser().exists()
    if entry.get("local_dir") and entry.get("gguf"):
        d = Path(str(entry["local_dir"])).expanduser()
        if not d.is_absolute():
            d = PROJECT_ROOT / d
        return (d / str(entry["gguf"])).exists()
    return False


def api_key_present(entry: dict) -> bool:
    """API 条目的 key 是否已在环境或 .env 里配好。"""
    if not entry.get("api"):
        return True
    import os

    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import translate as _T

        _T.load_dotenv()
    except Exception:  # noqa: BLE001
        pass
    if str(entry.get("api_key") or "").strip():
        return True
    env_name = str(entry.get("api_key_env") or f"{str(entry.get('api')).upper()}_API_KEY")
    return bool(os.environ.get(env_name, "").strip())


def choose_model_interactively(config_path: Path | None, current: str | None) -> str | None:
    """任务开始时选择用哪个翻译模型。返回注册名（None = 用 config 默认）。

    - 已用 --mt-model 指定 → 直接用，不提问
    - 非交互（管道/定时任务）→ 用 config 默认，不提问
    """
    if current:
        return current
    default, registry = load_model_registry(config_path)
    if not registry:
        return None
    if not sys.stdin.isatty():
        # 非交互（IDE 运行按钮 / 被脚本调用 / 输入重定向）：不提问，但把用的哪个模型说清楚
        remembered = recall_model()
        chosen = remembered if (remembered and remembered in registry) else default
        if chosen not in registry:
            chosen = next(iter(registry))
        logger.info("非交互运行 → 使用翻译模型 %s（想换：--mt-model <名字>、环境变量 SUBTITLE_MODEL，"
                    "或在 config 里设 translation.model）", chosen)
        return chosen

    # 只列出本地已有的模型：没下载的备选不参与选择（避免误选后突然下几个 GB）
    # 可选项 = 本地已有文件 + 云端 API；没填路径/没下载的本地模型不参与选择
    local = {k: v for k, v in registry.items() if model_is_local(v or {})}
    hidden = [k for k in registry if k not in local]
    unconfigured = [k for k in hidden
                    if not (registry[k] or {}).get("api")
                    and not (registry[k] or {}).get("path")
                    and not (registry[k] or {}).get("local_dir")]
    if not local:
        logger.error("没有可用的翻译模型：本地模型的 path 还是空的，云端 API 也没配置")
        if unconfigured:
            logger.error("→ 打开 config.yaml，填 translation.registry.%s.path（你的 gguf 路径）", unconfigured[0])
        logger.error("→ 或者填 translation.registry.deepseek-api.api_key 走云端 API")
        return None
    remembered = recall_model()
    if remembered in local:
        default = remembered          # 上次选过的优先作为默认
    if default not in local:
        logger.warning("默认模型 %r 本地不存在，改用 %r", default, next(iter(local)))
        default = next(iter(local))
    registry = local
    keys = list(registry)
    print("=" * 60)
    print(" 选择翻译模型（数字越小质量越高、内存占用越大）：")
    for i, k in enumerate(keys, 1):
        entry = registry[k] or {}
        star = "（默认）" if k == default else ""
        if entry.get("api"):
            tag = "远程 API｜不占本机内存" if api_key_present(entry) else "远程 API｜⚠️ .env 里还没填 key"
            print(f"   {i}) {k}  {tag}{star}")
        else:
            print(f"   {i}) {k}  {_model_size_hint(entry)}{star}")
        note = entry.get("note") or entry.get("size")
        if note:
            print(f"        {note}")
    if unconfigured:
        print(f"   ⚠️ 本地模型还没填路径：{'、'.join(unconfigured)}")
        print("      → 打开 config.yaml 填 translation.registry.<名字>.path（你的 gguf 路径）")
    rest = [k for k in hidden if k not in unconfigured]
    if rest:
        print(f"   （另有 {len(rest)} 个未下载的备选模型未列出：{'、'.join(rest)}）")
    print("   提示：长任务/需要边用电脑边跑，选内存小的那个；模型会在任务结束时自动关闭。")
    print("=" * 60)
    while True:
        try:
            raw = input(f"输入 1~{len(keys)}（回车默认 {default or keys[0]}）：").strip()
        except EOFError:
            return default or keys[0]
        if not raw:
            return default or keys[0]
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        if raw in registry:
            return raw
        print(f"请输入 1~{len(keys)}，或直接输入模型名")


def choose_mode_interactively() -> str:
    """不带参数运行时，直接问要执行哪个任务。"""
    print("=" * 60)
    print(" 请选择任务：")
    print("   1) 只提取字幕        音频 → 日文字幕 → 台本精修（有台本时），不翻译、不归档")
    print("   2) 完整运行          提取 + 台本精修 + 翻译 → 中文 LRC → 结束后归档")
    print("   3) 制作翻译          已有日文字幕 → 中文 LRC → 结束后归档")
    print("=" * 60)
    mapping = {"1": "extract", "2": "all", "3": "translate"}
    while True:
        try:
            choice = input("输入 1 / 2 / 3（回车默认 2）：").strip() or "2"
        except EOFError:
            return "all"
        if choice in mapping:
            return mapping[choice]
        print("请输入 1、2 或 3")


def main() -> int:
    p = argparse.ArgumentParser(
        description="音声 → 日文字幕 → 台本精修 → 中文字幕（extract=只提取字幕 ｜ all=完整运行 ｜ translate=制作翻译）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("mode", nargs="?", default=None,
                   choices=["all", "extract", "translate"],
                   help="extract=只提取字幕（含台本精修）｜ all=完整运行（提取+精修+翻译+归档）"
                        "｜ translate=制作翻译（已有日文字幕 → 中文 LRC + 归档）")
    p.add_argument("inputs", nargs="*", help="extract/all 传音频或目录（默认 input/）；translate 传 .ja.txt 或目录（默认 subtitles_ja/）")

    g = p.add_argument_group("提取阶段（anime-whisper）")
    g.add_argument("--asr-model", default=None, help="anime-whisper / kotoba-whisper-v2.2 / whisper-ja-1.5b")
    g.add_argument("--device", default=None, help="auto | mps | cpu")
    g.add_argument("--segment", default=None, choices=["fixed", "vad"], help="分段策略")
    g.add_argument("--window-ms", type=int, default=None, help="窗口长度（毫秒）")
    g.add_argument("--silence-gate-db", type=float, default=None, help="静音窗口门限（0=不过滤）")
    g.add_argument("--split-lines", default=None, choices=["punctuation", "none"], help="窗口内是否按标点细分")
    g.add_argument("--force-extract", action="store_true", help="只强制重跑提取")

    t = p.add_argument_group("翻译阶段（Sakura 14B）")
    t.add_argument("--mt-model", default=None,
                   help="翻译模型注册名（跳过开头的交互选择）；默认每次运行都会让你选，回车用 config 默认")
    t.add_argument("--bilingual", action="store_true", help="LRC 中日对照")
    t.add_argument("--glossary", default=None, help="术语表文件")
    t.add_argument("--batch-lines", type=int, default=None, help="每批翻译行数")
    t.add_argument("--limit", type=int, default=None, help="只翻译前 N 行（试跑）")
    t.add_argument("--dry-run", action="store_true", help="只打印提示词，不调用模型")
    t.add_argument("--no-auto-server", action="store_true", help="不自动启动 llama-server")
    t.add_argument("--force-translate", action="store_true", help="只强制重翻译（忽略缓存）")
    t.add_argument("--no-lrc", action="store_true",
                   help="只更新 output 里的 .zh.txt/缓存，不重写 LRC（保护手动改过的 LRC）")
    t.add_argument("--no-consolidate", action="store_true",
                   help="不自动归档到 output/<作品>/{字幕文件,音频,台本}，文件留在顶层工作区")
    t.add_argument("--style", default=None, help="翻译风格规范文件（默认 config translation.prompt.style_file）")
    t.add_argument("--no-style", action="store_true", help="不加载风格规范")

    c = p.add_argument_group("通用")
    c.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"), help="配置文件路径")
    c.add_argument("--force", action="store_true", help="两个阶段都强制重跑")
    c.add_argument("--output-dir", default=None,
                   help="日文字幕目录（默认 config paths.subtitles_dir）；中文文本/LRC 目录见 config paths")
    c.add_argument("--lrc-flat", action="store_true", help="所有 LRC 平铺到 lrc 根目录")
    c.add_argument("--track", default=None, help="只处理指定轨道号（refine / translate 都支持，如 05）")
    c.add_argument("--no-model", action="store_true",
                   help="台本精修只用规则、不加载模型（配合 extract）")
    c.add_argument("-v", "--verbose", action="store_true")

    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)

    mode = args.mode
    if mode is None:
        mode = choose_mode_interactively() if sys.stdin.isatty() else "all"

    # 任务开始时先确认用哪个翻译模型（extract 里也跑台本精修，同样要模型）
    if not (args.no_model and mode == "extract"):
        chosen = choose_model_interactively(Path(args.config), args.mt_model)
        args.mt_model = chosen
        if chosen:
            _default, reg = load_model_registry(Path(args.config))
            entry = reg.get(chosen) or {}
            if sys.stdin.isatty():
                remember_model(chosen)
            logger.info("翻译模型：%s（%s）%s", chosen, _model_size_hint(entry), entry.get("note") or "")

    paths = load_paths(Path(args.config))
    ensure_workspace(paths)

    logger.info("运行方式：%s", {"extract": "只提取字幕（含台本精修）", "all": "完整运行（提取+精修+翻译+归档）",
                                "translate": "制作翻译（+归档）"}[mode])
    logger.info("日文字幕目录：%s", paths["subtitles"])
    logger.info("中文文本目录：%s ｜ LRC 中转目录：%s", paths["output"], paths["lrc"])

    if mode in ("all", "extract"):
        targets = args.inputs or default_audio_inputs(args.config)
        if not targets:
            logger.error("没有可提取的内容：input/ 是空的（不会加载模型）")
            logger.error("→ 若你已经有日文字幕、只差翻译：python3 subtitle.py translate")
            logger.error("→ 若已归档想重跑：python3 subtitle.py translate \"output/<作品>/字幕文件/日文/<轨道>.ja.txt\"")
            logger.error("→ 或者把音频按作品放进 input/<RJ号>/ 再运行")
            return 2
        rc = run(build_extract_cmd(args, targets))
        if rc != 0:
            logger.error("提取阶段失败（退出码 %s），已中止", rc)
            return rc
        # 提取完接着做台本精修（1/2 都做）；没有台本自动跳过，继续后面的翻译
        rc = run_refine(args, paths, skip_if_empty=True)
        if rc != 0:
            logger.error("台本精修失败（退出码 %s），已中止", rc)
            return rc

    if mode in ("all", "translate"):
        # all：翻译刚提取出来的日文字幕目录（不是用户给的 input 路径）
        # translate：翻译命令行给的 .ja.txt/目录，默认扫日文字幕目录
        if mode == "all":
            targets = [str(Path(args.output_dir).expanduser() if args.output_dir and Path(args.output_dir).is_absolute() else (PROJECT_ROOT / args.output_dir if args.output_dir else paths["subtitles"]))]
        elif args.track and not args.inputs:
            targets = [str(f) for f in sorted(paths["subtitles"].rglob("*.ja.txt"))
                       if not f.name.endswith(".ja.refined.txt") and track_number(f.name) == args.track.zfill(2)]
            if not targets:
                logger.error("在 %s 下找不到轨道 %s 的日文字幕", paths["subtitles"], args.track)
                return 2
        else:
            targets = args.inputs or [str(paths["subtitles"])]
        rc = run(build_translate_cmd(args, targets))
        if rc != 0:
            logger.error("翻译阶段失败（退出码 %s）", rc)
            return rc

    logger.info("全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
