from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tomllib
from typing import Final
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
ROOT: Final[Path] = Path(__file__).resolve().parent
MANIFEST_PATH: Final[Path] = ROOT / "languages.toml"
VENDOR_DIR: Final[Path] = ROOT / "vendor"
DEFAULT_JOBS: Final[int] = int(os.environ.get("TSPACK_JOBS", "4"))
OPT_LEVEL: Final[str] = os.environ.get("TSPACK_OPT", "2")
@dataclass(frozen=True, slots=True)
class GrammarSpec:
    name: str
    repo_dir: str
    src_dir: str
    symbol: str
    enabled: bool
    extra_sources: tuple[str, ...] = ()
@dataclass(frozen=True, slots=True)
class ResolvedGrammar:
    name: str
    symbol: str
    src_path: Path
    include_dirs: tuple[Path, ...]
    c_sources: tuple[Path, ...]
    cpp_sources: tuple[Path, ...]
def _is_termux_or_android() -> bool:
    return (
        sys.platform == "android"
        or "TERMUX_VERSION" in os.environ
        or "ANDROID_ROOT" in os.environ
    )
def _load_manifest() -> dict[str, GrammarSpec]:
    with MANIFEST_PATH.open("rb") as fh:
        raw: dict[str, object] = tomllib.load(fh)
    langs_raw = raw.get("languages", {})
    if not isinstance(langs_raw, dict):
        raise RuntimeError(f"Invalid manifest: {MANIFEST_PATH}")
    specs: dict[str, GrammarSpec] = {}
    for name, entry in langs_raw.items():
        if not isinstance(entry, dict):
            continue
        extras = entry.get("extra_sources", [])
        specs[str(name)] = GrammarSpec(
            name=str(name),
            repo_dir=str(entry.get("repo_dir", f"tree-sitter-{name}")),
            src_dir=str(entry.get("src_dir", "src")),
            symbol=str(entry.get("symbol", f"tree_sitter_{name}")),
            enabled=bool(entry.get("enabled", False)),
            extra_sources=tuple(str(item) for item in extras) if isinstance(extras, list) else (),
        )
    return specs
def _selected_specs(manifest: dict[str, GrammarSpec]) -> list[GrammarSpec]:
    env_langs: str = os.environ.get("TSPACK_LANGS", "").strip()
    if not env_langs:
        return [spec for spec in manifest.values() if spec.enabled]
    if env_langs.lower() == "all":
        return list(manifest.values())
    requested: list[str] = [item.strip().lower() for item in env_langs.split(",") if item.strip()]
    unknown: list[str] = [name for name in requested if name not in manifest]
    if unknown:
        available = ", ".join(sorted(manifest))
        raise RuntimeError(f"Unknown TSPACK_LANGS entries: {', '.join(unknown)}. Available: {available}")
    return [manifest[name] for name in dict.fromkeys(requested)]
def _find_grammar_src(spec: GrammarSpec) -> Path | None:
    candidates: tuple[Path, ...] = (
        VENDOR_DIR / spec.repo_dir / spec.src_dir,
        VENDOR_DIR / spec.name / spec.src_dir,
        VENDOR_DIR / spec.name / "src",
        VENDOR_DIR / spec.repo_dir / "src",
        VENDOR_DIR / f"tree-sitter-{spec.name}" / spec.src_dir,
        VENDOR_DIR / f"tree-sitter-{spec.name}" / "src",
    )
    for candidate in candidates:
        if (candidate / "parser.c").is_file():
            return candidate
    return None
def _resolve_grammars(specs: list[GrammarSpec]) -> tuple[list[ResolvedGrammar], list[str]]:
    resolved: list[ResolvedGrammar] = []
    missing: list[str] = []
    for spec in specs:
        src_path: Path | None = _find_grammar_src(spec)
        if src_path is None:
            missing.append(spec.name)
            continue
        c_sources: list[Path] = [src_path / "parser.c"]
        cpp_sources: list[Path] = []
        for scanner_name in ("scanner.c", "scanner.cc", "scanner.cpp"):
            scanner_path: Path = src_path / scanner_name
            if scanner_path.is_file():
                if scanner_name.endswith(".c"):
                    c_sources.append(scanner_path)
                else:
                    cpp_sources.append(scanner_path)
                break
        for extra in spec.extra_sources:
            extra_path: Path = src_path / extra
            if extra_path.is_file():
                if extra_path.suffix == ".c":
                    c_sources.append(extra_path)
                else:
                    cpp_sources.append(extra_path)
        inc_dirs: list[Path] = [src_path, src_path.parent]
        if src_path.parent != VENDOR_DIR and src_path.parent.parent != VENDOR_DIR:
            inc_dirs.append(src_path.parent.parent)
        resolved.append(
            ResolvedGrammar(
                name=spec.name,
                symbol=spec.symbol,
                src_path=src_path,
                include_dirs=tuple(dict.fromkeys(inc_dirs)),
                c_sources=tuple(c_sources),
                cpp_sources=tuple(cpp_sources),
            )
        )
    resolved.sort(key=lambda item: item.name)
    return resolved, missing
def _pick_tool(env_key: str, sysconfig_key: str | None, fallbacks: tuple[str, ...]) -> list[str]:
    override: str = os.environ.get(env_key, "").strip()
    if override:
        return shlex.split(override)
    if sysconfig_key:
        cfg: str | None = sysconfig.get_config_var(sysconfig_key)
        if cfg:
            parts: list[str] = shlex.split(cfg)
            if parts and shutil.which(parts[0]):
                return [parts[0]]
    for name in fallbacks:
        if shutil.which(name):
            return [name]
    raise RuntimeError(f"Could not find required build tool ({env_key} / {', '.join(fallbacks)})")
def _find_optional_tool(candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        found: str | None = shutil.which(name)
        if found is not None:
            return found
    return None
def _needs_rebuild(target: Path, deps: list[Path]) -> bool:
    if not target.is_file():
        return True
    target_mtime: float = target.stat().st_mtime
    return any(dep.is_file() and dep.stat().st_mtime > target_mtime for dep in deps)
def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr or proc.stdout}")
def _compile_grammar(
    grammar: ResolvedGrammar,
    build_root: Path,
    cc: list[str],
    cxx: list[str] | None,
    ld_tool: str | None,
    objcopy_tool: str | None,
) -> Path:
    out_dir: Path = build_root / "grammars" / grammar.name
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_obj: Path = out_dir / f"{grammar.name}.o"
    all_sources: list[Path] = [*grammar.c_sources, *grammar.cpp_sources]
    if not _needs_rebuild(merged_obj, [*all_sources, MANIFEST_PATH]):
        return merged_obj
    common_flags: list[str] = [
        "-fPIC",
        f"-O{OPT_LEVEL}",
        "-fvisibility=hidden",
        "-ffunction-sections",
        "-fdata-sections",
        "-Wno-unused-function",
        "-Wno-unused-variable",
        "-Wno-unused-parameter",
        "-Wno-sign-compare",
    ]
    inc_flags: list[str] = [f"-I{inc}" for inc in grammar.include_dirs]
    unit_objs: list[Path] = []
    for idx, src in enumerate(grammar.c_sources):
        obj: Path = out_dir / f"c_{idx}_{src.stem}.o"
        cmd: list[str] = [*cc, "-std=c11", *common_flags, *inc_flags, "-c", str(src), "-o", str(obj)]
        _run(cmd)
        unit_objs.append(obj)
    if grammar.cpp_sources:
        if cxx is None:
            raise RuntimeError(f"Grammar '{grammar.name}' has a C++ scanner but no C++ compiler was found.")
        for idx, src in enumerate(grammar.cpp_sources):
            obj = out_dir / f"cpp_{idx}_{src.stem}.o"
            cmd = [*cxx, "-std=c++14", *common_flags, *inc_flags, "-c", str(src), "-o", str(obj)]
            _run(cmd)
            unit_objs.append(obj)
    if len(unit_objs) == 1:
        shutil.copy2(unit_objs[0], merged_obj)
    else:
        reloc_cmd: list[str] = [*cc, "-r", "-nostdlib", "-o", str(merged_obj), *(str(obj) for obj in unit_objs)]
        reloc = subprocess.run(reloc_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if reloc.returncode != 0:
            if ld_tool is not None:
                _run([ld_tool, "-r", "-o", str(merged_obj), *(str(obj) for obj in unit_objs)])
            else:
                raise RuntimeError(f"Relocatable link failed for '{grammar.name}':\n{reloc.stderr or reloc.stdout}")
    if objcopy_tool is not None and sys.platform != "darwin":
        if not grammar.cpp_sources:
            _run([objcopy_tool, f"--keep-global-symbol={grammar.symbol}", str(merged_obj)])
        else:
            res = subprocess.run(
                [objcopy_tool, f"--keep-global-symbol={grammar.symbol}", str(merged_obj)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                print(
                    f"[tspack] note: objcopy skipped for C++ grammar '{grammar.name}' "
                    f"(relying on -fvisibility=hidden): {res.stderr.strip()}"
                )
    return merged_obj
def _write_registry_header(grammars: list[ResolvedGrammar], out_path: Path) -> None:
    lines: list[str] = [
        "/* Auto-generated by tspack setup.py — do not edit manually. */",
        "#pragma once",
        "#include <stddef.h>",
        "typedef struct TSLanguage TSLanguage;",
    ]
    for grammar in grammars:
        lines.append(f"extern const TSLanguage *{grammar.symbol}(void);")
    lines.extend([
        "typedef struct {",
        "    const char *name;",
        "    const TSLanguage *(*fn)(void);",
        "} TsPackEntry;",
        "static const TsPackEntry TSPACK_REGISTRY[] = {",
    ])
    for grammar in grammars:
        lines.append(f'    {{"{grammar.name}", {grammar.symbol}}},')
    lines.extend([
        "};",
        "static const size_t TSPACK_COUNT = sizeof(TSPACK_REGISTRY) / sizeof(TSPACK_REGISTRY[0]);",
        "",
    ])
    new_content: str = "\n".join(lines)
    if not out_path.is_file() or out_path.read_text(encoding="utf-8") != new_content:
        out_path.write_text(new_content, encoding="utf-8")
class BuildTsPack(build_ext):
    def build_extension(self, ext: Extension) -> None:
        if ext.name != "tspack._binding":
            super().build_extension(ext)
            return
        manifest: dict[str, GrammarSpec] = _load_manifest()
        specs: list[GrammarSpec] = _selected_specs(manifest)
        grammars, missing = _resolve_grammars(specs)
        strict: bool = os.environ.get("TSPACK_STRICT", "").strip() in {"1", "true", "yes"}
        if missing:
            msg = f"[tspack] missing grammars in vendor/ ({len(missing)}): {', '.join(missing)}"
            if strict:
                raise RuntimeError(f"{msg}. Run `python3 fetch_grammars.py` first.")
            print(f"{msg} — skipping (run `python3 fetch_grammars.py` to fetch them)")
        if not grammars:
            raise RuntimeError(
                "No grammars found in vendor/. Run `python3 fetch_grammars.py` (or place grammars in vendor/) first."
            )
        print(f"[tspack] building single .so with {len(grammars)} languages: {', '.join(g.name for g in grammars)}")
        build_root: Path = Path(self.build_temp).resolve()
        build_root.mkdir(parents=True, exist_ok=True)
        cc: list[str] = _pick_tool("CC", "CC", ("clang", "gcc", "cc"))
        any_cpp: bool = any(bool(g.cpp_sources) for g in grammars)
        cxx: list[str] | None = (
            _pick_tool("CXX", "CXX", ("clang++", "g++", "c++")) if any_cpp else None
        )
        ld_tool: str | None = _find_optional_tool(("ld", "ld.lld"))
        objcopy_tool: str | None = _find_optional_tool(("objcopy", "llvm-objcopy"))
        jobs: int = max(1, min(DEFAULT_JOBS, len(grammars)))
        grammar_objs: list[Path] = []
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(_compile_grammar, grammar, build_root, cc, cxx, ld_tool, objcopy_tool): grammar.name
                for grammar in grammars
            }
            for future in as_completed(futures):
                name = futures[future]
                obj_path: Path = future.result()
                grammar_objs.append(obj_path)
                print(f"[tspack] compiled {name}")
        grammar_objs.sort()
        registry_header: Path = build_root / "_registry.inc"
        _write_registry_header(grammars, registry_header)
        binding_src: Path = ROOT / "src" / "tspack" / "_binding.c"
        binding_obj: Path = build_root / "_binding.o"
        py_include: str = sysconfig.get_path("include")
        binding_cmd: list[str] = [
            *cc,
            "-std=c11",
            "-fPIC",
            f"-O{OPT_LEVEL}",
            "-fvisibility=hidden",
            f"-I{build_root}",
            f"-I{py_include}",
            "-c",
            str(binding_src),
            "-o",
            str(binding_obj),
        ]
        _run(binding_cmd)
        ext_fullpath: Path = Path(self.get_ext_fullpath(ext.name)).resolve()
        ext_fullpath.parent.mkdir(parents=True, exist_ok=True)
        libraries: list[str] = []
        if any_cpp:
            if _is_termux_or_android():
                libraries.append("c++_shared")
            elif sys.platform == "darwin":
                libraries.append("c++")
            else:
                libraries.append("stdc++")
        extra_postargs: list[str] = []
        if sys.platform != "darwin":
            extra_postargs.append("-Wl,--gc-sections")
        all_objs: list[str] = [str(binding_obj), *(str(obj) for obj in grammar_objs)]
        self.compiler.link_shared_object(
            all_objs,
            str(ext_fullpath),
            libraries=libraries,
            library_dirs=self.compiler.library_dirs[:],
            runtime_library_dirs=self.compiler.runtime_library_dirs[:],
            extra_postargs=extra_postargs,
            target_lang="c++" if any_cpp else "c",
        )
        size_kb: float = ext_fullpath.stat().st_size / 1024.0
        print(f"[tspack] linked {ext_fullpath.name} ({size_kb:.1f} KiB, {len(grammars)} languages)")
setup(
    ext_modules=[Extension("tspack._binding", sources=["src/tspack/_binding.c"])],
    cmdclass={"build_ext": BuildTsPack},
)
