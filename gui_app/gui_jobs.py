#!/usr/bin/env python3
"""GUI 任务编排：extract_ja → refine_script → translate，收日志、算进度、可取消。

不调用 subtitle.py all：GUI 按作品分阶段执行，翻译只针对本次作品，
阶段边界清晰；命令与 CLI 的参数完全一致。
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import paths_util  # noqa: E402

import gui_fs  # noqa: E402
from gui_platform import SleepInhibitor, child_env, kill_process_tree, spawn_kwargs  # noqa: E402

STAGE_LABELS = {
    "preparing": "准备",
    "extract": "提取日文字幕",
    "refine": "台本精修",
    "translate": "翻译",
    "archive": "归档",
    "done": "完成",
}
_STAGE_WEIGHTS = {
    "all": {"extract": (0.0, 45.0), "refine": (45.0, 55.0), "translate": (55.0, 90.0), "archive": (90.0, 100.0)},
    "extract": {"extract": (0.0, 92.0), "refine": (92.0, 100.0)},
    "translate": {"translate": (0.0, 90.0), "archive": (90.0, 100.0)},
}


@dataclass
class Phase:
    name: str
    label: str
    argv: list[str]

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "label": self.label, "command": shlex.join(self.argv)}


def _script(name: str) -> str:
    return str(SCRIPT_DIR / name)


def build_commands(mode: str, paths: gui_fs.GuiPaths, work: str, model: str,
                   options: dict[str, Any]) -> list[Phase]:
    """构造三个阶段命令；纯函数，方便单测。"""
    if mode not in ("extract", "all", "translate"):
        raise ValueError(f"未知任务类型：{mode}")
    py = sys.executable
    cfg = str(paths.config)
    work = str(work)
    force = bool(options.get("force"))
    if mode in ("all", "translate") and not str(model or "").strip():
        raise ValueError("这个任务需要翻译模型：请先在设置页配置云端 API 或选择本地模型")
    phases: list[Phase] = []

    if mode in ("extract", "all"):
        extract = [py, _script("extract_ja.py"), "--config", cfg, str(paths.input / work)]
        if force:
            extract.append("--force")
        phases.append(Phase("extract", "提取日文字幕（ASR）", extract))

        refine = [py, _script("refine_script.py"), "--config", cfg,
                  "--work", work, "--skip-if-empty"]
        if options.get("refine_no_model") or not str(model or "").strip():
            refine.append("--no-model")
        else:
            refine += ["--model", str(model)]
        phases.append(Phase("refine", "台本精修", refine))

    if mode in ("all", "translate"):
        translate = [py, _script("translate.py"), "--config", cfg,
                     str(paths.ja / work), "--model", str(model)]
        if force:
            translate.append("--force")
        if options.get("bilingual"):
            translate.append("--bilingual")
        limit = options.get("limit")
        if limit:
            translate += ["--limit", str(int(limit))]
        if options.get("no_consolidate"):
            translate.append("--no-consolidate")
        phases.append(Phase("translate", "翻译", translate))
    return phases


# ---------------------------------------------------------------- 进度解析
class ProgressParser:
    """从三个子脚本的日志里提取进度；解析不出来就保持不确定，不伪造。"""

    def __init__(self, mode: str, *, total_files: int = 0, total_tracks: int = 0):
        self.mode = mode
        self.phase = ""
        self.stage = "preparing"
        self.stage_label = STAGE_LABELS["preparing"]
        self.percent = 0.0
        self.indeterminate = True
        self.current_file = ""
        self.eta_seconds: int | None = None
        self.total_files = int(total_files or 0)
        self.completed_files = 0
        self.total_tracks = int(total_tracks or 0)
        self.completed_tracks = 0
        self.total_lines = 0
        self.done_lines = 0
        self.file_inner = 0.0
        self.extract_sub = 0.0
        self.refine_total = 0
        self.refine_done = 0
        self.refine_inner = 0.0
        self.refine_skipped = False
        self.archive_started = False
        self.archive_done = False
        self.archive_skipped = False
        self.notices: list[str] = []

    # -- 阶段
    def _range(self, stage: str) -> tuple[float, float]:
        return _STAGE_WEIGHTS.get(self.mode, {}).get(stage, (0.0, 100.0))

    def begin_phase(self, phase: str) -> None:
        self.phase = phase
        if phase == "extract":
            self.stage = "extract"
            self.completed_files = 0
            self.file_inner = 0.0
            self.extract_sub = 0.0
        elif phase == "refine":
            self.stage = "refine"
            self.refine_done = 0
            self.refine_inner = 0.0
            self.refine_skipped = False
        elif phase == "translate":
            self.stage = "translate"
            self.completed_tracks = 0
            self.done_lines = 0
            self.total_lines = 0
            self.file_inner = 0.0
            self.archive_started = False
            self.archive_done = False
            self.archive_skipped = False
        self.stage_label = STAGE_LABELS.get(self.stage, self.stage)
        self.indeterminate = True
        self._recompute()

    def end_phase(self, phase: str) -> None:
        if phase == "extract":
            self.completed_files = max(self.completed_files, self.total_files)
            self.file_inner = 1.0
        elif phase == "refine":
            self.refine_done = max(self.refine_done, self.refine_total)
            self.refine_inner = 1.0
        elif phase == "translate":
            self.completed_tracks = max(self.completed_tracks, self.total_tracks)
            self.file_inner = 1.0
            if not self.archive_started:
                self.archive_skipped = True
        self.indeterminate = False
        self._recompute()

    def set_total_tracks(self, n: int) -> None:
        self.total_tracks = max(0, int(n or 0))
        self._recompute()

    # -- 日志
    def feed(self, phase: str, line: str) -> None:
        text = str(line or "")
        if phase == "extract":
            self._feed_extract(text)
        elif phase == "refine":
            self._feed_refine(text)
        elif phase == "translate":
            self._feed_translate(text)
        self._recompute()

    def _feed_extract(self, text: str) -> None:
        m = re.search(r"待处理文件\s*(\d+)\s*个", text)
        if m:
            self.total_files = max(self.total_files, int(m.group(1)))
            return
        m = re.search(r"开始处理：(.+?)\s*$", text)
        if m:
            self.current_file = m.group(1).strip()
            self.file_inner = 0.0
            self.extract_sub = 0.0
            return
        if "[1/4]" in text:
            self.extract_sub = max(self.extract_sub, 0.02)
            self.file_inner = max(self.file_inner, self.extract_sub)
        elif "[2/4]" in text:
            self.extract_sub = max(self.extract_sub, 0.08)
            self.file_inner = max(self.file_inner, self.extract_sub)
        elif "[3/4]" in text:
            self.extract_sub = max(self.extract_sub, 0.15)
            self.file_inner = max(self.file_inner, self.extract_sub)
        elif "[4/4]" in text:
            self.extract_sub = max(self.extract_sub, 0.95)
            self.file_inner = max(self.file_inner, self.extract_sub)
        m = re.search(r"转写进度\s*(\d+)\s*/\s*(\d+)", text)
        if m:
            total = max(1, int(m.group(2)))
            frac = min(1.0, max(0.0, int(m.group(1)) / total))
            self.file_inner = max(self.file_inner, 0.15 + 0.8 * frac)
            self.extract_sub = max(self.extract_sub, self.file_inner)
        if re.search(r"完成：\s*\d+\s*条字幕", text) or "跳过（已存在" in text:
            self.completed_files += 1
            self.current_file = ""
            self.file_inner = 0.0
            self.extract_sub = 0.0

    def _feed_refine(self, text: str) -> None:
        if "跳过台本精修" in text or "没有台本" in text:
            self.refine_skipped = True
            return
        m = re.search(r"待精修\s*(\d+)\s*个轨道", text)
        if m:
            self.refine_total = max(self.refine_total, int(m.group(1)))
            return
        m = re.search(r"作品\s+(.+?)：\s*(\d+)\s*个轨道", text)
        if m:
            self.current_file = m.group(1).strip()
            return
        m = re.search(r"判定进度\s*(\d+)\s*/\s*(\d+)", text)
        if m:
            total = max(1, int(m.group(2)))
            self.refine_inner = max(self.refine_inner, min(1.0, int(m.group(1)) / total))
            return
        m = re.search(r"汇总（累计\s*(\d+)\s*个轨道）", text)
        if m:
            self.refine_done = max(self.refine_done, int(m.group(1)))

    def _feed_translate(self, text: str) -> None:
        m = re.search(r"翻译\s+(.+?)：\s*(\d+)\s*行", text)
        if m:
            self.current_file = m.group(1).strip()
            self.total_lines = max(0, int(m.group(2)))
            self.done_lines = 0
            self.file_inner = 0.0
            return
        m = re.search(r"进度\s*(\d+)\s*/\s*(\d+)\s*行", text)
        if m:
            done, total = int(m.group(1)), max(1, int(m.group(2)))
            self.done_lines = done
            self.total_lines = max(self.total_lines, total)
            self.file_inner = min(1.0, done / total)
            m2 = re.search(r"剩余约\s*(\d+)\s*秒", text)
            if m2:
                self.eta_seconds = int(m2.group(1))
            return
        if re.search(r"完成：", text):
            self.completed_tracks += 1
            self.current_file = ""
            self.done_lines = 0
            self.total_lines = 0
            self.file_inner = 0.0
            return
        if "任务结束，开始归档工作区" in text:
            self.archive_started = True
            self.stage = "archive"
            self.stage_label = STAGE_LABELS["archive"]
            return
        if "已归档作品" in text:
            self.archive_done = True
            self.archive_started = True
            self.stage = "archive"
            self.stage_label = STAGE_LABELS["archive"]
            return
        if "本轮不归档" in text or "不归档" in text:
            self.archive_skipped = True

    # -- 总进度
    def _inner(self) -> float | None:
        stage = self.stage
        if stage == "extract":
            if not self.total_files:
                return None
            per = 1.0 / self.total_files
            return min(1.0, (self.completed_files + self.file_inner) * per)
        if stage == "refine":
            if self.refine_skipped:
                return 1.0
            if not self.refine_total:
                return None
            return min(1.0, (self.refine_done + self.refine_inner) / self.refine_total)
        if stage == "translate":
            if not self.total_tracks:
                return None
            per = 1.0 / self.total_tracks
            return min(1.0, (self.completed_tracks + self.file_inner) * per)
        if stage == "archive":
            if self.archive_done:
                return 1.0
            return 0.35 if self.archive_started else None
        return None

    def _recompute(self) -> None:
        inner = self._inner()
        start, end = self._range(self.stage)
        if inner is None:
            self.percent = max(self.percent, start)
            self.indeterminate = True
        else:
            pct = start + (end - start) * max(0.0, min(1.0, inner))
            self.percent = max(self.percent, min(100.0, pct))
            self.indeterminate = False
        self.stage_label = STAGE_LABELS.get(self.stage, self.stage)

    def snapshot(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "stage_label": self.stage_label,
            "percent": round(min(100.0, max(0.0, self.percent)), 1),
            "indeterminate": bool(self.indeterminate),
            "current_file": self.current_file,
            "eta_seconds": self.eta_seconds,
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "total_tracks": self.total_tracks,
            "completed_tracks": self.completed_tracks,
            "total_lines": self.total_lines,
            "done_lines": self.done_lines,
            "archive_started": self.archive_started,
            "archive_done": self.archive_done,
            "archive_skipped": self.archive_skipped,
            "refine_skipped": self.refine_skipped,
            "notices": list(self.notices),
        }


# ---------------------------------------------------------------- Job
@dataclass
class Job:
    id: str
    mode: str
    work: str
    model: str
    options: dict[str, Any]
    status: str = "preparing"
    stage: str = "preparing"
    stage_label: str = "准备"
    percent: float = 0.0
    indeterminate: bool = True
    current_file: str = ""
    eta_seconds: int | None = None
    error: str = ""
    exit_code: int | None = None
    created_at: str = ""
    started_at: str = ""
    ended_at: str = ""
    log_file: str = ""
    commands: list[dict[str, str]] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    total_files: int = 0
    completed_files: int = 0
    total_tracks: int = 0
    completed_tracks: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mode": self.mode,
            "work": self.work,
            "model": self.model,
            "options": dict(self.options),
            "status": self.status,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "percent": self.percent,
            "indeterminate": self.indeterminate,
            "current_file": self.current_file,
            "eta_seconds": self.eta_seconds,
            "error": self.error,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "log_file": self.log_file,
            "commands": list(self.commands),
            "results": dict(self.results),
            "total_files": self.total_files,
            "completed_files": self.completed_files,
            "total_tracks": self.total_tracks,
            "completed_tracks": self.completed_tracks,
        }


class JobManager:
    """单任务管理器：一个 GUI 同时只跑一个任务。"""

    def __init__(self, root: str | Path, paths: gui_fs.GuiPaths, *,
                 command_builder: Callable[..., list[Phase]] | None = None,
                 max_logs: int = 5000):
        self.root = Path(root).resolve()
        self.paths = paths
        self._build_commands = command_builder or build_commands
        self._lock = threading.RLock()
        self._job: Job | None = None
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=max_logs)
        self._seq = 0
        self._file = None
        self._last_job: dict[str, Any] | None = self._load_last_job()

    # ------------------------------------------------------------ 持久化
    @property
    def last_job_path(self) -> Path:
        return self.paths.gui / "last_job.json"

    def _load_last_job(self) -> dict[str, Any] | None:
        data = gui_fs.read_json(self.last_job_path, None)
        if not isinstance(data, dict) or not data.get("id"):
            return None
        if data.get("status") in ("preparing", "running", "cancelling"):
            data["status"] = "interrupted"
            data["error"] = "上次任务被中断（图形界面服务已重启）；请查看日志后重试"
        return data

    def _persist(self, job: Job) -> None:
        try:
            gui_fs.write_json(self.last_job_path, job.to_dict())
        except OSError:
            pass

    # ------------------------------------------------------------ 日志
    def _append(self, job: Job, text: str, *, level: str | None = None) -> None:
        text = str(text).rstrip("\n")
        if not text:
            return
        lv = (level or "").upper()
        if not lv:
            if "[ERROR]" in text or "❌" in text:
                lv = "ERROR"
            elif "[WARNING]" in text or "⚠️" in text:
                lv = "WARNING"
            elif "[DEBUG]" in text:
                lv = "DEBUG"
            else:
                lv = "INFO"
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "t": time.strftime("%H:%M:%S"),
                "level": lv,
                "text": text,
            }
            self._logs.append(entry)
            if self._file is not None:
                try:
                    self._file.write(f"{entry['t']} [{lv}] {text}\n")
                    self._file.flush()
                except OSError:
                    pass

    def log_entries(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            entries = [e for e in self._logs if e["seq"] > int(since or 0)]
            first = self._logs[0]["seq"] if self._logs else self._seq + 1
            truncated = bool(self._logs) and int(since or 0) + 1 < first
            return {
                "entries": entries,
                "last_seq": self._seq,
                "truncated": truncated,
            }

    # ------------------------------------------------------------ 运行
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._job and self._job.status in ("preparing", "running", "cancelling"))

    @property
    def current_job(self) -> Job | None:
        with self._lock:
            return self._job

    def last_job(self) -> dict[str, Any] | None:
        with self._lock:
            if self._job is not None:
                return self._job.to_dict()
            return dict(self._last_job) if self._last_job else None

    def snapshot(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            job = self._job.to_dict() if self._job else (dict(self._last_job) if self._last_job else None)
        logs = self.log_entries(since)
        return {"job": job, "logs": logs}

    def start(self, *, mode: str, work: str, model: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = dict(options or {})
        with self._lock:
            if self.is_running():
                raise RuntimeError("已有任务在运行，请等它结束或先取消")
            phases = self._build_commands(mode, self.paths, work, model, options)
            job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
            log_name = f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[\\\\/:*?\"<>|]+', '_', work) or 'job'}.log"
            log_path = self.paths.logs / log_name
            self.paths.logs.mkdir(parents=True, exist_ok=True)
            job = Job(
                id=job_id,
                mode=mode,
                work=work,
                model=str(model or ""),
                options=options,
                created_at=gui_fs.now_iso(),
                log_file=str(log_path),
                commands=[p.as_dict() for p in phases],
            )
            self._job = job
            self._cancel.clear()
            self._logs.clear()
            try:
                self._file = log_path.open("a", encoding="utf-8")
            except OSError:
                self._file = None
            self._persist(job)
            self._append(job, f"任务开始：{mode} / 作品 {work} / 模型 {model or '（不用模型）'}", level="INFO")
            self._thread = threading.Thread(target=self._run, args=(job, phases), daemon=True)
            self._thread.start()
            return job.to_dict()

    def _count_audio(self, work: str) -> int:
        root = self.paths.input / work
        if not root.is_dir():
            return 0
        return sum(1 for p in root.rglob("*") if p.is_file() and gui_fs.is_audio(p.name))

    def _count_tracks(self, work: str) -> int:
        try:
            return len(paths_util.iter_subtitles(self.paths.ja / work))
        except Exception:  # noqa: BLE001
            return 0

    def _apply(self, job: Job, state: dict[str, Any]) -> None:
        with self._lock:
            job.stage = str(state.get("stage") or job.stage)
            job.stage_label = str(state.get("stage_label") or job.stage_label)
            job.percent = max(job.percent, float(state.get("percent") or 0.0))
            job.indeterminate = bool(state.get("indeterminate"))
            job.current_file = str(state.get("current_file") or "")
            job.eta_seconds = state.get("eta_seconds")
            job.total_files = int(state.get("total_files") or 0)
            job.completed_files = int(state.get("completed_files") or 0)
            job.total_tracks = int(state.get("total_tracks") or 0)
            job.completed_tracks = int(state.get("completed_tracks") or 0)

    def _apply_phase(self, job: Job, parser: ProgressParser) -> None:
        self._apply(job, parser.snapshot())

    def _run(self, job: Job, phases: list[Phase]) -> None:
        parser = ProgressParser(job.mode, total_files=self._count_audio(job.work),
                                total_tracks=self._count_tracks(job.work))
        with self._lock:
            job.status = "running"
            job.started_at = gui_fs.now_iso()
        self._persist(job)
        error = ""
        try:
            with SleepInhibitor(enabled=bool(job.options.get("prevent_sleep", True)),
                                log=lambda m: self._append(job, m, level="WARNING")):
                for phase in phases:
                    if self._cancel.is_set() or self._shutdown.is_set():
                        break
                    parser.begin_phase(phase.name)
                    self._apply_phase(job, parser)
                    self._append(job, f"$ {shlex.join(phase.argv)}", level="INFO")
                    try:
                        proc = subprocess.Popen(
                            phase.argv,
                            cwd=str(self.root),
                            env=child_env(self.root),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            bufsize=1,
                            **spawn_kwargs(),
                        )
                    except OSError as exc:
                        error = f"无法启动 {phase.name} 阶段：{exc}"
                        self._append(job, error, level="ERROR")
                        break
                    with self._lock:
                        self._proc = proc
                    try:
                        if proc.stdout is not None:
                            for line in proc.stdout:
                                self._append(job, line)
                                parser.feed(phase.name, line)
                                self._apply_phase(job, parser)
                                if self._cancel.is_set():
                                    break
                        rc = proc.wait()
                    finally:
                        try:
                            if proc.stdout is not None:
                                proc.stdout.close()
                        except OSError:
                            pass
                        with self._lock:
                            self._proc = None
                    if self._cancel.is_set() or self._shutdown.is_set():
                        break
                    if rc != 0:
                        error = f"{phase.label}失败（退出码 {rc}）；详情见日志"
                        self._append(job, error, level="ERROR")
                        break
                    parser.end_phase(phase.name)
                    self._apply_phase(job, parser)
                    if phase.name == "extract" and job.mode == "all":
                        parser.set_total_tracks(self._count_tracks(job.work))
                        self._apply_phase(job, parser)
        except Exception as exc:  # noqa: BLE001
            error = f"任务异常：{type(exc).__name__}: {exc}"
            self._append(job, error, level="ERROR")
        finally:
            with self._lock:
                cancelled = self._cancel.is_set() or self._shutdown.is_set()
                if cancelled:
                    job.status = "cancelled"
                    job.stage_label = "已取消"
                    self._append(job, "任务已取消", level="WARNING")
                elif error:
                    job.status = "error"
                    job.error = error
                else:
                    job.status = "done"
                    job.stage = "done"
                    job.stage_label = STAGE_LABELS["done"]
                    job.percent = 100.0
                    job.indeterminate = False
                    self._append(job, "任务完成", level="INFO")
                job.ended_at = gui_fs.now_iso()
                try:
                    cfg, _ = gui_fs.load_config(self.root, self.paths.config)
                    job.results = gui_fs.scan_results(self.paths, cfg, job.work, job.mode)
                except Exception as exc:  # noqa: BLE001
                    self._append(job, f"扫描结果失败：{type(exc).__name__}: {exc}", level="WARNING")
                self._persist(job)
                self._last_job = job.to_dict()
                if self._file is not None:
                    try:
                        self._file.close()
                    except OSError:
                        pass
                    self._file = None
                self._proc = None

    # ------------------------------------------------------------ 取消/退出
    def cancel(self) -> bool:
        with self._lock:
            if not self._job or self._job.status not in ("preparing", "running", "cancelling"):
                return False
            if self._job.status != "cancelling":
                self._job.status = "cancelling"
                self._append(self._job, "正在取消任务…", level="WARNING")
            self._cancel.set()
            proc = self._proc
            job = self._job
        if proc is not None:
            def _kill():
                kill_process_tree(proc)
            threading.Thread(target=_kill, daemon=True).start()
        return True

    def shutdown(self) -> None:
        self._shutdown.set()
        self._cancel.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                kill_process_tree(proc)
            except Exception:  # noqa: BLE001
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=20)
        with self._lock:
            if self._job is not None and self._job.status not in ("done", "error", "cancelled", "interrupted"):
                self._job.status = "interrupted"
                self._job.ended_at = gui_fs.now_iso()
                self._job.error = self._job.error or "服务退出，任务被中断"
                self._last_job = self._job.to_dict()
                self._persist(self._job)
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
