from functools import cache
from typing import Final
from tree_sitter import Language, Parser
from tspack import _binding
__all__: Final[tuple[str, ...]] = (
    "ALIASES",
    "get_language",
    "get_parser",
    "has_language",
    "language",
    "languages",
)
ALIASES: Final[dict[str, str]] = {
    "c++": "cpp",
    "csharp": "c_sharp",
    "cs": "c_sharp",
    "htm": "html",
    "js": "javascript",
    "jsx": "javascript",
    "jsonc": "json",
    "kt": "kotlin",
    "md": "markdown",
    "py": "python",
    "rb": "ruby",
    "rs": "rust",
    "sh": "bash",
    "shell": "bash",
    "tex": "latex",
    "ts": "typescript",
    "yml": "yaml",
}
def _normalize(name: str) -> str:
    key: str = name.strip().lower()
    return ALIASES.get(key, key)
def languages() -> tuple[str, ...]:
    return _binding.languages()
def has_language(name: str) -> bool:
    return bool(_binding.has_language(_normalize(name)))
def language(name: str) -> object:
    return _binding.language(_normalize(name))
@cache
def get_language(name: str) -> Language:
    return Language(_binding.language(_normalize(name)))
def get_parser(name: str) -> Parser:
    lang: Language = get_language(name)
    try:
        return Parser(lang)
    except TypeError:
        parser = Parser()
        parser.language = lang
        return parser
