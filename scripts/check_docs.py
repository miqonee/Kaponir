#!/usr/bin/env python3
"""Проверка локальных Markdown-ссылок, файлов и GitHub-якорей без сети."""
from __future__ import annotations

import argparse
from html import unescape
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$")
HTML_ID = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
LINK_START = re.compile(r"(?<!!)\[[^\]\n]*\]\(")


def slugify(text: str) -> str:
    """Воспроизводит нужный поднабор github-slugger, включая кириллицу."""
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text).strip().lower()
    kept = []
    for char in text:
        category = unicodedata.category(char)
        if char in "-_" or char.isspace() or category[0] in "LN" or category.startswith("M"):
            kept.append(char)
    return re.sub(r"\s", "-", "".join(kept))


def markdown_files(root: Path = ROOT) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*.md")
        if not any(part in {"target", ".git", ".venv", "node_modules", "artifacts"} for part in path.parts)
    )


def anchors(path: Path) -> set[str]:
    result: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        result.update(HTML_ID.findall(line))
        match = HEADING.match(line)
        if not match:
            continue
        title = re.sub(r"\s+#+\s*$", "", match.group(2))
        base = slugify(title)
        count = counts.get(base, 0)
        counts[base] = count + 1
        result.add(base if count == 0 else f"{base}-{count}")
    return result


def targets(line: str):
    """Извлекает inline links; ссылки в fenced code отфильтрованы вызывающим кодом."""
    for match in LINK_START.finditer(line):
        start = match.end()
        depth = 0
        escaped = False
        for index in range(start, len(line)):
            char = line[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    raw = line[start:index].strip()
                    if raw.startswith("<") and ">" in raw:
                        raw = raw[1 : raw.index(">")]
                    else:
                        # Необязательный Markdown title отделён пробелом.
                        raw = re.split(r"\s+[\"']", raw, maxsplit=1)[0]
                    yield raw
                    break
                depth -= 1


def check(root: Path = ROOT) -> list[str]:
    root = root.resolve()
    files = markdown_files(root)
    known_anchors = {path: anchors(path) for path in files}
    errors: list[str] = []
    for path in files:
        fenced = False
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if FENCE.match(line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for raw in targets(line):
                if not raw or raw.startswith("#") and len(raw) == 1:
                    continue
                parsed = urlsplit(raw)
                if parsed.scheme or parsed.netloc or raw.startswith(("mailto:", "data:")):
                    continue
                relative = unquote(parsed.path)
                destination = path if not relative else (path.parent / relative).resolve()
                try:
                    destination.relative_to(root)
                except ValueError:
                    errors.append(f"{path.relative_to(root)}:{number}: ссылка вне проекта: {raw}")
                    continue
                if not destination.exists():
                    errors.append(f"{path.relative_to(root)}:{number}: файл не найден: {raw}")
                    continue
                if destination.is_dir():
                    errors.append(f"{path.relative_to(root)}:{number}: ссылка ведёт на каталог: {raw}")
                    continue
                if parsed.fragment and destination.suffix.lower() == ".md":
                    fragment = unquote(parsed.fragment).lower()
                    if fragment not in known_anchors.get(destination, set()):
                        errors.append(f"{path.relative_to(root)}:{number}: якорь не найден: {raw}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="корень проверяемого дерева (по умолчанию — репозиторий)")
    args = parser.parse_args()
    root = args.root.resolve()
    files = markdown_files(root)
    errors = check(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"Документация: {len(errors)} ошибок в {len(files)} Markdown-файлах.", file=sys.stderr)
        return 1
    print(f"Документация: локальные ссылки и якоря проверены в {len(files)} Markdown-файлах.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
