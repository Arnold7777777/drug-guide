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


# Records confirmed by hand to carry ANOTHER DRUG'S label prose in .json. The
# heuristic below flags about 42, and most of those are honest: ORILISSA really
# is elagolix, Veltassa really is patiromer, anastrozole really does talk about
# estrogen, and a combination product really does describe its own ingredients.
# Auto-repairing everything the heuristic flags would therefore throw away ~30
# correct mechanisms, so --fix acts only on this list. Each line was checked
# against the drug's own .js row before being added.
CONFIRMED = {
    "acetylcysteine":        "acetaminophen",
    "aluminum hydroxide":    "gabapentin",
    "calcium carbonate":     "risperidone",
    "docusate sodium":       "risperidone",
    "glycerin":              "levofloxacin",
    "ketorolac (ophthalmic)": "lidocaine",
    "magnesium sulfate":     "olanzapine",
    "misoprostol":           "ondansetron",
    "potassium iodide":      "lidocaine",
    "pseudoephedrine":       "hydrocodone",
    "pyrazinamide":          "rifampin",
    "pyridoxine (vit B6)":   "apomorphine (KYNMOBI)",
    "sodium bicarbonate":    "valproate",
}

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


BATCH = 20          # set_ids per query; the URL has to stay under ~4 KB
PAUSE = 0.3         # openFDA allows 240 requests/minute without a key


def resolve(sids):
    """{set_id: [generic names]} for every id, in batches.

    One request per id means 1061 requests and about sixteen minutes. openFDA
    takes an OR of set_ids in a single search, the way fetch-routes.py already
    queries it, which brings the same work down to about fifty requests.

    An id missing from the response resolves to [] - no label. An id in a batch
    that could not be fetched at all is left out of the map entirely, so the
    caller can tell "no label" from "could not ask" and keep the source.
    """
    out = {}
    ids = sorted(sids)
    for n in range(0, len(ids), BATCH):
        chunk = ids[n:n + BATCH]
        terms = " OR ".join('set_id:"%s"' % s for s in chunk)
        url = FDA + "?" + urllib.parse.urlencode({"search": terms, "limit": len(chunk)})
        got = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    got = json.load(r).get("results", [])
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:        # openFDA says 404 when nothing matches
                    got = []
                    break
                if attempt == 2:
                    break
                time.sleep(2 ** attempt)
            except Exception:
                if attempt == 2:
                    break
                time.sleep(2 ** attempt)
        if got is None:
            continue                      # could not ask - leave these unknown
        for sid in chunk:
            out[sid] = []
        for res in got:
            sid = (res.get("set_id") or "").lower()
            if sid in out:
                out[sid] = [g.lower() for g in ((res.get("openfda") or {}).get("generic_name") or [])]
        sys.stderr.write("\r  %d/%d set_ids resolved" % (min(n + BATCH, len(ids)), len(ids)))
        time.sleep(PAUSE)
    sys.stderr.write("\n")
    return out


def stem(n):
    s = re.sub(r"[^a-z]", "", n.lower())
    return s[:9] if len(s) >= 7 else None


def name_matches(name, gens):
    """Does any part of this record's name appear in the label's generic names?

    Matching on `name.split()[0]` was wrong for every combination product: the
    first whitespace word of "sitagliptin/metformin" is the whole thing, slash
    included, which never appears in the label's "sitagliptin and metformin
    hydrochloride". Split on non-letters instead and accept any component of
    four letters or more - a combination only has to recognise one of its own
    ingredients to prove the source is its own.
    """
    parts = [p for p in re.split(r"[^a-z]+", name.lower()) if len(p) >= 4]
    if not parts:
        parts = [re.sub(r"[^a-z]", "", name.lower())]
    return any(p in g.lower() for p in parts for g in gens)


def main():
    fix = "--fix" in sys.argv
    drop_src = "--drop-sources" in sys.argv
    letters = sorted(os.path.basename(f)[0] for f in glob.glob(os.path.join(DATA, "[A-Z].json")))
    everything = {}
    for L in letters:
        for d in json.load(open(os.path.join(DATA, L + ".json"))):
            everything[d["n"]] = L
    stems = {n: stem(n) for n in everything if stem(n)}

    # One pre-pass for every SPL id in the data. Resolving them one at a time is
    # 1061 requests and about sixteen minutes; batched it is under a minute, and
    # the matching below is then pure local work.
    every_sid = set()
    for L in letters:
        for d in json.load(open(os.path.join(DATA, L + ".json"))):
            every_sid.update(set_ids(d))
    sys.stderr.write("resolving %d set_ids against openFDA\n" % len(every_sid))
    resolved = resolve(every_sid)
    missing = len(every_sid) - len(resolved)
    if missing:
        sys.stderr.write("  %d could not be fetched - their sources are left alone\n" % missing)

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
                    sure = d["n"] in CONFIRMED
                    flagged.append((d["n"], others[:3], moa[:70], sure))
                    if fix and sure:
                        # from the .js, which was never affected - and cleared
                        # when the .js has none. Never prose of this tool's own.
                        d["moa"] = (live.get(d["n"], {}).get("moa") or "")
                        changed = True
            keep = []
            for src in (d.get("src") or []):
                m = re.search(r"set_id:%22([0-9a-f-]{36})%22|setid=([0-9a-f-]{36})", src.get("url", ""))
                if not m:
                    keep.append(src); continue
                sid = m.group(1) or m.group(2)
                if sid not in resolved:
                    keep.append(src); continue        # could not ask - keep it
                gens = resolved[sid]
                if not gens:
                    # Either no label, or a label carrying no openfda.generic_name
                    # at all - terbutaline has one of those. Neither is evidence
                    # that the source belongs to a different drug.
                    keep.append(src); continue
                if name_matches(d["n"], gens):
                    keep.append(src)
                else:
                    dropped.append((d["n"], sid, gens[0]))
                    changed = True
            # The src check stays report-only. Its two false-positive classes are
            # fixed above, but a heuristic that deletes provenance should be read
            # before it is trusted, and the mechanism repair is what this tool was
            # written for. Pass --drop-sources once the report looks right.
            if fix and drop_src and keep != (d.get("src") or []):
                d["src"] = keep
        if fix and changed:
            open(path, "w").write(json.dumps(rows, ensure_ascii=False))

    sure = [f for f in flagged if f[3]]
    print("mechanisms naming another drug: %d flagged, %d confirmed" % (len(flagged), len(sure)))
    for n, others, m, ok in flagged:
        print("   %s %-30s names %s" % ("FIX " if ok else "read", n[:30], others))
        print("        %s..." % m)
    print()
    print("SPL ids that do not resolve to this drug: %d%s" % (
        len(dropped), "" if drop_src else "   (report only - pass --drop-sources to act)"))
    for n, sid, got in dropped:
        print("   %-30s %s -> %s" % (n[:30], sid[:8], got[:36]))
    if not fix:
        print("\n(report only - pass --fix to repair)")
    return 1 if (flagged or dropped) and not fix else 0


if __name__ == "__main__":
    sys.exit(main())
