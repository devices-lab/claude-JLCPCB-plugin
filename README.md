# claude-JLCPCB-plugin

A Claude Code plugin that makes Claude a much better **JLCPCB parts searcher**: JLC's whole
catalogue (~7 million parts) searched locally in milliseconds, **ranked by stock** so the
best-stocked, usually cheapest equivalent comes first, with live LCSC stock checks and datasheet
text. It also puts LCSC parts **into KiCad schematics** with footprints and 3D models that resolve
on any machine.

## Install

In Claude Code:

```
/plugin marketplace add james1236/claude-JLCPCB-plugin
/plugin install jlcpcb-parts@jlcpcb-parts
```

Or without the plugin system: copy `skills/jlcpcb-parts/` into `~/.claude/skills/`.

Then just ask, e.g. *"find a 5 V-tolerant single bus switch with the most stock at JLC"*. On first
use Claude downloads the parts database itself (~1 GB, a few minutes; ~2 GB kept in
`~/.cache/claude-jlcpcb-plugin/`) and tells you it's doing so.

**Requires** Python 3.8+ (with SQLite 3.34+, true of any recent Python). KiCad features also use
[easyeda2kicad](https://github.com/uPesy/easyeda2kicad.py) (Claude installs it if missing) and
`kicad-cli` if present, to test-load edited schematics. For datasheets, poppler-utils lets
Claude read PDF pages directly; without it the plugin falls back to extracted text.

## What it does

| Command | |
| :--- | :--- |
| `search TERM...` | In-stock parts matching every term, highest stock first. Filters for category, package, Basic parts, price sort, live stock. |
| `part C1234` | One part, with live LCSC stock and price. |
| `datasheet C1234 --find "absolute maximum"` | Fetches and caches the PDF (preferring the manufacturer's English edition) and lists the pages that match, for Claude to read directly; `--text` prints extracted text where PDFs can't be rendered. |
| `import C1234 --lib Lib.kicad_sym` | Symbol, footprint and 3D model via easyeda2kicad, registered in the project's library tables with portable `${KIPRJMOD}` paths, then checked: footprint resolves, model exists, pad count and pitch reported. |
| `place C1234 --sch x.kicad_sch --at X Y` | Adds the part to a schematic (or `--replace U7`, `--dnp`, `--show "LCSC Part"`). Refuses while KiCad has the file open; rolls back if KiCad can't load the result. |
| `remove U7 --sch x.kicad_sch` | Deletes parts. |
| `check --project x.kicad_pro [--fix]` | Every placed part's footprint and 3D model resolves; `--fix` repairs broken model paths. |

You can run it yourself too: `python3 skills/jlcpcb-parts/scripts/jlcpcb.py -h`.

## Where the data comes from

The daily parts database published by
[Bouni/kicad-jlcpcb-tools](https://github.com/Bouni/kicad-jlcpcb-tools), re-indexed for fast
substring search. Its stock figures are JLC's as of that build; `part` and `search -l` read live
stock from LCSC. `update` refreshes the database and refuses a partial one.
