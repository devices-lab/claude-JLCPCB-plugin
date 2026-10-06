---
name: jlcpcb-parts
description: Find, check or substitute electronic components for a JLCPCB/LCSC build, and put them into a KiCad project - any time a part number, stock level, price, Basic/Extended status, alternative, datasheet figure, or a KiCad symbol/footprint/3D model for an LCSC part is needed. Searches JLC's whole catalogue locally, best-stocked first, with live LCSC stock.
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/jlcpcb.py search *), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/jlcpcb.py part *), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/jlcpcb.py cats *), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/jlcpcb.py info), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/jlcpcb.py datasheet *)
---

# JLCPCB parts and KiCad

The tool is `python3 ${CLAUDE_SKILL_DIR}/scripts/jlcpcb.py` (below: `jlcpcb`). Its `-h` is the full
syntax reference. Never quote a stock figure from a web search - use the tool.

## First use: set it up yourself

Run `jlcpcb info`. Exit status 2 means there is no parts database yet. Then, before searching:

1. Tell the user: first-time setup downloads JLC's parts database (~1 GB, a few minutes), needs
   ~10 GB free while building, and keeps ~2 GB in `~/.cache/claude-jlcpcb-plugin/`.
2. Run `jlcpcb update` (long-running: use a generous timeout or run it in the background), then
   carry on with the original request.

`info` also shows the database's age; if it is more than a couple of weeks old, mention it and
offer `update`. Live stock (`part`, `search -l`) is always current regardless.

`import` and `place --import-to` need easyeda2kicad. If it is missing, tell the user and install it
(`uv tool install easyeda2kicad`, else `pipx install easyeda2kicad`, else
`python3 -m pip install --user easyeda2kicad`). `datasheet` sets up its own PDF text index on first use if poppler's `pdftotext` is missing.

## Choosing a part

The user's rule: **sort candidates by highest stock first**, so a better-stocked (and usually
cheaper) equivalent isn't missed.

1. List the realistic alternatives for the function with `jlcpcb search` (sorted by stock), not
   just the first part that fits; say which ones you checked.
2. Prefer the best-stocked part that meets the spec; flag thin stock (hundreds) against the
   quantity needed.
3. Check the figures that matter in the datasheet, not the one-line description:
   `jlcpcb datasheet C1234 --find "absolute maximum"` fetches the PDF and lists the pages that
   match - then **Read the PDF itself at those pages** (`pages: "4"`), which keeps tables and
   drawings intact. If PDFs can't be rendered here (Read reports poppler/pdftoppm missing), use
   `--text` to print the extracted text instead, and mention to the user that installing
   poppler-utils lets Claude read datasheet pages directly.
4. Confirm with `jlcpcb part C1234` (live LCSC stock and price) before recommending it.
5. **Always give the user a JLCPCB search link** so they can sanity-check the chosen part against
   its neighbours on jlcpcb.com. `search` prints one built from the query
   (`https://jlcpcb.com/parts/componentSearch?isSearch=true&searchTxt=...`). JLC's search is
   keyword-based, so when the query was attribute fragments (`1:1 2.3V~3.6V`) that link is a poor
   one - give a representative search instead, usually the part family or MPN prefix
   (`SN74CB3T`), or the key value plus package (`10kΩ 0603`), URL-encoded into the same address.
   The page can't be pre-sorted or pre-filtered from the URL (it ignores sort parameters, and its
   `Package=` parameter didn't filter when tried), so tell the user to click the **Stock** column
   to sort high to low.
6. Parts the user hand-solders (headers, connectors) needn't be JLC parts: place them `--dnp`.

### Getting searches right

- **Descriptions are attribute strings, not prose.** Search a category plus attribute fragments
  (`-c Switch 1:1 2.3V~3.6V`) or an MPN prefix (`SN74CB3T`); English phrases mostly miss.
  `jlcpcb cats FILTER` lists category names.
- **JLC's spellings:** `10kΩ`, `100nF`, `4.7uF`, ranges with `~` (`2.3V~3.6V`, `-40℃~+85℃`).
  `ohm`, `Ω`, `µ`/`μ`, `°C` in a query are normalised for you.
- **Values need whole tokens:** `10k` also hits `110kΩ`, `5V` hits `6.5V`; use `+10kΩ`, `+5V`.
- **Package spellings vary:** `-p SOT-23` also matches SOT-23-5/-6; for the 3-pin part use
  `-P "SOT-23|SOT-23-3|SOT-23-3L|SOT-23(TO-236)"`.
- **Out-of-stock parts** only appear with `-a` (needed to find a known MPN's C-number at all).
- **`-b` is Basic only**; the data has no "Preferred" flag.
- **Check the manufacturer column.** The same MPN is often listed again for a second-source copy
  (HXY, TECH PUBLIC, UMW, JSM...) at a fraction of the price and with different specs.

## Putting a part into KiCad

```sh
jlcpcb import C1234 --lib path/in/project/MyLib.kicad_sym     # symbol + footprint + 3D model
jlcpcb place C1234 --sch board.kicad_sch --at X Y --import-to MyLib --show "LCSC Part"
jlcpcb place C1234 --sch board.kicad_sch --replace U7          # swap a part in place
jlcpcb place Connector_Generic:Conn_02x20_Odd_Even --sch board.kicad_sch --at X Y --dnp
jlcpcb remove U7 U8 --sch board.kicad_sch
jlcpcb check --project board.kicad_pro [--fix]                 # footprints + 3D models resolve?
```

- Keep imported libraries **inside the project folder**: `import` then registers them in the
  project's own sym-lib-table/fp-lib-table and writes `${KIPRJMOD}`-relative model paths, so the
  project works on any machine whatever its library setup.
- After `import`, read its report: the footprint's pad count and pitch must match what the part
  mates with (a "2x20 female header" was once 2.00 mm instead of 2.54 mm).
- `place`/`remove` refuse while KiCad has the schematic open - ask the user to close the schematic
  editor; saving there would overwrite the edit. They roll back if KiCad can't load the result.
- Positions are schematic millimetres (snapped to 1.27 mm). `place` adds parts only; wiring is
  still done in KiCad.
- Run `check` after edits; `--fix` repairs model paths it can find on disk.
