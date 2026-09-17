#!/usr/bin/env python3
"""Fill the `rt` (route of administration) field on every drug from the FDA label.

Source: openFDA drug label API -> results[].openfda.route, which is the route
recorded on the product's Structured Product Labeling. Public domain, and the
same source data/*.json already cites per drug in its `src` list.

It matches drugs to labels by the SPL set_id already stored in data/*.json, so
nothing is guessed and nothing is looked up by name. A drug whose label carries
no route is left without one rather than filled in from somewhere else.

Needs outbound access to api.fda.gov. In a cloud session that means the
environment's Network access is Custom with api.fda.gov in Allowed domains.

    python3 tools/fetch-routes.py            # fetch and write
    python3 tools/fetch-routes.py --dry-run  # fetch and report, write nothing
"""
import json, re, sys, time, urllib.parse, urllib.request, pathlib, collections

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
API  = "https://api.fda.gov/drug/label.json"
BATCH = 20          # set_ids per query; the URL has to stay under ~4 KB
PAUSE = 0.3         # openFDA allows 240 requests/minute without a key

# openFDA writes routes in SPL vocabulary; these are the forms a nursing
# student reads. Anything unmapped is title-cased and kept as-is.
PRETTY = {
    "ORAL": "PO", "INTRAVENOUS": "IV", "INTRAMUSCULAR": "IM",
    "SUBCUTANEOUS": "Subcut", "SUBLINGUAL": "SL", "BUCCAL": "Buccal",
    "TOPICAL": "Topical", "TRANSDERMAL": "Transdermal", "RECTAL": "Rectal",
    "VAGINAL": "Vaginal", "OPHTHALMIC": "Ophthalmic", "AURICULAR (OTIC)": "Otic",
    "NASAL": "Nasal", "RESPIRATORY (INHALATION)": "Inhaled",
    "INTRARESPIRATORY": "Inhaled", "INTRATHECAL": "Intrathecal",
    "EPIDURAL": "Epidural", "INTRADERMAL": "Intradermal",
    "INTRAOSSEOUS": "IO", "INTRA-ARTICULAR": "Intra-articular",
    "INTRAVESICAL": "Intravesical", "INTRAPERITONEAL": "Intraperitoneal",
    "PERCUTANEOUS": "Percutaneous", "PARENTERAL": "Parenteral",
    "INFILTRATION": "Infiltration", "INTRAVITREAL": "Intravitreal",
}
ORDER = ["PO", "SL", "Buccal", "IV", "IM", "Subcut", "Intradermal", "IO",
         "Inhaled", "Topical", "Transdermal", "Ophthalmic", "Otic", "Nasal",
         "Rectal", "Vaginal", "Intrathecal", "Epidural"]

def pretty(raw):
    return PRETTY.get(raw.upper().strip(), raw.strip().title())

def sort_routes(rs):
    return sorted(set(rs), key=lambda r: (ORDER.index(r) if r in ORDER else 99, r))

def set_id_of(drug):
    for s in drug.get("src") or []:
        m = re.search(r"set_id:%22([0-9a-f-]{36})%22|setid=([0-9a-f-]{36})", s.get("url", ""))
        if m:
            return m.group(1) or m.group(2)
    return None

def fetch(set_ids):
    """Return {set_id: [raw routes]} for one batch."""
    terms = " OR ".join('set_id:"%s"' % s for s in set_ids)
    url = API + "?" + urllib.parse.urlencode({"search": terms, "limit": len(set_ids)})
    with urllib.request.urlopen(url, timeout=45) as r:
        payload = json.load(r)
    out = {}
    for res in payload.get("results", []):
        sid = (res.get("set_id") or "").lower()
        routes = (res.get("openfda") or {}).get("route") or []
        if sid and routes:
            out[sid] = routes
    return out

def main():
    dry = "--dry-run" in sys.argv
    want = {}                       # set_id -> [(letter, index)]
    letters = {}
    for path in sorted(DATA.glob("[A-Z].json")):
        drugs = json.loads(path.read_text())
        letters[path.stem] = drugs
        for i, d in enumerate(drugs):
            sid = set_id_of(d)
            if sid:
                want.setdefault(sid.lower(), []).append((path.stem, i))

    ids = sorted(want)
    print("drugs with an SPL set_id: %d of %d"
          % (sum(len(v) for v in want.values()), sum(len(v) for v in letters.values())))

    found = {}
    for n in range(0, len(ids), BATCH):
        chunk = ids[n:n + BATCH]
        for attempt in range(3):
            try:
                found.update(fetch(chunk)); break
            except Exception as e:
                if attempt == 2:
                    print("  batch %d failed: %s" % (n // BATCH, e), file=sys.stderr)
                else:
                    time.sleep(2 ** attempt)
        print("\r  %d/%d set_ids queried, %d with a route"
              % (min(n + BATCH, len(ids)), len(ids), len(found)), end="", file=sys.stderr)
        time.sleep(PAUSE)
    print(file=sys.stderr)

    tally = collections.Counter()
    filled = 0
    for sid, routes in found.items():
        rs = sort_routes(pretty(r) for r in routes)
        for letter, i in want[sid]:
            letters[letter][i]["rt"] = rs
            filled += 1
            for r in rs:
                tally[r] += 1

    total = sum(len(v) for v in letters.values())
    print("routes written: %d of %d drugs (%.0f%%)" % (filled, total, 100.0 * filled / total))
    print("by route:", tally.most_common())

    if dry:
        print("--dry-run: nothing written"); return

    # data/<L>.json, data/<L>.js  (the site loads the .js; the .json is the source)
    for letter, drugs in letters.items():
        by_i = {d.get("i"): d.get("rt") for d in drugs if d.get("rt")}
        (DATA / (letter + ".json")).write_text(json.dumps(drugs, ensure_ascii=False))
        jsp = DATA / (letter + ".js")
        raw = jsp.read_text()
        arr = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
        for d in arr:
            rt = by_i.get(d.get("i"))
            if rt:
                d["rt"] = rt
        jsp.write_text('DG.put("%s",%s);\n' % (letter, json.dumps(arr, ensure_ascii=False)))

    # index.js / index.json carry the fields the search page filters on
    idx = json.loads((DATA / "index.json").read_text())
    lookup = {(d["k"] if isinstance(d.get("k"), str) else None, d.get("i")): None for d in idx}
    rt_by_key = {}
    for letter, drugs in letters.items():
        for d in drugs:
            if d.get("rt"):
                rt_by_key[(letter, str(d.get("i")))] = d["rt"]
    n_idx = 0
    for row in idx:
        rt = rt_by_key.get((row.get("k"), str(row.get("i"))))
        if rt:
            row["rt"] = rt; n_idx += 1
    (DATA / "index.json").write_text(json.dumps(idx, ensure_ascii=False))
    (DATA / "index.js").write_text('DG.put("index",%s);\n' % json.dumps(idx, ensure_ascii=False))
    print("index rows tagged:", n_idx)

    prov = json.loads((DATA / "provenance.json").read_text())
    prov["routes"] = {
        "field": "rt",
        "built": time.strftime("%Y-%m-%d"),
        "source": "openFDA drug label, results[].openfda.route, matched by SPL set_id",
        "url": "https://open.fda.gov/apis/drug/label/",
        "coverage": "%d of %d drugs" % (filled, total),
        "note": "A drug with no route here had none on its FDA label; it was not filled in from any other source.",
    }
    (DATA / "provenance.json").write_text(json.dumps(prov, indent=1, ensure_ascii=False))
    print("provenance.json updated")

if __name__ == "__main__":
    main()
