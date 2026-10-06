#!/usr/bin/env python3
"""claude-JLCPCB-plugin - a better local stock searcher for JLCPCB parts, plus KiCad integration.

Parts database (local copy of JLC's whole catalogue, ~7M parts):
  search TERM... [opts]     in-stock parts matching every TERM, highest stock first
  part C1234 ...            exact lookup (any stock level) with LIVE LCSC stock and price
  cats [FILTER]             category names with in-stock counts, for search -c
  info                      database location, size and age (exit status 2 if there is none)
  update                    download the latest database and build the index
                            (~1 GB download, ~10 GB free space while building, ~2 GB kept)

  datasheet C1234 ... [--find REGEX] [--text [--pages A-B]]
                            fetch the datasheet PDF (cached; the manufacturer's English edition
                            preferred over LCSC's often-Chinese copy) and list the pages matching
                            REGEX, e.g. --find "absolute maximum" - then READ THOSE PDF PAGES
                            (tables and drawings survive). --text prints the extracted text
                            instead, for when PDFs can't be rendered. The text index uses
                            pdftotext if installed, else a private venv with pypdf.

KiCad:
  import C1234 ... --lib PATH.kicad_sym
        fetch symbol, footprint and 3D model with easyeda2kicad into that library (footprints
        in PATH.pretty, models in PATH.3dshapes); register both libraries in the project's own
        sym-lib-table/fp-lib-table if missing (${KIPRJMOD}-relative, renamed on a clash); make
        the model paths ${KIPRJMOD}-relative; then verify footprint and model resolve and report
        the pad count and pitch - check those against what the part must mate with.
  place C1234|LIB:SYMBOL --sch FILE.kicad_sch --at X Y [--ref R] [--replace REF] [--dnp] ...
        add a part to a schematic. An LCSC number is looked up by its "LCSC Part" field in the
        project's symbol libraries (sym-lib-table); --import-to LIBNICK fetches it first if it
        isn't there. --replace REF takes REF's position and reference and removes the old part.
        --show FIELD displays a field (e.g. "LCSC Part") under the value; --dnp marks it
        do-not-populate (hand-fitted); --value/--footprint/--field NAME=VALUE override fields.
  remove REF... --sch FILE.kicad_sch
        delete parts from a schematic (and unused cached library symbols).
  check [--sch FILE | --project DIR] [--fix]
        every placed part's footprint must resolve through the fp-lib-tables and its 3D model
        must exist via a portable path; --fix repoints model paths at files it finds nearby.
  place/remove refuse while KiCad has the schematic open (KiCad would overwrite the edit on
  save), keep a .bak, and roll back if kicad-cli can no longer load the file.

Search TERMS are case-insensitive substrings of the MFR part number, package, category,
manufacturer or description; all must match.
  a|b        alternatives                      "SOT-23|SOT23"
  +TERM      whole token only                  +10kΩ matches "10kΩ", not "110kΩ"
             ('+', not '=': zsh expands a leading '=')
  ohm/Ω/µ/μ/°C are rewritten to JLC's spelling (Greek Ω, u, ℃). JLC writes "10kΩ", "100nF",
  "4.7uF", ranges with '~' ("2.3V~3.6V", "-40℃~+85℃").
Descriptions are terse attribute strings, not prose: search a category plus attribute fragments
("-c Switch 1:1 2.3V~3.6V") or an MPN prefix ("SN74CB3T"), not English ("bus switch").
search options:
  -c CAT   category substring (see `cats`)    -p PKG  package substring, '|' for alternatives
  -P PKG   package exact, '|' alternatives    -b      Basic parts only (no "Preferred" flag exists)
  -a       include out-of-stock parts (~1 s)  -m N    minimum stock (default 1)
  -s KEY   sort by stock (default) or price   -n N    rows (default 25)
  -l       add a LIVE LCSC stock column       --tsv   tab-separated full fields
Every search ends with a JLCPCB parts-site search URL representative of the query, for a human
to sanity-check the neighbours of the chosen part (JLC's own search is keyword-based, so it is
not an exact replay).
Database stock is JLC's as of the source's build date; `part` and -l read live LCSC stock.
The same MPN is often listed again for a second-source copy - check the manufacturer column.

Database: $JLCPCB_PARTS_DB, else $XDG_CACHE_HOME/claude-jlcpcb-plugin/parts.db (~/.cache/...).
Source: Bouni/kicad-jlcpcb-tools' daily parts database. Requires Python 3.8+ with SQLite 3.34+;
`import`/`place --import-to` also need easyeda2kicad (`pip install easyeda2kicad`).
"""
import argparse, concurrent.futures, datetime, glob, json, math, os, re, shutil, sqlite3, subprocess
import sys, tempfile, textwrap, urllib.parse, urllib.request, uuid, zipfile

DB = os.environ.get('JLCPCB_PARTS_DB') or os.path.join(
    os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'claude-jlcpcb-plugin', 'parts.db')
SRC_URL = 'https://bouni.github.io/kicad-jlcpcb-tools/'
LIVE_URL = 'https://wmsc.lcsc.com/ftps/wm/product/detail?productCode='
STALE_DAYS = 7
GRID = 1.27


def fetch(url, dest=None, timeout=300):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 claude-jlcpcb-plugin'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if dest is None:
            return r.read()
        with open(dest, 'wb') as f:
            shutil.copyfileobj(r, f, 1 << 20)


# =============================================================================== database
def cmd_update(args):
    home = os.path.dirname(DB)
    os.makedirs(home, exist_ok=True)
    free = shutil.disk_usage(home).free
    if free < 10e9:
        sys.exit(f'need ~10 GB free in {home} while building, have {free / 1e9:.1f} GB')
    if sqlite3.sqlite_version_info < (3, 34):
        sys.exit(f'SQLite {sqlite3.sqlite_version} is too old (need 3.34+ for trigram search)')
    src_dir = os.path.join(home, 'source')
    shutil.rmtree(src_dir, ignore_errors=True)
    os.makedirs(src_dir)
    try:
        n = int(fetch(SRC_URL + 'chunk_num_fts5.txt').decode().strip())
        zpath = os.path.join(src_dir, 'parts-fts5.db.zip')
        with open(zpath, 'wb') as out:
            for i in range(1, n + 1):
                print(f'downloading chunk {i}/{n}', file=sys.stderr, flush=True)
                part = zpath + f'.{i:03d}'
                fetch(SRC_URL + f'parts-fts5.db.zip.{i:03d}', part)
                with open(part, 'rb') as f:
                    shutil.copyfileobj(f, out, 1 << 20)
                os.remove(part)
        print('unzipping', file=sys.stderr, flush=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(src_dir)
        os.remove(zpath)
        print('building the index', file=sys.stderr, flush=True)
        build(os.path.join(src_dir, 'parts-fts5.db'))
    finally:
        shutil.rmtree(src_dir, ignore_errors=True)
    cmd_info(None)


def build(src):
    tmp = DB + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.execute('ATTACH ? AS s', (src,))
    c.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
        CREATE TABLE parts(lcsc INTEGER PRIMARY KEY, cat1, cat2, mfr, package, joints INTEGER,
                           manufacturer, libtype, description, datasheet, price, stock INTEGER);
        INSERT INTO parts SELECT CAST(substr(c0, 2) AS INTEGER), c1, c2, c3, c4, CAST(c5 AS INTEGER),
                                 c6, c7, c8, c9, c10, CAST(c11 AS INTEGER)
                          FROM s.parts_content WHERE c0 GLOB 'C[0-9]*';
        CREATE TABLE meta AS SELECT * FROM s.meta;
        DETACH s;
    """)
    # Mirrors have published partial catalogues before (only LCSC numbers >= C6374509); refuse
    # to replace a good database with one.
    total, = c.execute('SELECT count(*) FROM parts').fetchone()
    if total < 3_000_000 or not c.execute('SELECT 1 FROM parts WHERE lcsc = 20917').fetchone():
        c.close()
        os.remove(tmp)
        sys.exit(f'the source looks partial ({total} parts, AO3400A C20917 missing?) - kept the old database')
    c.executescript("""
        CREATE INDEX parts_stock ON parts(stock);
        CREATE VIRTUAL TABLE fts USING fts5(mfr, package, cat2, manufacturer, description,
                                            content='parts', content_rowid='lcsc', tokenize='trigram');
        INSERT INTO fts(rowid, mfr, package, cat2, manufacturer, description)
            SELECT lcsc, mfr, package, cat2, manufacturer, description FROM parts WHERE stock > 0;
        INSERT INTO fts(fts) VALUES('optimize');
    """)
    c.commit()
    c.close()
    os.replace(tmp, DB)


def connect():
    if not os.path.exists(DB):
        print(f'no parts database at {DB} - run `update` first (~1 GB download)', file=sys.stderr)
        sys.exit(2)
    c = sqlite3.connect(DB)
    c.create_function('tokmatch', 2, tokmatch, deterministic=True)
    return c


def normalise(term):
    t = term.replace('\u2126', '\u03a9')                       # OHM SIGN -> Greek omega, as JLC
    t = re.sub(r'(?i)ohms?', '\u03a9', t)
    t = t.replace('\u00b5', 'u').replace('\u03bc', 'u')        # micro sign / Greek mu -> u
    return re.sub(r'(?i)(°|deg ?)c\b', '℃', t)


_tok = {}


def tokmatch(text, term):
    rx = _tok.get(term)
    if rx is None:
        rx = _tok[term] = re.compile(r'(?<![\w.])' + re.escape(term) + r'(?![\w.])', re.I)
    return 1 if text and rx.search(text) else 0


def unit_price(price):
    try:
        return float(price.split(',')[0].split(':')[1])
    except Exception:
        return None


def live(lcsc):
    """(stock, unit USD price) from LCSC right now, or (None, None)."""
    try:
        r = json.loads(fetch(LIVE_URL + f'C{lcsc}', timeout=20))['result']
        return r.get('stockNumber'), (r.get('productPriceList') or [{}])[0].get('usdPrice')
    except Exception:
        return None, None


def source_date(c):
    try:
        return datetime.date.fromisoformat(str(c.execute('SELECT date FROM meta').fetchone()[0])[:10])
    except Exception:
        return None


def warn_if_stale(c):
    d = source_date(c)
    if d and (datetime.date.today() - d).days > STALE_DAYS:
        print(f'note: database stock is from {d} ({(datetime.date.today() - d).days} days old); '
              f'`part C...` or -l give live stock, `update` refreshes', file=sys.stderr)


def cmd_search(args):
    c = connect()
    warn_if_stale(c)
    fts, where, params, tok = [], ['p.stock >= ?'], [0 if args.all else args.min_stock], []
    hay = "(p.mfr || ' ' || p.package || ' ' || p.cat2 || ' ' || p.manufacturer || ' ' || p.description)"
    for raw in args.terms:
        alts = [normalise(a) for a in raw.split('|') if a]
        plain = [a[1:] if a.startswith('+') else a for a in alts]
        if not plain or not all(plain):
            continue
        if all(a.startswith('+') for a in alts):
            tok.append(plain)
        if not args.all and all(len(a) >= 3 for a in plain):
            fts.append('(' + ' OR '.join('"' + a.replace('"', '""') + '"' for a in plain) + ')')
        else:
            where.append('(' + ' OR '.join(f'{hay} LIKE ?' for _ in plain) + ')')
            params += [f'%{a}%' for a in plain]
    for alts in tok:
        where.append('(' + ' OR '.join("tokmatch(p.mfr || ' ' || p.package || ' ' || p.description, ?)"
                                       for _ in alts) + ')')
        params += alts
    if args.basic:
        where.append("p.libtype = 'Basic'")
    if args.category:
        where.append('(p.cat1 LIKE ? OR p.cat2 LIKE ?)')
        params += [f'%{args.category}%'] * 2
    for opt, op in ((args.package, 'LIKE'), (args.package_exact, '= ? COLLATE NOCASE')):
        if opt:
            alts = [normalise(a) for a in opt.split('|') if a]
            if op == 'LIKE':
                where.append('(' + ' OR '.join('p.package LIKE ?' for _ in alts) + ')')
                params += [f'%{a}%' for a in alts]
            else:
                where.append('(' + ' OR '.join('p.package = ? COLLATE NOCASE' for _ in alts) + ')')
                params += alts
    order = ("(p.price = '') ASC, CAST(substr(p.price, instr(p.price, ':') + 1) AS REAL) ASC, p.stock DESC"
             if args.sort == 'price' else 'p.stock DESC')
    cols = 'p.lcsc, p.mfr, p.package, p.libtype, p.stock, p.price, p.description, p.manufacturer, p.cat2'
    if fts:
        sql = (f'SELECT {cols} FROM fts JOIN parts p ON p.lcsc = fts.rowid '
               f'WHERE fts MATCH ? AND {" AND ".join(where)} ORDER BY {order} LIMIT ?')
        params = [' AND '.join(fts)] + params + [args.n]
    else:
        sql = f'SELECT {cols} FROM parts p WHERE {" AND ".join(where)} ORDER BY {order} LIMIT ?'
        params += [args.n]
    rows = c.execute(sql, params).fetchall()
    show(rows, args.live, args.tsv)
    # JLC's page can't be sorted from its URL (only its Stock column header sorts), so say so.
    print(f'JLCPCB search to sanity-check similar parts (click "Stock" to sort high to low): '
          f'{jlc_search_url(args, rows)}',
          file=sys.stderr if args.tsv else sys.stdout)
    if not rows:
        print('no matches - descriptions are attribute strings; try fewer terms, a category (-c), '
              'an MPN prefix, or -a to include out-of-stock parts', file=sys.stderr)


def jlc_search_url(args, rows):
    """A JLCPCB parts-site search representative of this query, for a human to compare the
    neighbours of the chosen part. Not an exact replay: JLC's search is keyword-based."""
    words = [normalise(t.split('|')[0]).lstrip('+') for t in args.terms if t.strip('|+')]
    for opt in (args.package_exact, args.package):
        if opt:
            words.append(normalise(opt.split('|')[0]))
            break
    if args.category:
        words.append(args.category)
    if not words and rows:
        words.append(rows[0][8])          # top result's category
    return 'https://jlcpcb.com/parts/componentSearch?isSearch=true&searchTxt=' + urllib.parse.quote(' '.join(words))


def show(rows, use_live, tsv):
    lives = {}
    if use_live and rows:
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            lives = dict(zip([r[0] for r in rows], ex.map(lambda r: live(r[0])[0], rows)))
    if tsv:
        print('\t'.join(['lcsc', 'stock'] + (['live'] if use_live else []) +
                        ['type', 'unit_usd', 'package', 'mfr', 'manufacturer', 'category', 'description']))
        for (lcsc, mfr, package, libtype, stock, price, desc, manuf, cat2) in rows:
            print('\t'.join(str(x) for x in [f'C{lcsc}', stock] + ([lives.get(lcsc)] if use_live else []) +
                            [libtype, unit_price(price), package, mfr, manuf, cat2, desc or '']))
        return
    # Truncate only for a real terminal; piped output keeps whole descriptions.
    width = shutil.get_terminal_size().columns if sys.stdout.isatty() else 10 ** 6
    print(f'{"LCSC":>10} {"stock":>9}' + (f' {"live":>9}' if use_live else '') +
          f' T {"$/1":>7} {"package":<16} {"MFR part":<24} {"manufacturer":<14} description')
    for (lcsc, mfr, package, libtype, stock, price, desc, manuf, cat2) in rows:
        p = unit_price(price)
        line = f'{"C" + str(lcsc):>10} {stock:>9}'
        if use_live:
            line += f' {"?" if lives.get(lcsc) is None else lives.get(lcsc):>9}'
        line += (f' {libtype[:1]} {"" if p is None else f"{p:.4f}":>7} {package[:16]:<16} {mfr[:24]:<24} '
                 f'{(manuf or "")[:14]:<14} {desc or "(" + cat2 + ")"}')
        print(line[:width])


def lcsc_number(code):
    m = re.fullmatch(r'\s*[Cc]?(\d+)\s*', code)
    return int(m.group(1)) if m else None


def cmd_part(args):
    c = connect()
    cols = [d[0] for d in c.execute('SELECT * FROM parts LIMIT 0').description]
    for code in args.codes:
        n = lcsc_number(code)
        if n is None:
            print(f'{code}: not an LCSC number (C1234); to find an MPN use `search -a MPN`')
            continue
        r = c.execute('SELECT * FROM parts WHERE lcsc = ?', (n,)).fetchone()
        ls, lp = live(n)
        if not r:
            print(f'C{n}: not in the database' + ('' if ls is None else f' (LCSC lists it: {ls} in stock)'))
            continue
        d = dict(zip(cols, r))
        print(f'C{n}  {d["mfr"]}  ({d["manufacturer"]}, {d["package"]}, {d["libtype"]})')
        print(f'  {d["cat1"]} / {d["cat2"]}')
        if d['description']:
            print(textwrap.fill(d['description'], 100, initial_indent='  ', subsequent_indent='  '))
        print(f'  stock {d["stock"]} in database ({source_date(c)}), '
              f'{"? (live lookup failed)" if ls is None else ls} live on LCSC'
              f'{"" if lp is None else f" at ${lp}/1"}')
        print(f'  price tiers {d["price"]}')
        print(f'  {d["datasheet"]}')


def cmd_cats(args):
    c = connect()
    f = f'%{args.filter or ""}%'
    for cat1, cat2, n in c.execute('SELECT cat1, cat2, count(*) FROM parts WHERE stock > 0 AND '
                                   '(cat1 LIKE ? OR cat2 LIKE ?) GROUP BY 1, 2 ORDER BY 3 DESC', (f, f)):
        print(f'{n:>7}  {cat1} / {cat2}')


def cmd_info(_):
    c = connect()
    total, instock = c.execute('SELECT count(*), sum(stock > 0) FROM parts').fetchone()
    d = source_date(c)
    age = f'{(datetime.date.today() - d).days} days old' if d else 'unknown age'
    print(f'{DB}: {total} parts, {instock} in stock (searchable); stock as of {d} ({age})')


# ========================================================================= s-expressions
def block_at(s, j):
    """(start, end) of the balanced s-expression opening at s[j] == '(' - strings respected."""
    d, k, n = 0, j, len(s)
    while k < n:
        ch = s[k]
        if ch == '"':
            k += 1
            while s[k] != '"':
                k += 2 if s[k] == '\\' else 1
        elif ch == '(':
            d += 1
        elif ch == ')':
            d -= 1
            if d == 0:
                return j, k + 1
        k += 1
    raise ValueError('unbalanced s-expression')


def children(s, a, b):
    """Top-level child expressions of the block s[a:b], as (start, end) pairs."""
    out, k = [], a + 1
    while True:
        k = next_paren(s, k, b - 1)
        if k < 0:
            return out
        x, y = block_at(s, k)
        out.append((x, y))
        k = y


def next_paren(s, k, end):
    while k < end:
        ch = s[k]
        if ch == '"':
            k += 1
            while s[k] != '"':
                k += 2 if s[k] == '\\' else 1
        elif ch == '(':
            return k
        k += 1
    return -1


def head(s, a):
    m = re.match(r'\(\s*([^\s()"]+)', s[a:a + 80])
    return m.group(1) if m else ''


def first_str(t):
    m = re.search(r'"((?:[^"\\]|\\.)*)"', t)
    return m.group(1) if m else None


def strings(t, n):
    return [m.group(1) for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', t)][:n]


def q(v):
    return '"' + str(v).replace('\\', '\\\\').replace('"', '\\"') + '"'


def cut(s, a, b):
    """Remove s[a:b] together with the line indentation and newline in front of it."""
    while a > 0 and s[a - 1] in '\t ':
        a -= 1
    if a > 0 and s[a - 1] == '\n':
        a -= 1
    return s[:a] + s[b:]


# ================================================================================= KiCad
def expand_vars(uri, proj_dir):
    def sub(m):
        name = m.group(1)
        if name == 'KIPRJMOD':
            return proj_dir
        if name in os.environ:
            return os.environ[name]
        if re.fullmatch(r'KICAD\d*_SYMBOL_DIR', name):
            for d in ('/usr/share/kicad/symbols', '/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols',
                      r'C:\Program Files\KiCad\9.0\share\kicad\symbols'):
                if os.path.isdir(d):
                    return d
        return m.group(0)
    return re.sub(r'\$\{([^}]+)\}', sub, uri)


def global_lib_table():
    for base in (os.environ.get('KICAD_CONFIG_HOME'), os.path.expanduser('~/.config/kicad'),
                 os.path.expanduser('~/Library/Preferences/kicad'), os.path.expandvars(r'%APPDATA%\kicad')):
        if base and os.path.isdir(base):
            for v in sorted(os.listdir(base), reverse=True):
                p = os.path.join(base, v, 'sym-lib-table')
                if os.path.exists(p):
                    return p
    return None


def parse_lib_table(path):
    """[(nickname, uri)] - KiCad writes the global tables unquoted, project ones quoted."""
    tok = r'(?:"((?:[^"\\]|\\.)*)"|([^\s()"]+))'
    rx = re.compile(r'\(lib\s*\(name\s+' + tok + r'\)\s*\(type\s+' + tok + r'\)\s*\(uri\s+' + tok + r'\)')
    return [(m.group(1) or m.group(2), m.group(5) or m.group(6))
            for m in rx.finditer(open(path, encoding='utf-8').read())]


def sym_libs(proj_dir):
    """Library nickname -> .kicad_sym path: the project table, then the global one."""
    libs = {}
    for table in [os.path.join(proj_dir, 'sym-lib-table'), global_lib_table()]:
        if table and os.path.exists(table):
            for name, uri in parse_lib_table(table):
                libs.setdefault(name, expand_vars(uri, proj_dir))
    return libs


def lib_symbols_in(path):
    """{name: text} of every top-level symbol in a .kicad_sym."""
    s = open(path, encoding='utf-8').read()
    a, b = block_at(s, s.index('(kicad_symbol_lib'))
    return {first_str(s[x:y]): s[x:y] for x, y in children(s, a, b) if head(s, x) == 'symbol'}


def project_sheets(proj_dir, proj):
    """{sheet file: [instance paths]} for every sheet reachable from the project's root."""
    out = {}

    def walk(f, path, depth):
        if not os.path.exists(f) or depth > 20:
            return
        s = open(f, encoding='utf-8').read()
        path = path or '/' + re.search(r'\(uuid\s+"([^"]+)"', s).group(1)
        out.setdefault(os.path.abspath(f), []).append(path)
        for m in re.finditer(r'\(sheet\s', s):
            x, y = block_at(s, m.start())
            blk = s[x:y]
            fm = re.search(r'\(property\s+"Sheet ?[Ff]ile"\s+"([^"]+)"', blk)
            um = re.search(r'\(uuid\s+"([^"]+)"', blk)
            if fm and um:
                walk(os.path.join(os.path.dirname(f), fm.group(1)), path + '/' + um.group(1), depth + 1)
    walk(os.path.join(proj_dir, proj + '.kicad_sch'), '', 0)
    return out


def find_project(target):
    """(project dir, project name) owning a schematic or library path. A folder can hold several
    projects; for a schematic the one whose sheet tree reaches it wins."""
    target = os.path.abspath(target)
    if target.endswith('.kicad_pro'):
        return os.path.dirname(target), os.path.splitext(os.path.basename(target))[0]
    d = target if os.path.isdir(target) else os.path.dirname(target)
    while True:
        names = sorted(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(os.path.join(d, '*.kicad_pro')))
        if names:
            if target.endswith('.kicad_sch'):
                owners = [n for n in names if target in project_sheets(d, n)]
                if len(owners) == 1:
                    return d, owners[0]
                if not owners:
                    sys.exit(f'{target} is not in the sheet tree of any project in {d} ({", ".join(names)})')
                names = owners
            if len(names) == 1:
                return d, names[0]
            if target.endswith('.kicad_sch'):
                sys.exit(f'{target} belongs to several projects: {", ".join(names)}')
            return d, None          # several projects; fine for library tables, not for schematics
        if os.path.dirname(d) == d:
            sys.exit(f'no .kicad_pro found above {target}')
        d = os.path.dirname(d)


def sheet_paths(proj_dir, proj, target):
    """Every hierarchical instance path of the sheet file `target`, e.g. ['/root', '/root/s1']."""
    paths = project_sheets(proj_dir, proj).get(os.path.abspath(target))
    if not paths:
        sys.exit(f'{target} is not reachable from the project root {proj}.kicad_sch')
    return paths


def check_unlocked(sch, force):
    lock = os.path.join(os.path.dirname(os.path.abspath(sch)), '~' + os.path.basename(sch) + '.lck')
    if os.path.exists(lock) and not force:
        sys.exit(f'{sch} is open in KiCad ({lock}). Close the schematic editor first - saving there would '
                 f'overwrite this edit. (--force to ignore a stale lock.)')


def write_checked(sch, new, proj_dir, proj):
    """Write with a .bak; roll back if kicad-cli can't load the project any more."""
    shutil.copyfile(sch, sch + '.bak')
    with open(sch, 'w', encoding='utf-8') as f:
        f.write(new)
    cli = shutil.which('kicad-cli')
    if not cli:
        print('note: kicad-cli not found, so the result was not load-tested', file=sys.stderr)
        return
    root = os.path.join(proj_dir, proj + '.kicad_sch')
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run([cli, 'sch', 'export', 'netlist', '-o', os.path.join(td, 'n.net'), root],
                           capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copyfile(sch + '.bak', sch)
        sys.exit(f'KiCad could not load the edited schematic, so it was rolled back:\n{r.stderr or r.stdout}')


def placed_symbols(s):
    """[(start, end, reference)] of placed symbols in a schematic."""
    a, b = block_at(s, s.index('(kicad_sch'))
    out = []
    for x, y in children(s, a, b):
        if head(s, x) == 'symbol' and '(lib_id' in s[x:x + 200]:
            m = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', s[x:y])
            out.append((x, y, m.group(1) if m else '?'))
    return out


def prune_cache(s):
    """Drop cached library symbols no placed symbol uses any more."""
    used = set(re.findall(r'\(lib_id\s+"([^"]+)"', s))
    j = s.find('(lib_symbols')
    if j < 0:
        return s
    a, b = block_at(s, j)
    for x, y in reversed(children(s, a, b)):
        if head(s, x) == 'symbol' and first_str(s[x:y]) not in used:
            s = cut(s, x, y)
    return s


def project_refs(proj_dir):
    refs = set()
    for f in glob.glob(os.path.join(proj_dir, '**', '*.kicad_sch'), recursive=True):
        t = open(f, encoding='utf-8').read()
        refs |= set(re.findall(r'\(reference\s+"([^"]+)"', t))
        refs |= set(re.findall(r'\(property\s+"Reference"\s+"([^"?]+)"', t))
    return refs


def next_ref(prefix, refs):
    nums = [int(r[len(prefix):]) for r in refs if r.startswith(prefix) and r[len(prefix):].isdigit()]
    return f'{prefix}{max(nums, default=0) + 1}'


def pin_numbers(sym_text, name, unit):
    """Pin numbers of `unit` (plus the shared unit 0), in order."""
    nums = []
    for m in re.finditer(r'\(symbol\s+"' + re.escape(name) + r'_(\d+)_(\d+)"', sym_text):
        if int(m.group(1)) in (0, unit):
            x, y = block_at(sym_text, m.start())
            nums += re.findall(r'\(number\s+"([^"]*)"', sym_text[x:y])
    seen = set()
    return [n for n in nums if not (n in seen or seen.add(n))]


def lib_properties(sym_text):
    """[(name, value, x, y, hidden)] of a library symbol's own properties."""
    out = []
    a, b = block_at(sym_text, 0)
    for x, y in children(sym_text, a, b):
        if head(sym_text, x) != 'property':
            continue
        t = sym_text[x:y]
        name, value = (strings(t, 2) + [''])[:2]
        at = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)', t)
        hidden = bool(re.search(r'\bhide\b(?!\s+no)', t))
        out.append((name, value, float(at.group(1)) if at else 0.0, float(at.group(2)) if at else 0.0, hidden))
    return out


def find_symbol(proj_dir, part):
    """(nickname, symbol name, symbol text) for an LCSC number or a 'Lib:Symbol' id."""
    libs = sym_libs(proj_dir)
    if ':' in part and lcsc_number(part) is None:
        nick, name = part.split(':', 1)
        if nick not in libs or not os.path.exists(libs[nick]):
            sys.exit(f'library {nick!r} is not in the project or global sym-lib-table')
        syms = lib_symbols_in(libs[nick])
        if name not in syms:
            sys.exit(f'{name!r} is not in {libs[nick]}')
        return nick, name, syms[name]
    n = lcsc_number(part)
    if n is None:
        sys.exit(f'{part!r} is neither an LCSC number nor Lib:Symbol')
    pat = re.compile(r'\(property\s+"(?:LCSC Part|LCSC|LCSC Part #|JLCPCB Part)"\s+"C?' + str(n) + r'"')
    for nick, path in libs.items():          # project libraries come first
        if os.path.exists(path) and path.endswith('.kicad_sym'):
            text = open(path, encoding='utf-8').read()
            if pat.search(text):
                for name, sym in lib_symbols_in(path).items():
                    if pat.search(sym):
                        return nick, name, sym
    return None


def cmd_place(args):
    sch = args.sch
    check_unlocked(sch, args.force)
    proj_dir, proj = find_project(sch)
    found = find_symbol(proj_dir, args.part)
    if found is None:
        if not args.import_to:
            sys.exit(f'{args.part} is in none of the project\'s symbol libraries - add --import-to LIBNICK '
                     f'to fetch it with easyeda2kicad first')
        libs = sym_libs(proj_dir)
        if args.import_to not in libs:
            sys.exit(f'--import-to {args.import_to!r} is not in {os.path.join(proj_dir, "sym-lib-table")}')
        import_parts([args.part], libs[args.import_to], proj_dir)
        found = find_symbol(proj_dir, args.part)
        if found is None:
            sys.exit(f'easyeda2kicad ran but {args.part} still was not found in {libs[args.import_to]}')
    nick, name, sym = found
    if re.search(r'\(extends\s+"', sym):
        sys.exit(f'{nick}:{name} is a derived (extends) symbol, which this tool does not flatten - place it from KiCad')
    lib_id = f'{nick}:{name}'

    s = open(sch, encoding='utf-8').read()
    x, y, rot = args.at[0] if args.at else None, args.at[1] if args.at else None, args.rot
    ref = args.ref
    if args.replace:
        hit = [p for p in placed_symbols(s) if p[2] == args.replace]
        if not hit:
            sys.exit(f'{args.replace} is not placed in {sch}')
        a, b, _ = hit[0]
        old = s[a:b]
        m = re.search(r'\(at\s+([-\d.]+)\s+([-\d.]+)\s*([-\d.]*)\)', old)
        if x is None:
            x, y = float(m.group(1)), float(m.group(2))
            rot = float(m.group(3) or 0) if args.rot is None else args.rot
        ref = ref or args.replace
        s = cut(s, a, b)
    if x is None:
        sys.exit('give --at X Y (mm), or --replace REF')
    x, y = round(x / GRID) * GRID, round(y / GRID) * GRID
    rot = rot or 0
    props = lib_properties(sym)
    prefix = next((v for n, v, *_ in props if n == 'Reference'), 'U').rstrip('?')
    ref = ref or next_ref(prefix, project_refs(proj_dir))
    unit = args.unit

    overrides = {'Reference': ref}
    if args.value:
        overrides['Value'] = args.value
    if args.footprint:
        overrides['Footprint'] = args.footprint
    for kv in args.field or []:
        k, _, v = kv.partition('=')
        overrides[k] = v
    cos, sin = math.cos(math.radians(rot)), math.sin(math.radians(rot))
    lines = []
    names = set()
    # --show: display these fields in a stack under the Value
    vx, vy = next(((px, py) for n, _, px, py, _ in props if n == 'Value'), (0.0, -2.54))
    shown = {name: (vx, vy - 2.54 * (i + 1)) for i, name in enumerate(args.show or [])}
    props = [(n, v, *shown[n], False) if n in shown else (n, v, px, py, h) for n, v, px, py, h in props]
    for field in shown:
        if field not in [n for n, *_ in props]:
            sys.exit(f'--show {field!r}: the symbol has no such field')
    for pname, pval, px, py, hidden in props:
        if pname.startswith('ki_'):
            continue
        names.add(pname)
        pval = overrides.get(pname, pval)
        # library coordinates are Y-up; schematic coordinates are Y-down
        dx, dy = px * cos - py * sin, -(px * sin + py * cos)
        lines.append(f'\t\t(property {q(pname)} {q(pval)} (at {x + dx:.3f} {y + dy:.3f} 0)'
                     f' (effects (font (size 1.27 1.27)){" (hide yes)" if hidden else ""}))')
    for k, v in overrides.items():
        if k not in names:
            lines.append(f'\t\t(property {q(k)} {q(v)} (at {x:.3f} {y:.3f} 0) (effects (font (size 1.27 1.27)) (hide yes)))')
    pins = pin_numbers(sym, name, unit)
    if not pins:
        sys.exit(f'{lib_id} has no pins in unit {unit} - refusing to place it (KiCad would drop it from the netlist)')
    paths = sheet_paths(proj_dir, proj, sch)
    inst = ' '.join(f'(path {q(p)} (reference {q(ref)}) (unit {unit}))' for p in paths)
    block = (f'\t(symbol\n\t\t(lib_id {q(lib_id)})\n\t\t(at {x:.3f} {y:.3f} {rot:g})\n\t\t(unit {unit})\n'
             f'\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n'
             f'\t\t(dnp {"yes" if args.dnp else "no"})\n\t\t(uuid {q(uuid.uuid4())})\n' + '\n'.join(lines) + '\n' +
             ''.join(f'\t\t(pin {q(n)} (uuid {q(uuid.uuid4())}))\n' for n in pins) +
             f'\t\t(instances (project {q(proj)} {inst}))\n\t)\n')

    # cache the library symbol in the schematic, as KiCad does
    if not re.search(r'\(symbol\s+' + re.escape(q(lib_id)), s):
        cached = sym.replace(f'(symbol {q(name)}', f'(symbol {q(lib_id)}', 1)
        cached = '\n'.join('\t\t' + ln for ln in cached.split('\n'))
        j = s.find('(lib_symbols')
        if j < 0:
            k = s.index('\n', s.index('(paper'))
            s = s[:k + 1] + '\t(lib_symbols\n' + cached + '\n\t)\n' + s[k + 1:]
        else:
            a, b = block_at(s, j)
            s = s[:b - 1].rstrip() + '\n' + cached + '\n\t' + s[b - 1:]
    end = s.rindex(')')
    tail = re.search(r'\n\t\((sheet_instances|embedded_fonts)', s)
    at = tail.start() + 1 if tail else end
    s = prune_cache(s[:at] + block + s[at:])
    write_checked(sch, s, proj_dir, proj)
    print(f'placed {ref} = {lib_id} at ({x:.2f}, {y:.2f}){" [DNP]" if args.dnp else ""} in {sch}')


def cmd_remove(args):
    sch = args.sch
    check_unlocked(sch, args.force)
    proj_dir, proj = find_project(sch)
    s = open(sch, encoding='utf-8').read()
    gone = []
    for a, b, ref in reversed(placed_symbols(s)):
        if ref in args.refs:
            s = cut(s, a, b)
            gone.append(ref)
    missing = set(args.refs) - set(gone)
    if missing:
        sys.exit(f'not placed in {sch}: {", ".join(sorted(missing))} (nothing changed)')
    write_checked(sch, prune_cache(s), proj_dir, proj)
    print(f'removed {", ".join(reversed(gone))} from {sch}')


# ============================================================================ import
# easyeda2kicad writes 3D model paths relative to the directory it ran in, and names the footprint
# library after the output file; KiCad resolves neither unless the project happens to match. So an
# import here also registers the libraries in the project's own tables and rewrites model paths
# to ${KIPRJMOD}/..., then checks that the footprint and model really resolve.
TABLES = {'sym': ('sym-lib-table', 'sym_lib_table'), 'fp': ('fp-lib-table', 'fp_lib_table')}


def table_entries(path, proj_dir):
    if not path or not os.path.exists(path):
        return {}
    return {name: os.path.normpath(expand_vars(uri, proj_dir)) for name, uri in parse_lib_table(path)}


def global_table(kind):
    t = global_lib_table()
    return t and os.path.join(os.path.dirname(t), TABLES[kind][0])


def portable(path, proj_dir):
    """${KIPRJMOD}-relative when inside the project (works on any machine), else absolute."""
    path = os.path.abspath(path)
    if proj_dir:
        rel = os.path.relpath(path, proj_dir)
        if not rel.startswith('..'):
            return '${KIPRJMOD}/' + rel.replace(os.sep, '/')
    return path.replace(os.sep, '/')


def ensure_table_entry(proj_dir, kind, lib_path, nick):
    """Nickname under which the project table lists lib_path, adding an entry if needed."""
    fname, root = TABLES[kind]
    table = os.path.join(proj_dir, fname)
    want = os.path.normpath(os.path.abspath(lib_path))
    local = table_entries(table, proj_dir)
    for name, path in local.items():
        if path == want:
            return name
    taken = set(local) | set(table_entries(global_table(kind), proj_dir))
    base, i = nick, 2
    while nick in taken:
        nick, i = f'{base}_{i}', i + 1
    line = f'  (lib (name "{nick}")(type "KiCad")(uri "{portable(want, proj_dir)}")(options "")(descr ""))\n'
    if os.path.exists(table):
        t = open(table, encoding='utf-8').read().rstrip()
        t = t[:-1].rstrip() + '\n' + line + ')\n'
    else:
        t = f'({root}\n  (version 7)\n{line})\n'
    open(table, 'w', encoding='utf-8').write(t)
    print(f'  added "{nick}" to {fname} -> {portable(want, proj_dir)}')
    return nick


def resolve_path(p, proj_dir, kind_dir):
    """Expand a KiCad path the way KiCad would; None if a variable can't be resolved."""
    p = expand_vars(p, proj_dir or '')
    m = re.match(r'\$\{(KICAD\d*_3DMODEL_DIR|KICAD\d*_FOOTPRINT_DIR)\}', p)
    if m:
        for d in ('/usr/share/kicad/' + kind_dir, '/Applications/KiCad/KiCad.app/Contents/SharedSupport/' + kind_dir,
                  r'C:\Program Files\KiCad\9.0\share\kicad' + '\\' + kind_dir):
            if os.path.isdir(d):
                p = d + p[m.end():]
                break
        else:
            return None
    if '${' in p:
        return None
    return p if os.path.isabs(p) else os.path.join(proj_dir or '.', p)


def fix_models(mod_file, proj_dir, fix):
    """Problems with a footprint's 3D model paths; with fix, repair what can be found on disk."""
    t = open(mod_file, encoding='utf-8').read()
    problems, changed = [], False
    for m in reversed(list(re.finditer(r'\(model\s+"([^"]+)"', t))):
        path = m.group(1)
        stock = re.match(r'\$\{KICAD\d*_3DMODEL_DIR\}', path)
        r = resolve_path(path, proj_dir, '3dmodels')
        if r and os.path.exists(r) and (stock or path.startswith('${KIPRJMOD}')):
            continue
        if r and os.path.exists(r):
            problem = f'model path {path} only resolves on this machine'
        elif stock:
            continue        # KiCad's own model library, not installed here: not this project's to fix
        else:
            problem = f'3D model {path} not found'
        # look for the file next to the footprint library: <lib>.3dshapes, then any .3dshapes nearby
        name = os.path.basename(path.replace('\\', '/'))
        stem = os.path.splitext(name)[0]
        lib_dir = os.path.dirname(os.path.dirname(os.path.abspath(mod_file)))
        cands = [c for ext in ('', '.step', '.stp', '.wrl') for c in
                 glob.glob(os.path.join(lib_dir, '**', '*.3dshapes', (stem + ext) if ext else name), recursive=True)]
        if fix and cands:
            new = portable(cands[0], proj_dir)
            t = t[:m.start(1)] + new + t[m.end(1):]
            changed = True
            problems.append(f'fixed: {path} -> {new}')
        else:
            problems.append(problem + ('' if cands else ' (no such file under the library)'))
    if changed:
        open(mod_file, 'w', encoding='utf-8').write(t)
    return problems


def easyeda_cmd():
    exe = shutil.which('easyeda2kicad')
    if exe:
        return [exe]
    try:
        import easyeda2kicad  # noqa: F401
        return [sys.executable, '-m', 'easyeda2kicad']
    except ImportError:
        sys.exit('easyeda2kicad is not installed: `pip install easyeda2kicad` (or `uv tool install easyeda2kicad`)')


def find_footprint(fp_id, proj_dir):
    """Path of the .kicad_mod for 'Nick:Name', via the project then the global fp-lib-table."""
    if ':' not in fp_id:
        return None, 'no library nickname'
    nick, name = fp_id.split(':', 1)
    for table in (os.path.join(proj_dir, 'fp-lib-table'), global_table('fp')):
        for n, path in table_entries(table, proj_dir).items():
            if n == nick:
                d = resolve_path(path, proj_dir, 'footprints') or path
                f = os.path.join(d, name + '.kicad_mod')
                return (f, None) if os.path.exists(f) else (None, f'{name}.kicad_mod not in {d}')
    return None, f'library "{nick}" is in neither fp-lib-table'


def pad_report(mod_file):
    t = open(mod_file, encoding='utf-8').read()
    pads = [(float(m.group(1)), float(m.group(2))) for m in
            re.finditer(r'\(pad\s+"?[^"\s]*"?\s+\w+\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)', t)]
    pitch = min((math.dist(p, q2) for i, p in enumerate(pads) for q2 in pads[i + 1:]), default=0)
    return f'{len(pads)} pads, closest pad pitch {pitch:.3f} mm'


def import_parts(codes, lib_path, proj_dir=None):
    if not lib_path.endswith('.kicad_sym'):
        sys.exit(f'--lib must be a .kicad_sym file, got {lib_path}')
    lib_path = os.path.abspath(lib_path)
    base = lib_path[:-len('.kicad_sym')]
    os.makedirs(os.path.dirname(lib_path), exist_ok=True)
    if proj_dir is None:
        try:
            proj_dir, _ = find_project(lib_path)
        except SystemExit:
            proj_dir = None
            print('note: no KiCad project above the library, so library tables are not updated and model '
                  'paths are absolute (they will only work on this machine)', file=sys.stderr)
    nick = os.path.basename(base)
    for code in codes:
        n = lcsc_number(code)
        if n is None:
            sys.exit(f'{code} is not an LCSC number')
        r = subprocess.run(easyeda_cmd() + ['--full', f'--lcsc_id=C{n}', '--output', base],
                           capture_output=True, text=True, cwd=os.path.dirname(lib_path))
        out = (r.stdout + r.stderr).strip()
        m = re.search(r'Symbol name\s*:\s*(.+)', out)
        if not m and 'already' not in out:
            sys.exit(f'easyeda2kicad failed for C{n}:\n{out}')
        print(f'C{n}: {m.group(1).strip() if m else "already in the library"} -> {lib_path}')
        for ln in out.splitlines():
            if ('ERROR' in ln and 'already exists' not in ln) or 'WARNING' in ln:
                print('  ' + ln.strip())
    if proj_dir:
        sym_nick = ensure_table_entry(proj_dir, 'sym', lib_path, nick)
        fp_nick = ensure_table_entry(proj_dir, 'fp', base + '.pretty', nick)
    else:
        sym_nick = fp_nick = nick
    # point the imported symbols' Footprint at the nickname the project really uses
    text = open(lib_path, encoding='utf-8').read()
    pat = r'(\(property\s+"Footprint"\s+")' + re.escape(nick) + ':'
    if fp_nick != nick:
        text = re.sub(pat, r'\g<1>' + fp_nick + ':', text)
        open(lib_path, 'w', encoding='utf-8').write(text)
    verify_imported(codes, lib_path, sym_nick, proj_dir)


def verify_imported(codes, lib_path, sym_nick, proj_dir):
    syms = lib_symbols_in(lib_path)
    for code in codes:
        n = lcsc_number(code)
        hit = [(name, t) for name, t in syms.items()
               if re.search(r'\(property\s+"LCSC Part"\s+"C?' + str(n) + '"', t)]
        if not hit:
            print(f'  C{n}: PROBLEM - no symbol carries LCSC Part C{n}')
            continue
        name, t = hit[0]
        fp = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', t)
        fp_id = fp.group(1) if fp else ''
        f, why = find_footprint(fp_id, proj_dir) if proj_dir else (None, 'no project')
        if not f:
            print(f'  C{n}: {sym_nick}:{name} - PROBLEM: footprint {fp_id}: {why}')
            continue
        issues = fix_models(f, proj_dir, fix=True)
        bad = [i for i in issues if not i.startswith('fixed')]
        print(f'  C{n}: {sym_nick}:{name}, footprint {fp_id} ({pad_report(f)}), '
              f'3D model {"PROBLEM: " + "; ".join(bad) if bad else "ok"}')
        for i in issues:
            if i.startswith('fixed'):
                print('    ' + i)


def cmd_import(args):
    import_parts(args.codes, args.lib, args.project)


def cmd_check(args):
    """Every placed symbol's footprint and 3D model must resolve on any machine."""
    proj_dir, proj = find_project(args.sch or args.project)
    if not proj:
        sys.exit(f'{args.project} holds several projects - name one: --project path/to/name.kicad_pro')
    files = [args.sch] if args.sch else list(project_sheets(proj_dir, proj))
    seen, problems, n_ok = {}, 0, 0
    for f in files:
        s = open(f, encoding='utf-8').read()
        for a, b, ref in placed_symbols(s):
            blk = s[a:b]
            fp = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', blk)
            fp_id = fp.group(1) if fp else ''
            if ref.startswith('#') or re.search(r'\(on_board\s+no\)', blk):
                continue        # power flags and symbols that never reach the board
            if not fp_id:
                print(f'{ref}: no footprint assigned')
                problems += 1
                continue
            if fp_id not in seen:
                path, why = find_footprint(fp_id, proj_dir)
                issues = [why] if not path else fix_models(path, proj_dir, args.fix)
                seen[fp_id] = issues
            issues = seen[fp_id]
            if issues:
                for i in issues:
                    print(f'{ref}: {fp_id}: {i}')
                problems += sum(not i.startswith('fixed') for i in issues)
            else:
                n_ok += 1
    print(f'{n_ok} parts ok, {problems} problem(s)' + ('' if args.fix or not problems else
                                                        ' - `check --fix` repairs model paths it can find'))
    sys.exit(1 if problems else 0)


# ========================================================================= datasheets
# Fetch and extract, never interpret: a datasheet's tables differ per vendor, and Claude reads
# page text reliably, so the tool's job is a cached PDF plus text with page markers, and a way
# to pull out the pages that matter (absolute maximum ratings, pinout, package drawing).
EXTRACT = r"""
import sys, logging
logging.disable(logging.WARNING)
from pypdf import PdfReader
for i, page in enumerate(PdfReader(sys.argv[1]).pages, 1):
    try:
        t = page.extract_text(extraction_mode='layout')   # keeps lines and table columns
    except Exception:
        t = page.extract_text()
    sys.stdout.write(f'\n=== page {i} ===\n' + (t or ''))
"""


def ds_dir():
    d = os.path.join(os.path.dirname(DB), 'datasheets')
    os.makedirs(d, exist_ok=True)
    return d


def is_pdf(path):
    with open(path, 'rb') as f:
        return f.read(5) == b'%PDF-'


def datasheet_urls(n):
    """Candidate PDF links, manufacturer first: LCSC's own copy is often a Chinese translation."""
    urls = []
    if os.path.exists(DB):
        row = sqlite3.connect(DB).execute('SELECT datasheet FROM parts WHERE lcsc = ?', (n,)).fetchone()
        if row and row[0]:
            url = row[0]
            if 'ti.com/cn/' in url:
                urls.append(url.replace('ti.com/cn/', 'ti.com/'))    # TI's English original
            urls.append(url)
    try:
        r = json.loads(fetch(LIVE_URL + f'C{n}', timeout=20))['result']
        urls.append(r.get('pdfUrl'))
    except Exception:
        pass
    return [u for i, u in enumerate(urls) if u and u not in urls[:i]]


def cjk_share(text):
    letters = [c for c in text if c.isalpha()]
    return sum('\u4e00' <= c <= '\u9fff' for c in letters) / max(len(letters), 1)


def extract(pdf):
    if shutil.which('pdftotext'):           # poppler keeps table columns apart well
        r = subprocess.run(['pdftotext', '-layout', pdf, '-'], capture_output=True, text=True)
        pages = r.stdout.split('\f')
        text = ''.join(f'\n=== page {i} ===\n{p}' for i, p in enumerate(pages, 1) if p.strip() or i < len(pages))
    else:
        venv = os.path.join(os.path.dirname(DB), 'venv')
        py = os.path.join(venv, 'Scripts' if os.name == 'nt' else 'bin', 'python')
        if not os.path.exists(py):
            print(f'first datasheet: setting up a private Python environment with pypdf in {venv}',
                  file=sys.stderr)
            subprocess.run([sys.executable, '-m', 'venv', venv], check=True)
            subprocess.run([py, '-m', 'pip', 'install', '-q', 'pypdf'], check=True)
        r = subprocess.run([py, '-I', '-c', EXTRACT, pdf], capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr)
        text = r.stdout
    return text.replace('\u25a1', ' ')      # some PDFs encode spaces as a placeholder glyph


def get_datasheet(n):
    """(pdf, txt) for a part, cached; prefers an English text when one is available."""
    pdf, txt = (os.path.join(ds_dir(), f'C{n}.{ext}') for ext in ('pdf', 'txt'))
    if os.path.exists(pdf) and os.path.exists(txt):
        return pdf, txt
    urls, fallback = datasheet_urls(n), None
    for i, url in enumerate(urls):
        tmp = os.path.join(ds_dir(), f'C{n}.{i}.part')
        try:
            fetch(url, tmp, timeout=120)
            if not is_pdf(tmp):
                raise ValueError('not a PDF')
            text = extract(tmp)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            continue
        if cjk_share(text) < 0.2:
            os.replace(tmp, pdf)
            open(txt, 'w', encoding='utf-8').write(text)
            if fallback:
                os.remove(fallback[0])
            return pdf, txt
        if fallback:
            os.remove(tmp)
        else:
            fallback = (tmp, text, url)
    if fallback:
        print(f'note: only a Chinese-language datasheet was found for C{n} ({fallback[2]})', file=sys.stderr)
        os.replace(fallback[0], pdf)
        open(txt, 'w', encoding='utf-8').write(fallback[1])
        return pdf, txt
    sys.exit(f'no PDF datasheet found for C{n} (tried: {", ".join(urls) or "nothing"})')


def cmd_datasheet(args):
    # The PDF itself is the thing to read (Claude Code's Read tool renders pages, tables and
    # drawings included); the text here is an index for finding WHICH pages, and a fallback
    # where PDFs can't be rendered.
    for code in args.codes:
        n = lcsc_number(code)
        if n is None:
            sys.exit(f'{code} is not an LCSC number')
        pdf, txt = get_datasheet(n)
        text = open(txt, encoding='utf-8').read()
        pages = re.split(r'\n=== page (\d+) ===\n', text)[1:]
        pages = {int(pages[i]): pages[i + 1] for i in range(0, len(pages), 2)}
        print(f'C{n}: {pdf} ({len(pages)} pages)\n  text index: {txt}')
        show = []
        if args.find:
            # a space in the pattern matches any whitespace run: PDF text breaks lines anywhere
            rx = re.compile(re.sub(r' +', r'\\s+', args.find), re.I)
            hits = [i for i, t in pages.items() if rx.search(t)]
            print(f'  "{args.find}" on page(s) {", ".join(map(str, hits)) or "none"}')
            for i in hits:
                m = rx.search(pages[i])
                line = pages[i][pages[i].rfind('\n', 0, m.start()) + 1:pages[i].find('\n', m.end())]
                print(f'    p{i}: {" ".join(line.split())[:140]}')
            show = hits[:args.max_pages]
        if args.pages:
            a, _, b = args.pages.partition('-')
            show += list(range(int(a), int(b or a) + 1))
        if args.text:
            for i in show:
                if i in pages:
                    print(f'\n=== page {i} ===\n{pages[i].strip()}')


# ============================================================================== main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('update').set_defaults(fn=cmd_update)
    s = sub.add_parser('search')
    s.add_argument('terms', nargs='*')
    s.add_argument('-c', '--category')
    s.add_argument('-p', '--package')
    s.add_argument('-P', '--package-exact')
    s.add_argument('-b', '--basic', action='store_true')
    s.add_argument('-a', '--all', action='store_true')
    s.add_argument('-m', '--min-stock', type=int, default=1)
    s.add_argument('-s', '--sort', choices=['stock', 'price'], default='stock')
    s.add_argument('-n', type=int, default=25)
    s.add_argument('-l', '--live', action='store_true')
    s.add_argument('--tsv', action='store_true')
    s.set_defaults(fn=cmd_search)
    p = sub.add_parser('part')
    p.add_argument('codes', nargs='+')
    p.set_defaults(fn=cmd_part)
    k = sub.add_parser('cats')
    k.add_argument('filter', nargs='?')
    k.set_defaults(fn=cmd_cats)
    sub.add_parser('info').set_defaults(fn=cmd_info)
    i = sub.add_parser('import')
    i.add_argument('codes', nargs='+')
    i.add_argument('--lib', required=True, help='target .kicad_sym')
    i.add_argument('--project', help='project directory (default: the one above --lib)')
    i.set_defaults(fn=cmd_import)
    ck = sub.add_parser('check')
    ck.add_argument('--sch')
    ck.add_argument('--project', default='.')
    ck.add_argument('--fix', action='store_true')
    ck.set_defaults(fn=cmd_check)
    pl = sub.add_parser('place')
    pl.add_argument('part', help='LCSC number (C1234) or Lib:Symbol')
    pl.add_argument('--sch', required=True)
    pl.add_argument('--at', nargs=2, type=float, metavar=('X', 'Y'))
    pl.add_argument('--rot', type=float)
    pl.add_argument('--ref')
    pl.add_argument('--replace', metavar='REF')
    pl.add_argument('--unit', type=int, default=1)
    pl.add_argument('--value')
    pl.add_argument('--footprint')
    pl.add_argument('--field', action='append', metavar='NAME=VALUE')
    pl.add_argument('--dnp', action='store_true', help='do not populate (hand-fitted parts)')
    pl.add_argument('--show', action='append', metavar='FIELD', help='display FIELD under the value, e.g. "LCSC Part"')
    pl.add_argument('--import-to', metavar='LIBNICK')
    pl.add_argument('--force', action='store_true')
    pl.set_defaults(fn=cmd_place)
    ds = sub.add_parser('datasheet')
    ds.add_argument('codes', nargs='+')
    ds.add_argument('--find', metavar='REGEX', help='list the pages matching REGEX, with the matching line')
    ds.add_argument('--pages', metavar='A[-B]', help='pages to print with --text')
    ds.add_argument('--text', action='store_true', help='print the extracted text of the found/given pages')
    ds.add_argument('--max-pages', type=int, default=3, help='with --find --text, at most this many pages')
    ds.set_defaults(fn=cmd_datasheet)
    r = sub.add_parser('remove')
    r.add_argument('refs', nargs='+')
    r.add_argument('--sch', required=True)
    r.add_argument('--force', action='store_true')
    r.set_defaults(fn=cmd_remove)
    args = ap.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
