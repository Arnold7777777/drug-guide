#!/usr/bin/env python3
"""Add one drug to the guide from its FDA label, and renumber cleanly.

Written because famotidine was missing. The only entry carrying the word was
`ibuprofen/famotidine` (Duexis) filed under I, so searching for the drug a
nursing student actually meets returned a combination NSAID product instead.
Of the H2 blockers the guide held only nizatidine.

Nothing here is written from memory. The entry is built from the same two
public sources every other record cites:

  * RxNav      - resolves the generic name to an RxCUI
  * openFDA    - the Structured Product Label: indications, contraindications,
                 warnings, boxed warning, adverse reactions, counselling text,
                 mechanism of action, brand names

A field the label does not carry is left empty rather than filled in from
somewhere else, and if the name cannot be resolved the script writes nothing
at all.

Needs outbound access to rxnav.nlm.nih.gov and api.fda.gov. In a cloud session
that means the environment's Network access is Custom with both in Allowed
domains - the same requirement fetch-routes.py has for api.fda.gov.

    python3 tools/add-drug.py famotidine              # fetch, insert, write
    python3 tools/add-drug.py famotidine --dry-run    # fetch and report only
    python3 tools/add-drug.py famotidine --fc "H2-receptor antagonist"
"""
import json, re, sys, urllib.parse, urllib.request, pathlib, html, time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RXNAV = "https://rxnav.nlm.nih.gov/REST"
FDA = "https://api.fda.gov/drug/label.json"

# the key order every existing record uses, so a diff stays readable
KEYS = ["n", "alias", "brands", "rx", "ha", "cls", "fc", "cc", "moa", "ind",
        "ci", "warn", "bbw", "ae", "teach", "rxcui", "src", "read", "i"]


class Unreachable(Exception):
    pass


def get(url, timeout=45):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError:
        raise
    except Exception as e:
        raise Unreachable(url) from e


def clean(text, limit=4000):
    """SPL sections arrive as lists of long strings with markup and runs of space."""
    if isinstance(text, list):
        text = " ".join(text)
    if not text:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    # labels repeat the section heading in caps; keep it, it reads as a lead-in
    return text[:limit].strip()


def rxcui_for(name):
    d = get("%s/rxcui.json?%s" % (RXNAV, urllib.parse.urlencode({"name": name})))
    ids = ((d.get("idGroup") or {}).get("rxnormId")) or []
    return ids[0] if ids else None


def label_for(name):
    """Prefer an exact generic-name match; fall back to a general search."""
    for term in ('openfda.generic_name:"%s"' % name, '"%s"' % name):
        url = FDA + "?" + urllib.parse.urlencode({"search": term, "limit": 5})
        try:
            res = get(url).get("results", [])
        except Exception:
            res = []
        for r in res:
            of = r.get("openfda") or {}
            names = [x.lower() for x in (of.get("generic_name") or [])]
            if any(name.lower() in x for x in names):
                return r
        if res:
            return res[0]
    return None


def build(name, fc_override=None):
    rxcui = rxcui_for(name)
    lab = label_for(name)
    if lab is None:
        sys.exit("no FDA label found for %r - nothing written" % name)
    of = lab.get("openfda") or {}
    sid = (lab.get("set_id") or "").lower()
    brands = sorted({b for b in (of.get("brand_name") or [])})
    rx = "Rx"
    if any("OTC" in (t or "").upper() for t in (of.get("product_type") or [])):
        rx = "OTC"

    src = []
    if sid:
        src += [
            {"name": "openFDA drug label (SPL %s)" % sid,
             "url": FDA + "?search=set_id:%%22%s%%22" % sid},
            {"name": "DailyMed SPL %s" % sid,
             "url": "https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid=%s" % sid},
        ]
    if rxcui:
        src += [
            {"name": "RxNorm", "url": "%s/rxcui/%s/properties.json" % (RXNAV, rxcui)},
            {"name": "RxClass ATC",
             "url": "%s/rxclass/class/byRxcui.json?rxcui=%s" % (RXNAV, rxcui)},
        ]

    return {
        "n": name,
        "alias": [],
        "brands": brands,
        "rx": rx,
        "ha": 0,
        "cls": "",
        "fc": fc_override or "",
        "cc": "",
        "moa": clean(lab.get("mechanism_of_action") or lab.get("clinical_pharmacology")),
        "ind": clean(lab.get("indications_and_usage")),
        "ci": clean(lab.get("contraindications")),
        "warn": clean(lab.get("warnings_and_cautions") or lab.get("warnings")),
        "bbw": clean(lab.get("boxed_warning")),
        "ae": clean(lab.get("adverse_reactions")),
        "teach": clean(lab.get("information_for_patients") or lab.get("spl_patient_package_insert")),
        "rxcui": rxcui or "",
        "src": src,
        "read": time.strftime("%Y-%m-%d"),
        "i": None,                      # assigned during the renumber below
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    name = args[0]
    dry = "--dry-run" in sys.argv
    fc = None
    if "--fc" in sys.argv:
        fc = sys.argv[sys.argv.index("--fc") + 1]

    letter = name[0].upper()
    path = DATA / (letter + ".json")
    if not path.exists():
        sys.exit("no data file for letter %r" % letter)
    drugs = json.loads(path.read_text())
    if any(d.get("n", "").lower() == name.lower() for d in drugs):
        sys.exit("%s is already in data/%s.json - nothing to do" % (name, letter))

    try:
        entry = build(name, fc)
    except Unreachable as e:
        host = urllib.parse.urlparse(str(e)).netloc
        sys.exit(
            "cannot reach %s - nothing written.\n"
            "This script builds the entry from the drug's FDA label, so it needs\n"
            "outbound access to both rxnav.nlm.nih.gov and api.fda.gov. In a cloud\n"
            "session set the environment's Network access to Custom and add both to\n"
            "Allowed domains, then run this again." % (host or "the data sources"))
    print("built %s  rxcui=%s  brands=%s" % (name, entry["rxcui"] or "-", ", ".join(entry["brands"][:4]) or "-"))
    for k in ("moa", "ind", "ci", "bbw", "ae", "teach"):
        print("  %-5s %s" % (k, (entry[k][:90] + "...") if entry[k] else "(label carries none)"))
    if not entry["src"]:
        sys.exit("no citable source resolved - refusing to write an unsourced entry")

    # insert alphabetically, then renumber i across every letter file
    drugs.append(entry)
    drugs.sort(key=lambda d: d.get("n", "").lower())
    files = {p.stem: (drugs if p.stem == letter else json.loads(p.read_text()))
             for p in sorted(DATA.glob("[A-Z].json"))}
    n = 0
    for L in sorted(files):
        for d in files[L]:
            d["i"] = n
            n += 1
    print("renumbered %d entries; %s lands at i=%d"
          % (n, name, next(d["i"] for d in drugs if d["n"] == name)))

    if dry:
        print("--dry-run: nothing written")
        return

    for L, arr in files.items():
        (DATA / (L + ".json")).write_text(json.dumps(arr, ensure_ascii=False))
        (DATA / (L + ".js")).write_text('DG.put("%s",%s);\n' % (L, json.dumps(arr, ensure_ascii=False)))

    idx = []
    for L in sorted(files):
        for d in files[L]:
            idx.append({"i": d["i"], "n": d["n"], "a": d.get("alias", []),
                        "b": d.get("brands", []), "fc": d.get("fc", ""),
                        "cc": d.get("cc", ""), "ha": d.get("ha", 0),
                        "bbw": 1 if d.get("bbw") else 0, "k": L, "od": 0})
    (DATA / "index.json").write_text(json.dumps(idx, ensure_ascii=False))
    (DATA / "index.js").write_text('DG.put("index",%s);\n' % json.dumps(idx, ensure_ascii=False))
    print("wrote %d letter files and the index (%d rows)" % (len(files), len(idx)))

    prov = json.loads((DATA / "provenance.json").read_text())
    prov["drugs"] = len(idx)
    prov.setdefault("added", []).append(
        {"n": name, "on": entry["read"], "rxcui": entry["rxcui"],
         "via": "tools/add-drug.py, openFDA SPL + RxNav"})
    (DATA / "provenance.json").write_text(json.dumps(prov, indent=1, ensure_ascii=False))
    print("provenance.json updated")


if __name__ == "__main__":
    main()
