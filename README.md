# tspack

Selective, **single-`.so`** tree-sitter language pack in pure C/C++ (zero Rust, zero `tree-sitter-cli`, Termux-friendly).

## Why single-`.so` builds usually fail — and how `tspack` solves it

1. **Header collisions (`scanner.h`, `tag.h`, `schema.h`)**: Each grammar is compiled in isolation with only its own `src/` and parent directory in `-I`.
2. **Duplicate helper symbols across grammars**: Each grammar's `parser.o` + `scanner.o` are relocatably linked (`cc -r -nostdlib`) and localized via `objcopy --keep-global-symbol=tree_sitter_<lang>` (or `llvm-objcopy`), making symbol collisions between grammars impossible.
3. **Mixed C / C++ scanners & Termux libc++**: `.c` files compile with `-std=c11`, `.cc`/`.cpp` with `-std=c++14`, and `-lc++_shared` (Termux/Android) or `-lstdc++` (Linux) is linked only when a selected grammar actually has a C++ scanner.
4. **Included helper `.c` files** (e.g., `yaml`'s `schema.core.c`): Compiles only `parser.c` + `scanner.{c,cc,cpp}` rather than globbing `src/*.c`.
5. **Single `.so` + PyCapsule ABI**: Everything links into one `tspack/_binding*.so` returning `PyCapsule("tree_sitter.Language")` compatible with `tree-sitter >= 0.22`.

## Quick Start (Termux & Linux)

### Termux prerequisites
```bash
pkg install clang llvm git python
pip install setuptools wheel tree-sitter
```

### 1. Fetch selective grammars (~22 core languages by default)
```bash
cd tspack
python3 fetch_grammars.py --list          # view default (*) and optional languages
python3 fetch_grammars.py                 # fetch the ~22 default languages into vendor/
```
Or fetch only a custom subset:
```bash
TSPACK_LANGS=python,javascript,typescript,tsx,bash,c,cpp,rust,go,html,css,lua,toml,json,vim,yaml,xml,php,ruby,java python3 fetch_grammars.py
```
If you already have grammar repos cloned in `vendor/`, `setup.py` automatically detects both `vendor/tree-sitter-<lang>/...` and `vendor/<lang>/...` and skips any missing ones unless `TSPACK_STRICT=1`.

### 2. Build & install the single `.so`
```bash
pip install --no-build-isolation .
# Smaller .so for Termux (-Os) with 4 compile workers:
TSPACK_OPT=s TSPACK_JOBS=4 pip install --no-build-isolation .
```

## Usage

```python
import tspack

print(tspack.languages())               # ('bash', 'c', 'cpp', ..., 'zsh')
print(tspack.has_language("python"))    # True

lang = tspack.get_language("python")    # cached tree_sitter.Language
parser = tspack.get_parser("ts")        # supports aliases ('js', 'ts', 'py', 'sh', 'rs', ...)
tree = parser.parse(b"const x: number = 1;")
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TSPACK_LANGS` | enabled in `languages.toml` | Comma-separated subset (or `all`) |
| `TSPACK_JOBS` | `4` | Parallel compiler jobs |
| `TSPACK_OPT` | `2` | Compiler optimization (`2`, `3`, `s` for smaller `.so`) |
| `TSPACK_STRICT` | `0` | Set to `1` to fail if any selected language is missing from `vendor/` |
