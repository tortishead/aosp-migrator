"""Prompt-only fallback: Copilot answers with search/replace blocks, applied here in the scratch checkout.

A block names a file (relative to the checkout) on the line before it:

    path/to/File.cpp
    <<<<<<< SEARCH
    exact lines now in the file
    =======
    the lines that replace them
    >>>>>>> REPLACE

Blocks apply in order, several per file; each SEARCH must occur exactly once in the file
as the blocks before it left it. An empty SEARCH creates a file that does not exist. All
or nothing: a block that does not match fails the whole answer and no file is written.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_BLOCK = re.compile(
    r"^(?P<path>[^\n<>=]+?)[ \t]*\n(?:```[^\n]*\n)?<<<<<<< SEARCH\n(?P<search>.*?)^=======\n(?P<replace>.*?)^>>>>>>> REPLACE[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


class BlockError(Exception):
    """A block cannot be applied; nothing was written."""


@dataclass(frozen=True)
class Block:
    path: str
    search: str
    replace: str


def parse_blocks(text: str) -> list[Block]:
    return [Block(m.group("path").strip().strip("`"), m.group("search"), m.group("replace")) for m in _BLOCK.finditer(text)]


def apply_blocks(root: Path, blocks: list[Block]) -> list[str]:
    """Apply `blocks` under `root`; the paths changed, in first-change order. Raises `BlockError`."""
    if not blocks:
        raise BlockError("no search/replace blocks in the answer")
    contents: dict[str, str | None] = {}
    for n, block in enumerate(blocks, 1):
        path = _checked(root, block.path)
        if path not in contents:
            file = root / path
            contents[path] = file.read_text() if file.is_file() else None
        current = contents[path]
        where = f"block {n} ({path})"
        if current is None:
            if block.search:
                raise BlockError(f"{where}: file does not exist")
            contents[path] = block.replace
            continue
        if not block.search:
            raise BlockError(f"{where}: empty SEARCH for an existing file")
        count = current.count(block.search)
        if count != 1:
            raise BlockError(f"{where}: SEARCH {'not found' if count == 0 else f'found {count} times'}")
        contents[path] = current.replace(block.search, block.replace, 1)
    for path, text in contents.items():
        file = root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        assert text is not None
        file.write_text(text)
    return list(contents)


def _checked(root: Path, path: str) -> str:
    """The path normalized; refused when absolute, leaving `root` (also through a symlink) or inside `.git`."""
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] == ".git":
        raise BlockError(f"not a path in the checkout: {path}")
    resolved = (root / pure).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise BlockError(f"not a path in the checkout: {path}")
    return pure.as_posix()
