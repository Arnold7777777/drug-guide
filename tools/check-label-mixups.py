#!/usr/bin/env python3
"""Find records whose mechanism text belongs to a different drug.

Four entries carried another drug's label prose in data/*.json: docusate
sodium described risperidone, calcium carbonate described risperidone too,
aluminum hydroxide described gabapentin, and glycerin described levofloxacin.
Each of those records cites two SPL set_ids, and in every case the second one
no longer resolves to a label - a dead id whose text was merged in by whatever
built the data.

The data/*.js view was not affected. It carries its own short clinical
mechanism, which stayed correct throughout, so nothing the site displayed was
ever wrong. This only repairs the .json.

The check has two parts, and neither guesses:

  * a mechanism that names some OTHER drug in the guide and never names this
    one is flagged for a human to read
  * every SPL set_id is resolved against openFDA; one that returns no label,
    or a label for a different generic, is dropped from the record's sources

Repair takes the mechanism from the .js if it has one, and otherwise clears
the field. It never writes prose of its own.

Needs outbound access to api.fda.gov.

    python3 tools/check-label-mixups.py            # report only
    python3 tools/check-label-mixups.py --fix      # repair the .json
"""
import json, glob, os, re, sys, time, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FDA = "https://api.fda.gov/drug/label.json"


def js_rows(letter):
    p = os.path.join(DATA, letter + ".js")
    s = open(p).read()
    return json.loads(s[s.index("["):s.rindex("]") + 1])


def set_ids(rec):
    out = []
    for s in rec.get("src") or []:
        m = re.search(r"set_id:%22([0-9a-f-]{36})%22|setid=([0-9a-f-]{36})", s.get("url", ""))
        if m:
            out.append(m.group(1) or m.group(2))
    return list(dict.fromkeys(out))


def label(sid):
    url = FDA + "?" + urllib.parse.urlencode({"search": 'set_id:"%s"' % sid, "limit": 1})
    try:
        with urllib.request.urlopen(url, timeout=45) as r:
            res = json.load(r).get("results", [])
    except Exception:
        return None
    return (res[0].get("openfda") or {}) if res else {}


def stem(n):
    s = re.sub(r"[^a-z]", "", n.lower())
    return s[:9] if len(s) >= 7 else None


def main():
    fix = "--fix" in sys.argv
    letters = sorted(os.path.basename(f)[0] for f in glob.glob(os.path.join(DATA, "[A-Z].json")))
    everything = {}
    for L in letters:
        for d in json.load(open(os.path.join(DATA, L + ".json"))):
            everything[d["n"]] = L
    stems = {n: stem(n) for n in everything if stem(n)}

    flagged, dropped = [], []
    for L in letters:
        path = os.path.join(DATA, L + ".json")
        rows = json.load(open(path))
        live = {d["n"]: d for d in js_rows(L)}
        changed = False
        for d in rows:
            moa = (d.get("moa") or "")
            flat = re.sub(r"[^a-z]", "", moa.lower())
            mine = stems.get(d["n"])
            if len(moa) >= 40 and mine and mine not in flat:
                others = sorted(n for n, s in stems.items() if n != d["n"] and s in flat)
                if others:
                    flagged.append((d["n"], others[:3], moa[:70]))
                    if fix:
                        d["moa"] = (live.get(d["n"], {}).get("moa") or "")
                        changed = True
            keep = []
            for s in (d.get("src") or []):
                m = re.search(r"set_id:%22([0-9a-f-]{36})%22|setid=([0-9a-f-]{36})", s.get("url", ""))
                if not m:
                    keep.append(s); continue
                sid = m.group(1) or m.group(2)
                of = label(sid)
                time.sleep(0.25)
                if of is None:
                    keep.append(s); continue          # network hiccup, keep it
                gens = [g.lower() for g in (of.get("generic_name") or [])]
                first = d["n"].split()[0].lower()
                if gens and any(first in g for g in gens):
                    keep.append(s)
                else:
                    dropped.append((d["n"], sid, (gens[:1] or ["(no label)"])[0]))
                    changed = True
            if fix and keep != (d.get("src") or []):
                d["src"] = keep
        if fix and changed:
            open(path, "w").write(json.dumps(rows, ensure_ascii=False))

    print("mechanisms naming another drug: %d" % len(flagged))
    for n, others, m in flagged:
        print("   %-30s names %s" % (n[:30], others))
        print("       %s..." % m)
    print()
    print("SPL ids that do not resolve to this drug: %d" % len(dropped))
    for n, sid, got in dropped:
        print("   %-30s %s -> %s" % (n[:30], sid[:8], got[:36]))
    if not fix:
        print("\n(report only - pass --fix to repair)")
    return 1 if (flagged or dropped) and not fix else 0


if __name__ == "__main__":
    sys.exit(main())
