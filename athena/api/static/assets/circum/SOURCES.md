# Circum Icons — selected assets

- Source pack: `M:\Athena\Circum_1.0-Free` (the supplied directory has no bundled license or readme).
- Official project: `https://github.com/Klarr-Agency/Circum-icons`
- License: Mozilla Public License 2.0 (see `LICENSE`). The pack is used locally; the selected SVG files are copied to `source/` and compiled into `icons.svg`.
- Modification: the original paths use `style="fill: #1e1e23"`. The sprite replaces that with `fill="currentColor"` so icons follow Athena's theme tokens. The original selected SVGs are kept under `source/` for MPL-2.0 provenance.

## Selected symbols

| Sprite symbol | Original file |
|---|---|
| `i-chat` | `Communication/Chat_1.svg` |
| `i-chat-new` | `Communication/Chat_2.svg` |
| `i-send` | `Communication/Paper_Plane.svg` |
| `i-research` | `Navigations & Travels/Compass_1.svg` |
| `i-run-details` | `User Interface/View_List.svg` |
| `i-how` | `Navigations & Travels/Route.svg` |
| `i-documents` | `Files & Folders/Folder_On.svg` |
| `i-settings` | `User Interface/Settings.svg` |
| `i-sidebar` | `User Interface/View_Column.svg` |
| `i-help` | `User Interface/Circle_Question.svg` |
| `i-theme-dark` | `User Interface/Dark.svg` |
| `i-theme-light` | `User Interface/Light.svg` |
| `i-appearance` | `Design/Palette.svg` |
| `i-menu` | `User Interface/Circle_More.svg` |
| `i-close` | `User Interface/Circle_Remove.svg` |
| `i-chevron-left` / `i-chevron-right` / `i-chevron-down` | `User Interface/Circle_Chev_*.svg` |
| `i-plus` | `User Interface/Circle_Plus.svg` |
| `i-edit` | `Design/Edit.svg` |
| `i-delete` | `User Interface/Trash_Can.svg` |
| `i-copy` | `User Interface/Export.svg` |
| `i-search` / `i-source-web` | `User Interface/Search.svg` |
| `i-model` / `i-defaults` | `User Interface/Slider_Horizontal.svg` |
| `i-tools` | `User Interface/Grid_2-V.svg` |
| `i-rag` / `i-source-doc` | `Files & Folders/File_On.svg` |
| `i-check` | `User Interface/Circle_Check.svg` |
| `i-alert` | `User Interface/Circle_Alert.svg` |
| `i-warning` | `User Interface/Warning.svg` |
| `i-info` / `i-about` | `User Interface/Circle_Info.svg` |
| `i-refresh` | `Media Controls/Repeat.svg` |
| `i-play` | `Media Controls/Play_1.svg` |
| `i-stop` | `Media Controls/Stop_1.svg` |
| `i-lock` | `User Interface/Lock.svg` |
| `i-add-model` | `User Interface/Circle_Plus.svg` |
| `i-models` / `i-tutorial-progress` | `User Interface/Circle_List.svg` |
| `i-library` | `User Interface/Bookmark.svg` |
| `i-mcp` | `User Interface/View_Board.svg` |
| `i-personalisation` | `User Interface/User.svg` |
| `i-tutorial-back` | `User Interface/Circle_Chev_Left.svg` |
| `i-tutorial-next` | `User Interface/Circle_Chev_Right.svg` |
| `i-tutorial-skip` | `User Interface/Circle_Remove.svg` |
| `i-tutorial-finish` | `User Interface/Circle_Check.svg` |

Regenerate with `python scripts/build_circum_icons.py`; `--check` fails when the committed sprite is stale.
