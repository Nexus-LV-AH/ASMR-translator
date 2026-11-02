#!/usr/bin/env python3
"""模型直连下载器（走 hf-mirror，多线程 + 断点续传）。

为什么不用 huggingface_hub：
    本机实测 hub 的分块/Xet 传输只有 ~0.3MB/s，而直连 hf-mirror 可达 ~4MB/s。
    模型体积 1.5GB，差距是 80 分钟 vs 6 分钟，因此自带一个下载器。

用法：
    python scripts/fetch_model.py anime-whisper
    python scripts/fetch_model.py --hf-id litagin/anime-whisper --dest .models/anime-whisper
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

DEFAULT_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")

# 需要的文件（含 tokenizer / config / 权重）；figs、README 等无关文件不下载
REQUIRED_FILES = [
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "added_tokens.json",
    "special_tokens_map.json",
    "normalizer.json",
]


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _remote_size(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length else None
    except Exception:
        return None


def _probe(url: str) -> tuple[int | None, bool]:
    """探测远端文件大小与是否支持 Range。"""
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            length = resp.headers.get("Content-Length")
            accepts = (resp.headers.get("Accept-Ranges", "").lower() == "bytes")
            return (int(length) if length else None, accepts)
    except Exception:
        return None, False


def _download_range(url: str, target: Path, start: int, end: int, label: str) -> None:
    """下载 [start, end] 区间，支持续传（target 已存在则在已有字节基础上继续）。"""
    expected = end - start + 1
    have = target.stat().st_size if target.exists() else 0
    if have >= expected:
        return
    headers = {"User-Agent": "Mozilla/5.0", "Range": f"bytes={start + have}-{end}"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp, open(
        target, "ab"
    ) as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    if target.stat().st_size != expected:
        raise IOError(f"{label} 分片大小不符：{target.stat().st_size} != {expected}")


def download_file_parallel(
    url: str,
    dest: Path,
    label: str,
    connections: int = 8,
    min_size_for_parallel: int = 100 << 20,
) -> None:
    """大文件并行分段下载（本机实测：单连接 0.1MB/s vs 8 线程约 45MB/s）。"""
    size, accepts_ranges = _probe(url)
    if not size or not accepts_ranges or size < min_size_for_parallel:
        return download_file(url, dest, label)

    tmp_parts = [dest.with_suffix(dest.suffix + f".part{i}") for i in range(connections)]
    chunk = (size + connections - 1) // connections
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(idx: int) -> None:
        start = idx * chunk
        end = min(start + chunk - 1, size - 1)
        if start > end:
            return
        try:
            _download_range(url, tmp_parts[idx], start, end, f"{label}[{idx}]")
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,), name=f"dl{i}") for i in range(connections)]
    for t in threads:
        t.start()

    # 进度轮询
    while any(t.is_alive() for t in threads):
        done = sum(p.stat().st_size for p in tmp_parts if p.exists())
        sys.stdout.write(f"\r  {label}: {done / size * 100:5.1f}%  {_human(done)}/{_human(size)}   ")
        sys.stdout.flush()
        time.sleep(1.0)

    for t in threads:
        t.join()
    sys.stdout.write("\n")
    if errors:
        raise errors[0]

    total = sum(p.stat().st_size for p in tmp_parts if p.exists())
    if total != size:
        raise IOError(f"{label} 合并前大小校验失败：{total} != {size}")

    with open(dest, "wb") as out:
        for p in tmp_parts:
            with open(p, "rb") as f:
                while True:
                    buf = f.read(1 << 22)
                    if not buf:
                        break
                    out.write(buf)
            p.unlink(missing_ok=True)


def download_one(url: str, dest: Path, label: str) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    try:
        download_file_parallel(url, dest, label)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  ⚠️ {label} 并行下载失败（{exc}），退回单连接续传重试")
        download_file(url, dest, label)


def download_file(url: str, dest: Path, label: str, show_progress: bool = True) -> None:
    """断点续传下载单个文件（.part 临时文件 + 完成后原子替换）。"""
    if dest.exists() and dest.stat().st_size > 0:
        return

    tmp = dest.with_suffix(dest.suffix + ".part")
    existing = tmp.stat().st_size if tmp.exists() else 0

    def _open(offset: int):
        headers = {"User-Agent": "Mozilla/5.0"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)

    resp = _open(existing)
    if existing and resp.status != 206:  # 服务器不支持断点续传 → 从头来
        resp.close()
        existing = 0
        resp = _open(0)

    length = resp.headers.get("Content-Length")
    total = int(length) + existing if length else None

    done = existing
    t0 = time.time()
    with resp, open(tmp, "ab" if existing else "wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if show_progress and total:
                speed = (done - existing) / max(1e-6, time.time() - t0)
                sys.stdout.write(
                    f"\r  {label}: {done / total * 100:5.1f}%  "
                    f"{_human(done)}/{_human(total)}  {_human(speed)}/s   "
                )
                sys.stdout.flush()
    if show_progress:
        sys.stdout.write("\n")
    tmp.replace(dest)


def resolve_files(entry: dict) -> list[str]:
    """决定要下载哪些文件：显式 files > 单个 gguf > whisper 默认文件集。"""
    if entry.get("files"):
        return list(entry["files"])
    if entry.get("gguf"):
        return [entry["gguf"]]
    return list(REQUIRED_FILES)


def fetch(
    hf_id: str,
    dest: Path,
    endpoint: str = DEFAULT_ENDPOINT,
    jobs: int = 4,
    files: list[str] | None = None,
) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    base = f"{endpoint}/{hf_id}/resolve/main"
    print(f"==> 从 {endpoint} 下载 {hf_id} → {dest}")

    todo = list(files or REQUIRED_FILES)
    errors: list[tuple[str, Exception]] = []
    lock = threading.Lock()

    # 大文件单独先行（GGUF/safetensors 走并行分段），其余小文件并行
    big = [f for f in todo if f.endswith((".safetensors", ".gguf"))]
    small = [f for f in todo if not f.endswith((".safetensors", ".gguf"))]

    def worker(files: list[str]) -> None:
        for name in files:
            target = dest / name
            if target.exists() and target.stat().st_size > 0:
                continue
            try:
                download_one(f"{base}/{name}", target, name)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append((name, exc))

    threads = [threading.Thread(target=worker, args=(big,), name="big")]
    n_small_workers = max(1, jobs - 1)
    for i in range(n_small_workers):
        chunk = small[i::n_small_workers]
        if chunk:
            threads.append(threading.Thread(target=worker, args=(chunk,), name=f"small{i}"))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if errors:
        for name, exc in errors:
            print(f"  ❌ {name} 下载失败：{exc}")
        raise SystemExit(1)

    # 清理半成品
    for part in dest.glob("*.part"):
        part.unlink(missing_ok=True)

    size = sum(f.stat().st_size for f in dest.iterdir() if f.is_file())
    print(f"==> 完成：{dest}（{_human(size)}）")
    return dest


def load_registry(config_path: Path) -> dict:
    import yaml

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def main() -> int:
    p = argparse.ArgumentParser(description="直连下载模型（走 hf-mirror，支持并行分段与断点续传）")
    p.add_argument("model", nargs="?", help="config.yaml 里的注册名，例如 anime-whisper / sakura-galtransl-14b")
    p.add_argument("--hf-id", default=None, help="直接指定 HuggingFace 模型 id")
    p.add_argument("--file", default=None, help="只下载指定文件（如某个 .gguf）")
    p.add_argument("--dest", default=None, help="下载目录（默认 .models/<注册名>）")
    p.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="HF 镜像地址")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"))
    args = p.parse_args()

    cfg = load_registry(Path(args.config))
    # 注册表分布在 model.registry（ASR）与 translation.registry（翻译）两处
    registry: dict = {}
    for section in ("model", "translation"):
        registry.update((cfg.get(section) or {}).get("registry") or {})

    files: list[str] | None = None
    if args.hf_id:
        hf_id = args.hf_id
        dest = Path(args.dest) if args.dest else PROJECT_ROOT / ".models" / hf_id.split("/")[-1]
    elif args.model and args.model in registry:
        entry = registry[args.model]
        hf_id = entry["hf_id"]
        dest = Path(args.dest or entry.get("local_dir") or (PROJECT_ROOT / ".models" / args.model))
        files = resolve_files(entry)
    else:
        p.error(f"请给出注册名（{'/'.join(registry) or '无'}）或用 --hf-id 指定模型")

    if args.file:
        files = [args.file]
    if not dest.is_absolute():
        dest = PROJECT_ROOT / dest

    fetch(hf_id, dest, endpoint=args.endpoint, files=files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
