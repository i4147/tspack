#!/usr/bin/env python3
import argparse
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from typing import Final
ROOT: Final[Path] = Path(__file__).resolve().parent
MANIFEST_PATH: Final[Path] = ROOT / "languages.toml"
VENDOR_DIR: Final[Path] = ROOT / "vendor"
DEFAULT_JOBS: Final[int] = 4
@dataclass(frozen=True, slots=True)
class RepoJob:
    repo: str
    repo_dir: str
    ref: str | None
    languages: tuple[str, ...]
    src_dirs: tuple[str, ...]
def _load_manifest() -> dict[str, dict[str, object]]:
    with MANIFEST_PATH.open("rb") as fh:
        raw: dict[str, object] = tomllib.load(fh)
    langs = raw.get("languages", {})
    if not isinstance(langs, dict):
        raise RuntimeError(f"Invalid manifest: {MANIFEST_PATH}")
    return {str(k): v for k, v in langs.items() if isinstance(v, dict)}
def _select_languages(manifest: dict[str, dict[str, object]], raw_langs: str) -> list[str]:
    spec: str = raw_langs.strip() or os.environ.get("TSPACK_LANGS", "").strip()
    if not spec:
        return sorted(name for name, cfg in manifest.items() if bool(cfg.get("enabled", False)))
    if spec.lower() == "all":
        return sorted(manifest)
    requested: list[str] = [item.strip().lower() for item in spec.split(",") if item.strip()]
    unknown: list[str] = [name for name in requested if name not in manifest]
    if unknown:
        raise SystemExit(f"Unknown language(s): {', '.join(unknown)}\nAvailable: {', '.join(sorted(manifest))}")
    return list(dict.fromkeys(requested))
def _group_jobs(manifest: dict[str, dict[str, object]], selected: list[str]) -> list[RepoJob]:
    grouped: dict[str, tuple[str, str | None, list[str], list[str]]] = {}
    for lang in selected:
        cfg = manifest[lang]
        repo_dir = str(cfg.get("repo_dir", f"tree-sitter-{lang}"))
        repo = str(cfg["repo"])
        ref_val = cfg.get("ref")
        ref = str(ref_val) if isinstance(ref_val, str) and ref_val.strip() else None
        src_dir = str(cfg.get("src_dir", "src"))
        if repo_dir not in grouped:
            grouped[repo_dir] = (repo, ref, [lang], [src_dir])
        else:
            grouped[repo_dir][2].append(lang)
            grouped[repo_dir][3].append(src_dir)
    return [
        RepoJob(repo=repo, repo_dir=repo_dir, ref=ref, languages=tuple(langs), src_dirs=tuple(srcs))
        for repo_dir, (repo, ref, langs, srcs) in sorted(grouped.items())
    ]
def _has_parsers(dest: Path, src_dirs: tuple[str, ...]) -> bool:
    return all((dest / sd / "parser.c").is_file() for sd in src_dirs)
def _fetch_one(job: RepoJob, *, update: bool) -> tuple[str, str]:
    dest: Path = VENDOR_DIR / job.repo_dir
    if dest.exists():
        if not update and _has_parsers(dest, job.src_dirs):
            return job.repo_dir, "cached"
        if update and (dest / ".git").exists():
            proc = subprocess.run(
                ["git", "-C", str(dest), "pull", "--ff-only"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and _has_parsers(dest, job.src_dirs):
                return job.repo_dir, "updated"
        if not _has_parsers(dest, job.src_dirs):
            shutil.rmtree(dest)
    cmd: list[str] = ["git", "clone", "--depth", "1", "--single-branch"]
    if job.ref is not None:
        cmd.extend(["--branch", job.ref])
    cmd.extend([job.repo, str(dest)])
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed for {job.repo_dir}: {proc.stderr.strip() or proc.stdout.strip()}")
    if not _has_parsers(dest, job.src_dirs):
        missing = [sd for sd in job.src_dirs if not (dest / sd / "parser.c").is_file()]
        raise RuntimeError(
            f"{job.repo_dir} cloned, but missing parser.c in {', '.join(missing)} "
            f"(repo may require a different ref/branch in languages.toml)"
        )
    return job.repo_dir, "cloned"
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fetch_grammars",
        description="Shallow-clone selected tree-sitter grammars into vendor/ for tspack.",
    )
    parser.add_argument(
        "--langs",
        default="",
        help="comma-separated language list or 'all' (default: enabled entries in languages.toml or TSPACK_LANGS)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"parallel git clone workers (default: {DEFAULT_JOBS})",
    )
    parser.add_argument("--update", action="store_true", help="git pull existing repos in vendor/")
    parser.add_argument("--list", action="store_true", help="list available languages in languages.toml and exit")
    args: argparse.Namespace = parser.parse_args(argv)
    manifest = _load_manifest()
    if args.list:
        for name in sorted(manifest):
            mark = "*" if manifest[name].get("enabled", False) else " "
            print(f"[{mark}] {name:<14} {manifest[name].get('repo_dir', '')}")
        return 0
    if shutil.which("git") is None:
        print("error: 'git' is required to fetch grammars.", file=sys.stderr)
        return 2
    selected: list[str] = _select_languages(manifest, str(args.langs))
    jobs: list[RepoJob] = _group_jobs(manifest, selected)
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Fetching {len(jobs)} repo(s) for {len(selected)} language(s): {', '.join(selected)}")
    failures: list[str] = []
    workers: int = max(1, min(int(args.jobs), len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, job, update=bool(args.update)): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                repo_dir, status = future.result()
                print(f"  [{status:7}] {repo_dir} ({', '.join(job.languages)})")
            except Exception as exc:
                failures.append(f"{job.repo_dir}: {exc}")
                print(f"  [FAILED ] {job.repo_dir}: {exc}", file=sys.stderr)
    if failures:
        print(f"\n{len(failures)} repo(s) failed.", file=sys.stderr)
        return 1
    print("\nDone. Build tspack with: pip install --no-build-isolation .")
    return 0
if __name__ == "__main__":
    raise SystemExit(main())
