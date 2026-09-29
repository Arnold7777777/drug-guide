#!/usr/bin/env python3
"""Recompute the drug counts written into the HTML from the data itself.

Every page states how many drugs the guide holds. Those numbers were typed by
hand, so they drifted: on 29 Sep 2026 eight pages said "981 drugs" and
by-class.html and by-moa.html said "959 drugs", while data/index.json held
979. provenance.json explains the 981 - the build dropped two entries,
"ombitasvir/paritaprevir/ritonavir and dasabuvir" and "transdermal" - but the
pages were never updated, and both still carried dead links to them.

So derive the number instead of typing it. The count is len(data/index.json),
which is the list every page actually searches.

It also resyncs any data/*.js that has drifted from its .json, because the
site loads the .js and the count is only true if those agree.

    python3 tools/refresh-counts.py           # rewrite, print a summary
    python3 tools/refresh-counts.py --check   # exit 1 if anything is stale
"""
import json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Only the claims that state the WHOLE guide. Anchored on their surrounding
# words on purpose: antidotes.html also says "110 drugs from the guide with a
# documented overdose treatment", which counts something else entirely and must
# not be rewritten.
TOTAL_CLAIMS = [
    re.compile(r'(?<=<span class="nsub">)(\d{2,5})(?= drugs, searchable</span>)'),
    re.compile(r'(?<=<p class="lede">)(\d{2,5})(?= drugs\. Search by)'),
    re.compile(r"(?<=Every one of the <b>)(\d{2,5})(?= drugs</b> is here exactly once)"),
    re.compile(r"(?<=Same <b>)(\d{2,5})(?= drugs</b>)"),
]


def main():
    check = "--check" in sys.argv
    n = len(json.loads((DATA / "index.json").read_text()))

    # the letter files and the index must agree, or the number is meaningless
    letters = sum(len(json.loads(p.read_text())) for p in sorted(DATA.glob("[A-Z].json")))
    if letters != n:
        sys.exit("data/index.json has %d rows but the letter files hold %d - "
                 "fix the data before trusting a count" % (n, letters))

    # The site loads data/*.js, not the .json, and they are two different views:
    # the .js carries pron, dnc, ncls, od, when and wtag, which the .json has no
    # column for. So a .js is NEVER rebuilt from its .json - that would delete
    # every pronunciation, do-not-confuse pair, timing tag and antidote flag.
    #
    # What did drift is membership. provenance.json records two entries the build
    # dropped, and index.js, O.js and T.js still carried them, so the site really
    # served 981 searchable rows with two ghosts behind them. Drop exactly those
    # names, by name, and touch nothing else.
    dropped = set(json.loads((DATA / "provenance.json").read_text()).get("dropped") or [])
    resynced = []
    if dropped:
        for jsp in sorted(DATA.glob("*.js")):
            raw = jsp.read_text()
            try:
                rows = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
            except ValueError:
                continue                      # nursing.js is not a plain array
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                continue
            keep = [d for d in rows if d.get("n") not in dropped]
            if len(keep) == len(rows):
                continue
            resynced.append((jsp.name, len(rows), len(keep)))
            if not check:
                jsp.write_text('DG.put("%s",%s);' % (jsp.stem, json.dumps(keep, ensure_ascii=False)))
    for name, was, now in resynced:
        print("%-18s dropped %d stale entr%s (%d -> %d rows)"
              % (name, was - now, "y" if was - now == 1 else "ies", was, now))

    # The two views must agree on i as well as on membership. They did not: on
    # 29 Sep 2026 961 of 979 drugs carried a different i in the .json than in the
    # .js, which would have made fetch-routes.py attach routes to the wrong
    # drugs. The .js is authoritative here because it is what the site loads.
    drift = []
    for jsonp in sorted(DATA.glob("[A-Z].json")):
        jsp = jsonp.with_suffix(".js")
        if not jsp.exists():
            continue
        raw = jsp.read_text()
        live = {d.get("n"): d.get("i") for d in json.loads(raw[raw.index("["):raw.rindex("]") + 1])}
        rows = json.loads(jsonp.read_text())
        off = [d for d in rows if d.get("n") in live and d.get("i") != live[d["n"]]]
        if off:
            drift.append((jsonp.name, len(off), len(rows)))
            if not check:
                for d in rows:
                    if d.get("n") in live:
                        d["i"] = live[d["n"]]
                jsonp.write_text(json.dumps(rows, ensure_ascii=False))
    for name, off, tot in drift:
        print("%-18s %d of %d i values re-pointed at the .js" % (name, off, tot))

    changes, stale = [], 0
    for path in sorted(ROOT.glob("*.html")):
        src = path.read_text(encoding="utf-8")
        seen = []

        def fix(m):
            if m.group(1) == str(n):
                return m.group(0)
            seen.append(m.group(1))
            return str(n)

        new = src
        for pat in TOTAL_CLAIMS:
            new = pat.sub(fix, new)
        if new != src:
            stale += 1
            for old in seen:
                changes.append((path.name, old, str(n)))
            if not check:
                path.write_text(new, encoding="utf-8")

    for name, old, new in changes:
        print("%-18s %r drugs -> %r drugs" % (name, old, new))
    print("%d count%s %s in %d file%s (data holds %d)" % (
        len(changes), "" if len(changes) == 1 else "s",
        "stale" if check else "updated", stale, "" if stale == 1 else "s", n))

    prov = json.loads((DATA / "provenance.json").read_text())
    if prov.get("drugs") != n:
        print("note: provenance.json says drugs=%r, data says %d" % (prov.get("drugs"), n))

    return 1 if (check and (changes or resynced or drift)) else 0


if __name__ == "__main__":
    sys.exit(main())
