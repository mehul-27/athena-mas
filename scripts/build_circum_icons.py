"""Build the local Circum icon sprite from the supplied pack.

The pack is not committed: this script reads `M:\\Athena\\Circum_1.0-Free\\SVG`
(or `CIRCUM_SVG_DIR`) and writes only the icons Athena uses. It also copies the
selected source SVGs and the official MPL-2.0 license for attribution.

Run with `--check` in tests to fail when the committed sprite is stale.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "athena" / "api" / "static" / "assets" / "circum"
SPRITE = ASSETS / "icons.svg"
SOURCE_OUT = ASSETS / "source"
SOURCE_ROOT = Path(os.getenv("CIRCUM_SVG_DIR") or r"M:\Athena\Circum_1.0-Free\SVG")

# Every entry is a file that exists in the supplied pack. The symbol id is
# `i-<key>`; the served sprite is the only runtime asset.
SYMBOLS: dict[str, str] = {
    "chat": "Communication/Chat_1.svg",
    "chat-new": "Communication/Chat_2.svg",
    "send": "Communication/Paper_Plane.svg",
    "research": "Navigations & Travels/Compass_1.svg",
    "run-details": "User Interface/View_List.svg",
    "how": "Navigations & Travels/Route.svg",
    "documents": "Files & Folders/Folder_On.svg",
    "settings": "User Interface/Settings.svg",
    "sidebar": "User Interface/View_Column.svg",
    "help": "User Interface/Circle_Question.svg",
    "theme-dark": "User Interface/Dark.svg",
    "theme-light": "User Interface/Light.svg",
    "appearance": "Design/Palette.svg",
    "menu": "User Interface/Circle_More.svg",
    "close": "User Interface/Circle_Remove.svg",
    "chevron-left": "User Interface/Circle_Chev_Left.svg",
    "chevron-right": "User Interface/Circle_Chev_Right.svg",
    "chevron-down": "User Interface/Circle_Chev_Down.svg",
    "plus": "User Interface/Circle_Plus.svg",
    "edit": "Design/Edit.svg",
    "delete": "User Interface/Trash_Can.svg",
    "copy": "User Interface/Export.svg",
    "search": "User Interface/Search.svg",
    "model": "User Interface/Slider_Horizontal.svg",
    "tools": "User Interface/Grid_2-V.svg",
    "rag": "Files & Folders/File_On.svg",
    "source-doc": "Files & Folders/File_On.svg",
    "source-web": "User Interface/Search.svg",
    "check": "User Interface/Circle_Check.svg",
    "alert": "User Interface/Circle_Alert.svg",
    "warning": "User Interface/Warning.svg",
    "info": "User Interface/Circle_Info.svg",
    "refresh": "Media Controls/Repeat.svg",
    "play": "Media Controls/Play_1.svg",
    "stop": "Media Controls/Stop_1.svg",
    "lock": "User Interface/Lock.svg",
    "add-model": "User Interface/Circle_Plus.svg",
    "models": "User Interface/Circle_List.svg",
    "defaults": "User Interface/Slider_Horizontal.svg",
    "library": "User Interface/Bookmark.svg",
    "mcp": "User Interface/View_Board.svg",
    "personalisation": "User Interface/User.svg",
    "about": "User Interface/Circle_Info.svg",
    "tutorial-back": "User Interface/Circle_Chev_Left.svg",
    "tutorial-next": "User Interface/Circle_Chev_Right.svg",
    "tutorial-skip": "User Interface/Circle_Remove.svg",
    "tutorial-finish": "User Interface/Circle_Check.svg",
    "tutorial-progress": "User Interface/Circle_List.svg",
}


def _inner(svg_text: str) -> str:
    start = svg_text.find(">")
    end = svg_text.rfind("</svg>")
    if start < 0 or end < 0:
        raise ValueError("not an SVG document")
    inner = svg_text[start + 1 : end]
    inner = inner.replace('style="fill: #1e1e23"', 'fill="currentColor"')
    inner = inner.replace("style=\"fill: #1e1e23\"", 'fill="currentColor"')
    inner = re.sub(r'\s(?:id|data-name)="[^"]*"', "", inner)
    inner = re.sub(r">\s+<", "><", inner).strip()
    return inner


def build() -> str:
    if not SOURCE_ROOT.exists():
        raise SystemExit(
            f"Circum source directory not found: {SOURCE_ROOT}. "
            "Set CIRCUM_SVG_DIR to the pack's SVG folder."
        )
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">'
    ]
    for key, relative in SYMBOLS.items():
        source = SOURCE_ROOT / relative
        if not source.exists():
            raise SystemExit(f"Missing Circum icon: {source}")
        inner = _inner(source.read_text(encoding="utf-8"))
        parts.append(f'<symbol id="i-{key}" viewBox="0 0 24 24">{inner}</symbol>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write(sprite: str) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    SPRITE.write_text(sprite, encoding="utf-8")
    SOURCE_OUT.mkdir(parents=True, exist_ok=True)
    for key, relative in SYMBOLS.items():
        source = SOURCE_ROOT / relative
        shutil.copyfile(source, SOURCE_OUT / f"{key}.svg")
    license_src = ROOT / "athena" / "api" / "static" / "assets" / "circum" / "LICENSE"
    if not license_src.exists():
        print("warning: MPL-2.0 LICENSE is missing from assets/circum", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the sprite is stale")
    args = parser.parse_args(argv)
    sprite = build()
    if args.check:
        current = SPRITE.read_text(encoding="utf-8") if SPRITE.exists() else ""
        if current != sprite:
            print("icons.svg is stale; run scripts/build_circum_icons.py", file=sys.stderr)
            return 1
        return 0
    write(sprite)
    print(f"wrote {SPRITE} ({len(SYMBOLS)} symbols)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
