# Sioyek

Sioyek is a PDF viewer with a focus on textbooks and research papers.

## Contents
* [Custom Enhancements & LaTeX Workflow](#custom-enhancements--latex-workflow)
* [Installation](#install)
* [Documentation](#documentation)
* [Video Demo](#feature-video-overview)
* [Features](#features)
* [Build Instructions](#build-instructions)
* [Buy Me a Coffee (or a Book!)](#donation)

## Custom Enhancements & LaTeX Workflow

This fork extends Sioyek with deep **LaTeX / SyncTeX integration**, **cross-recompilation PDF annotation preservation**, **Markdown + LaTeX study cards**, and **macOS native Apple Vision OCR**:

### 1. Automatic Annotation Preservation Across LaTeX Recompilations (`preserve_pdf_annotations.py`)
When recompiling a `.tex` document (via `tectonic`, `latexmk`, `xelatex`, etc.), the freshly generated `.pdf` normally loses all PDF-embedded annotations and shifts text coordinates. Sioyek now handles this automatically on `Document::open` and `Document::reload`:
* **Snapshot Storage & Zero-Overhead Fast Open**: Automatically maintains a snapshot of all PDF annotations in `<pdf_dir>/.sioyek_bak/<pdf_stem>.pdf_annots.json` with `(pdf_mtime_ns, pdf_mtime_sec, pdf_size)` caching. Opening an unchanged large PDF verifies metadata in C++ in `< 0.1ms` and skips spawning Python entirely.
* **Multi-Software Annotation Support**: Preserves Sioyek annotations (`add_markdown` cards/pills, `add_text` FreeText) as well as annotations created in external PDF viewers (**macOS Preview / PDFKit**, **Adobe Acrobat**, **PDF Expert**, **Skim**), including:
  * Text markups: `Highlight`, `Underline`, `StrikeOut`, `Squiggly` (with word-level `Quad` re-anchoring across horizontal shifts and line re-wraps)
  * Notes & text boxes: `FreeText` (preserving `/DA` font size, text color, and background fill color) and `Text` sticky notes (preserving icon types)
  * Drawings & shapes: `Ink`, `Square`, `Circle`, `Line`, `PolyLine`, `Polygon`, `Caret`, and third-party `Stamp` graphics (preserving border widths, dash patterns, interior fill colors, and arrow `line_ends`)
* **SyncTeX Smart Tracking + Collision Avoidance + Status Bar Feedback**:
  * Each annotation records a SyncTeX anchor (`tex_file`, `line`, `line_text`, `ref_page`, `ref_y`, `delta_y`). If `.tex` edits shift a paragraph vertically or push it onto another page, restored annotations automatically follow the paragraph's new `(page, y)` (keeping `x` unchanged).
  * **Margin Collision Avoidance**: When multiple originally non-overlapping margin annotations (`Stamp`, `FreeText`, `Text`) land on overlapping vertical regions after SyncTeX relocation, lower annotations automatically shift downward (`+6pt` gap) to prevent overlap.
  * **Status Bar Feedback**: Upon recompilation and restoration, Sioyek displays a concise summary in the bottom status bar (e.g., `已恢复 17 条批注（其中 3 条随段落跨页迁移）`).
* **Sioyek Database Highlight Re-Alignment**: Sioyek SQLite highlights (`h`) verify their underlying text on reload and automatically re-align `selection_begin` / `selection_end` across `orig_page ± 2` if LaTeX recompilation shifted the text.

### 2. LaTeX Source Study Notes (`as` / `us`) & One-Click PDF-to-TeX Promotion (`T`)
* **Study Notes (`_add_study_note`, key `as` / undo `us`)**: Select a region or text in a SyncTeX-enabled PDF to insert or edit `\studynote{...}` directly in the corresponding `.tex` source file (automatically injecting the preamble macro with automatic line-breaking support for long words, underscores `\_`, and slashes `\slash`).
* **One-Click Promotion from Temporary PDF Notes (`am` / `at`) to LaTeX `\studynote{...}`**: Option-click (`⌥ + Left Click`) any Markdown card/pill (`am`) or `FreeText`/`Text` (`at`) annotation and press `T` (or click `📤 转为 TeX 批注 (T)`) to convert and write the note directly into the corresponding `.tex` line as `\studynote{...}` via SyncTeX and remove the temporary PDF annotation in one step.
* **Source Highlight Embedding (`embed_tex_annotation.py`)**: Adding a highlight (`h` + `[a-z]`) in a SyncTeX-enabled PDF automatically wraps the corresponding `.tex` source span with `\sioyekhlbegin[type]...\sioyekhlend[type] \sioyekhlmark[type]{...}`. Deleting the highlight (`dh`) or pressing `uh` removes/undoes the `.tex` markup cleanly (backups stored in `.sioyek_bak/`).

### 3. Full-Document PDF Annotation Overview (`gm`) & Markdown Cards/Pills (`am` / `⌥ + Click`)
* **Create / Edit (`am`)**: Select a region and press `am` to write notes with full Markdown formatting, code blocks, and rendered LaTeX math (`$...$`, `$$...$$`). Can be displayed as a full card or collapsed into a compact pill badge.
* **Quick View, Edit & Promote (`⌥ + Left Click`)**: Option-click any Markdown card, pill, or `FreeText`/`Text` note to open the interactive reader/editor popup (`E` to edit, `T` to promote to `.tex` `\studynote{...}`).
* **Full-Document PDF Annotation Overview (`gm`)**: Press `gm` (`goto_markdown`) to fuzzy-search, jump to, or delete (`d` / `Delete`) across **all** PDF annotations in the document: `📌 [胶囊]`, `📋 [卡片]`, `📝 [文本]` (`FreeText` & Sticky Note), `🖍️ [高亮笔记]`, `🖍️ [外部高亮]`, and `🏷️ [印章]`.

### 4. FreeText PDF Annotations (`at` / `dt` / `ut`) & Apple Vision OCR (`:ocr`)
* **FreeText (`at` / `dt` / `ut`)**: Press `at` to embed a FreeText box directly into the PDF (with automatic CJK font support and mouse drag-to-move), `dt` to delete a selected annotation, or `ut` to undo the last added annotation on the current page.
* **Apple Vision OCR (`_ocr`)**: Run `:_ocr` on scanned PDFs to inject an invisible, accurately aligned text layer via macOS native Vision framework (automatically skipping pages that already have text).

### Summary of Custom Keybindings (`keys_user.config`)
| Key / Action | Command | Description |
| :--- | :--- | :--- |
| `am` | `_add_markdown` | Create or edit a Markdown + LaTeX card / pill |
| `gm` | `goto_markdown` | Full-document PDF annotation overview (search, jump, `d` to delete) |
| `⌥ + Left Click` | `_md_click_handler` | Inspect/edit (`E`) or promote (`T`) Markdown / FreeText note to `.tex` |
| `at` / `dt` / `ut` | `_add_text` / `_delete_text` / `_delete_last_text` | Add, delete, or undo PDF annotation |
| `as` / `us` | `_add_study_note` / `_undo_study_note` | Insert/edit or undo `\studynote{}` in `.tex` source |
| `h` / `dh` / `uh` | `add_highlight` / `delete_highlight` / `_undo_highlight` | Add, delete, or undo highlight (syncs with `.tex` via SyncTeX) |
| `⌘ + Left Click` | `smart_jump_under_cursor` | Smart jump to figure / equation / reference |
| `⌘ + ⇧ + Left Click` | `synctex_under_cursor` | Inverse search jump to `.tex` line in VS Code |
| `⇧ + Left Click` | `overview_under_cursor` | Open floating overview preview window |
| `T` / `gt` | `_translate` / `_translate_google` | Look up selection in Eudic (`T`) or Google Translate (`gt`) |
| `D` | `_dual_panelify` | Toggle side-by-side dual-panel reading mode |
| `dap` | `delete_all_portals` | Clear all portals in the current document |

## Install
### Official packages
There are installers for Windows, macOS and Linux. See [Releases page](https://github.com/ahrm/sioyek/releases).

### Homebrew Cask
There is a homebrew cask available here: https://formulae.brew.sh/cask/sioyek. Install by running:
```
brew install --cask sioyek
```
### Third-party packages for Linux
If you prefer to install sioyek with a package manager, you can look at this list. Please note that they are provided by third party packagers. USE AT YOUR OWN RISK! If you're reporting a bug for a third-party package, please mention which package you're using.

Distro | Link | Maintainer
------- | ----- | -------------
Flathub | [sioyek](https://flathub.org/apps/details/com.github.ahrm.sioyek) | [@nbenitez](https://flathub.org/apps/details/com.github.ahrm.sioyek)
Alpine | [sioyek](https://pkgs.alpinelinux.org/packages?name=sioyek) | [@jirutka](https://github.com/jirutka)
Arch | [AUR sioyek](https://aur.archlinux.org/packages/sioyek) | [@goggle](https://github.com/goggle)
Arch | [AUR sioyek-git](https://aur.archlinux.org/packages/sioyek-git/) | [@hrdl-github](https://github.com/hrdl-github)
Arch | [AUR sioyek-appimage](https://aur.archlinux.org/packages/sioyek-appimage/) | [@DhruvaSambrani](https://github.com/DhruvaSambrani)
Debian | [sioyek](https://packages.debian.org/sioyek) | [@viccie30](https://github.com/viccie30)
NixOS | [sioyek](https://search.nixos.org/packages?channel=unstable&show=sioyek&from=0&size=50&sort=relevance&type=packages&query=sioyek) | [@podocarp](https://github.com/podocarp)
openSUSE | [Publishing](https://build.opensuse.org/package/show/Publishing/sioyek) | [@uncomfyhalomacro](https://github.com/uncomfyhalomacro)
openSUSE | [Factory](https://build.opensuse.org/package/show/openSUSE:Factory/sioyek) | [@uncomfyhalomacro](https://github.com/uncomfyhalomacro)
Ubuntu | [sioyek](https://packages.ubuntu.org/sioyek) | [@viccie30](https://github.com/viccie30)
Guix | [sioyek](https://hpc.guix.info/package/sioyek) | [@guix-community](https://codeberg.org/org/guix/members)


## Documentation
You can view the official documentation [here](https://sioyek-documentation.readthedocs.io/en/latest/).
## Feature Video Overview

[![Sioyek feature overview](https://img.youtube.com/vi/yTmCI0Xp5vI/0.jpg)](https://www.youtube.com/watch?v=yTmCI0Xp5vI)

For a more in-depth tutorial, see this video:

[![Sioyek Tutorial](https://img.youtube.com/vi/RaHRvnb0dY8/0.jpg)](https://www.youtube.com/watch?v=RaHRvnb0dY8)

## Features

### Quick Open

https://user-images.githubusercontent.com/6392321/125321111-9b29dc00-e351-11eb-873e-94ea30016a05.mp4

You can quickly search and open any file you have previously interacted with using sioyek.

### Table of Contents

https://user-images.githubusercontent.com/6392321/125321313-cf050180-e351-11eb-9275-c2759c684af5.mp4

You can search and jump to table of contents entries.

### Smart Jump

https://user-images.githubusercontent.com/6392321/125321419-e5ab5880-e351-11eb-9688-95374a22774f.mp4

You can jump to any referenced figure or bibliography item *even if the PDF file doesn't provide links*. You can also search the names of bibliography items in google scholar/libgen by middle clicking/shift+middle clicking on their name.

### Overview

https://user-images.githubusercontent.com/6392321/154683015-0bae4f92-78e2-4141-8446-49dd7c2bd7c9.mp4

You can open a quick overview of figures/references/tables/etc. by right clicking on them (Like Smart Jump, this feature works even if the document doesn't provide links).

### Mark

https://user-images.githubusercontent.com/6392321/125321811-505c9400-e352-11eb-85e0-ffc3ae5f8cb8.mp4

Sometimes when reading a document you need to go back a few pages (perhaps to view a definition or something) and quickly jump back to where you were. You can achieve this by using marks. Marks are named locations within a PDF file (each mark has a single character name for example 'a' or 'm') which you can quickly jump to using their name. In the aforementioned example, before going back to the definition you mark your location and later jump back to the mark by invoking its name. Lower case marks are local to the document and upper case marks are global (this should be very familiar to you if you have used vim).

### Bookmarks

https://user-images.githubusercontent.com/6392321/125322503-1a6bdf80-e353-11eb-8018-5e8fc43b8d05.mp4

Bookmarks are similar to marks except they are named by a text string and they are all global.

### Highlights


https://user-images.githubusercontent.com/6392321/130956728-7e0a87fa-4ada-4108-a8fc-9d9d04180f56.mp4


Highlight text using different kinds of highlights. You can search among all the highlights.

### Portals (this feature is most useful for users with multiple monitors)



https://user-images.githubusercontent.com/6392321/125322657-41c2ac80-e353-11eb-985e-8f3ce9808f67.mp4

Suppose you are reading a paragraph which references a figure which is not very close to the current location. Jumping back and forth between the current paragraph and the figure can be very annoying. Using portals, you can link the paragraph's location to the figure's location. Sioyek shows the closest portal destination in a separate window (which is usually placed on a second monitor). This window is automatically updated to show the closest portal destination as the user navigates the document.


### Configuration


https://user-images.githubusercontent.com/6392321/125337160-e4832700-e363-11eb-8801-0bee58121c2d.mp4

You can customize all key bindings and some UI elements by editing `keys_user.config` and `prefs_user.config`. The default configurations are in `keys.config` and `prefs.config`.



## Build Instructions

### Linux

#### Fedora

Run the following commands to install dependencies, clone the repository and compile sioyek on Fedora (tested on Fedora Workstation 36).

```
sudo dnf install qt5-qtbase-devel qt5-qtbase-static qt5-qt3d-devel harfbuzz-devel mesa-libGL-devel glfw-devel
git clone --recursive https://github.com/ahrm/sioyek
cd sioyek
./build_linux.sh
``` 

#### Generic distribution
1. Install Qt 5 and make sure `qmake` is in `PATH`.

    Run `qmake --version` to make sure the `qmake` in path is using Qt 5.x.
2. Install `libharfbuzz`:
```
sudo apt install libharfbuzz-dev
```
3. Clone the repository and build:
```
git clone --recursive https://github.com/ahrm/sioyek
cd sioyek
./build_linux.sh
```

### Windows
1. Install Visual Studio (tested on 2019, other relatively recent versions should work too)
2. Install Qt 5 and make sure qmake is in `PATH`.
3. Clone the repository and build using 64 bit Visual Studio Developer Command Prompt:
```
git clone --recursive https://github.com/ahrm/sioyek
cd sioyek
build_windows.bat
```

### Mac

#### One-Click Build & Install (`install_mac.sh`)
On macOS (Apple Silicon / Intel), use `install_mac.sh` to build, bundle Qt frameworks, preserve existing user configs (`prefs_user.config` / `keys_user.config`), install to `/Applications/sioyek.app`, and launch:
```zsh
./install_mac.sh              # incremental build, install to /Applications, and launch
./install_mac.sh --no-install # build only (output in build/sioyek.app)
./install_mac.sh --clean      # full rebuild including mupdf
```

#### Manual Build (`build_mac.sh`)
1. Install Xcode.
2. Clone the repository and build: (The code below is in Zsh, which is the default shell on macOS.)
```zsh
(
setopt PIPE_FAIL PRINT_EXIT_VALUE ERR_RETURN SOURCE_TRACE XTRACE

git clone --recursive https://github.com/ahrm/sioyek
cd sioyek
chmod +x build_mac.sh

brew install 'qt@5' freeglut mesa harfbuzz

export PATH="/opt/homebrew/opt/qt@5/bin:$PATH"
#: The above is needed to make =qmake= from =qt= be found.
#: Find the path using =brew info 'qt@5'=.

MAKE_PARALLEL=8 ./build_mac.sh

mv build/sioyek.app /Applications/
sudo codesign --force --sign - --deep /Applications/sioyek.app
)
```

## Donation
If you enjoy sioyek, please consider donating to support its development.

<a href="https://www.buymeacoffee.com/ahrm" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="41" width="174"></a>
