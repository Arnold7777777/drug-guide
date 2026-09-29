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


def clean(text, limit=900):
    """SPL sections arrive as lists of long strings with markup and runs of space."""
    if isinstance(text, list):
        text = " ".join(text)
    if not text:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"\s+", " ", text).strip()
    # SPL sections open with their own number and heading - "12.1 Mechanism of
    # Action ...", "4 CONTRAINDICATIONS ...". The existing records carry the prose
    # without it, so drop it and keep the sentence.
    text = re.sub(r"^\d+(\.\d+)*\s+", "", text)
    text = re.sub(r"^(MECHANISM OF ACTION|INDICATIONS AND USAGE|CONTRAINDICATIONS|"
                  r"ADVERSE REACTIONS|WARNINGS AND PRECAUTIONS|PATIENT COUNSELING INFORMATION|"
                  r"Mechanism of Action|Indications and Usage|Patient Counseling Information)\s+",
                  "", text)
    # Labels repeat themselves: the Highlights block and the full section both
    # come back, so the same sentence lands twice. Keep first occurrences only.
    parts, seen, out = re.split(r"(?<=\.)\s+", text), set(), []
    for sent in parts:
        key = re.sub(r"[^a-z0-9]", "", sent.lower())[:60]
        if key and key in seen:
            continue
        seen.add(key)
        out.append(sent)
    text = " ".join(out)
    # standard SPL filler: the clinical-trial caveat and the trial demographics
    text = re.sub(r"\b(Clinical Trial Experience\s+)?Because clinical trials are conducted under "
                  r"widely varying conditions.*?(?=[A-Z][a-z])", "", text, flags=re.S)
    # MedWatch boilerplate and section cross-references add nothing to a study card
    text = re.sub(r"To report SUSPECTED ADVERSE REACTIONS.*?medwatch\.?", "", text, flags=re.I)
    text = re.sub(r"\(\s*\d+(\.\d+)?\s*\)", "", text)
    text = re.sub(r"(?<=[.:])\s+\d+\.\d+\s+(?=[A-Z])", " ", text)   # bare "6.1" between sentences
    text = re.sub(r"\[see [^\]]{0,80}\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    # every field in the existing data stops by about 900 characters; cut on a
    # sentence if there is one nearby, otherwise on a word, never mid-word
    if len(text) > limit:
        cut = text.rfind(". ", limit - 220, limit)
        if cut > 0:
            text = text[:cut + 1]
        else:
            text = text[:text.rfind(" ", 0, limit)].rstrip(" ,;") + "\u2026"
    return text.strip()


# Store-brand and descriptor products crowd out the brand a nurse would
# recognise: a famotidine search returns "Leader Acid Reducer" and
# "CareOne Acid Relief" alongside Pepcid AC. Drop anything that is a plain
# description of the drug's effect rather than a brand.
DESCRIPTOR = re.compile(
    r"acid (reducer|relief|controller)|heartburn|antacid|"
    r"\b(basic care|foster and thrive|equate|good ?sense|sunmark|care ?one|leader|"
    r"health ?mart|topcare|rite aid|kirkland|member'?s mark|up ?and ?up|signature care|"
    r"quality choice|premier value|berkley|calmicid)\b", re.I)


def brands_for(name, of):
    """Real brand names, gathered across every label for this generic.

    One label usually lists the generic as its own brand, so a single fetch
    gives "Famotidine" and misses Pepcid AC. Scan the whole result set, drop
    the generic itself and the store-brand descriptors, and keep the rest.
    """
    found = {}
    try:
        res = get(FDA + "?" + urllib.parse.urlencode(
            {"search": 'openfda.generic_name:"%s"' % name, "limit": 100})).get("results", [])
    except Exception:
        res = [{"openfda": of}]
    for r in res:
        o = r.get("openfda") or {}
        gens = [g.strip().lower() for g in (o.get("generic_name") or [])]
        if name.lower() not in gens:
            continue
        for b in (o.get("brand_name") or []):
            b = b.strip()
            k = b.lower()
            if not b or k == name.lower() or DESCRIPTOR.search(b):
                continue
            found.setdefault(k, b)          # first spelling wins
    # shortest first: "Pepcid AC" ahead of "Maximum Strength PEPCID AC Icy Cool Mint"
    return sorted(found.values(), key=lambda x: (len(x), x.lower()))[:6]


def classes_for(rxcui):
    """cls and cc from RxClass, the same source the other records cite."""
    if not rxcui:
        return "", ""
    try:
        d = get("%s/rxclass/class/byRxcui.json?rxcui=%s" % (RXNAV, rxcui))
    except Exception:
        return "", ""
    atc, epc, chem = [], [], []
    for it in (d.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo", []):
        c = it.get("rxclassMinConceptItem") or {}
        name, typ = c.get("className", ""), c.get("classType", "")
        if typ == "ATC1-4":
            atc.append(name)
        elif typ == "EPC":
            epc.append(name)
        elif typ == "CHEM":
            chem.append(name)
    # an rxcui shared with a combination product drags in the other drug's ATC
    # class, so prefer the ATC class that echoes the FDA establishment class
    best = ""
    for a in atc:
        if any(w.lower() in a.lower() for e in epc for w in re.findall(r"[A-Za-z0-9-]{4,}", e)):
            best = a
            break
    cls = best or (atc[0] if atc else "")
    cc = ""
    return cls, cc


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
    cls, cc = classes_for(rxcui)
    epc_fc = ""
    for e in (of.get("pharm_class_epc") or []):
        epc_fc = re.sub(r"\s*\[EPC\]$", "", e).strip()
        break
    brands = brands_for(name, of)
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
        "cls": cls,
        "fc": fc_override or epc_fc,
        "cc": cc,
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
    print("  cls   %s" % (entry["cls"] or "(RxClass gave none)"))
    print("  fc    %s" % (entry["fc"] or "(label gave none)"))
    for k in ("moa", "ind", "ci", "bbw", "ae", "teach"):
        print("  %-5s %s" % (k, (entry[k][:90] + "...") if entry[k] else "(label carries none)"))
    if not entry["src"]:
        sys.exit("no citable source resolved - refusing to write an unsourced entry")

    # i is an identifier, not a sort key - the pages sort by name and look drugs
    # up by name. So give the new drug an unused i and renumber nothing: a global
    # renumber would rewrite 979 records to add one, and would push data/*.json
    # back out of step with the data/*.js the site actually loads.
    files = {p.stem: (drugs if p.stem == letter else json.loads(p.read_text()))
             for p in sorted(DATA.glob("[A-Z].json"))}
    used = {d.get("i") for arr in files.values() for d in arr if isinstance(d.get("i"), int)}
    entry["i"] = max(used) + 1
    drugs.append(entry)
    drugs.sort(key=lambda d: d.get("n", "").lower())
    print("%s added at i=%d (%d entries, none renumbered)"
          % (name, entry["i"], sum(len(v) for v in files.values())))

    if dry:
        print("--dry-run: nothing written")
        return

    # Only the letter that gained a drug is touched, and its .js is edited rather
    # than regenerated: the .js carries pron, dnc, ncls, od, when and wtag, which
    # the .json has no column for, so rebuilding it from the .json would delete
    # every pronunciation and timing tag in that letter.
    (DATA / (letter + ".json")).write_text(json.dumps(drugs, ensure_ascii=False))
    jsp = DATA / (letter + ".js")
    raw = jsp.read_text()
    live = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
    live.append({k: entry[k] for k in ("i", "n", "alias", "brands", "rx", "ha", "cls", "moa", "ind", "ci", "bbw", "ae", "teach")})
    live.sort(key=lambda d: d.get("n", "").lower())
    jsp.write_text('DG.put("%s",%s);' % (letter, json.dumps(live, ensure_ascii=False)))

    idxp = DATA / "index.js"
    raw = idxp.read_text()
    idx = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
    idx.append({"i": entry["i"], "n": name, "a": entry.get("alias", []),
                "b": entry.get("brands", []), "fc": entry.get("fc", ""),
                "cc": entry.get("cc", ""), "ha": 0,
                "bbw": 1 if entry.get("bbw") else 0, "k": letter, "od": 0})
    idx.sort(key=lambda d: d.get("n", "").lower())
    idxp.write_text('DG.put("index",%s);' % json.dumps(idx, ensure_ascii=False))
    (DATA / "index.json").write_text(json.dumps(idx, ensure_ascii=False))
    print("wrote data/%s.json, data/%s.js and the index (%d rows)" % (letter, letter, len(idx)))

    prov = json.loads((DATA / "provenance.json").read_text())
    prov["drugs"] = len(idx)
    prov.setdefault("added", []).append(
        {"n": name, "on": entry["read"], "rxcui": entry["rxcui"],
         "via": "tools/add-drug.py, openFDA SPL + RxNav"})
    (DATA / "provenance.json").write_text(json.dumps(prov, indent=1, ensure_ascii=False))
    print("provenance.json updated")


if __name__ == "__main__":
    main()
