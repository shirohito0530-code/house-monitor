#!/usr/bin/env python3
"""Read-only audit of station-walk extraction and persistence."""
from __future__ import annotations
import json, re, unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"data"
WALK_FIELDS=("targetStationWalkMinutes","stationWalkMinutes","walkMinutes")
RAW_FIELDS=("transportRaw","stationAccessRaw","accessRaw","access","transport")
SOURCE_FIELDS=("targetStationWalkSource","stationWalkSource","walkSource")
AVAIL_FIELDS=("targetStationWalkAvailable","stationWalkAvailable","walkAvailable")
DETAILS=("detail","lastSuccessfulDetail","detailContent","detailData","detailResult")
EMPTY={"","-","－","不明","未定","なし","N/A","n/a"}
ID_RE=re.compile(r"/(nc_\d+)(?:/|$|[?#])",re.I)
def load_rows(filename):
    p=DATA/filename
    if not p.exists(): raise FileNotFoundError(p)
    obj=json.loads(p.read_text(encoding="utf-8"))
    rows=obj if isinstance(obj,list) else obj.get("properties",[]) if isinstance(obj,dict) else None
    if not isinstance(rows,list): raise ValueError(f"{filename}: unsupported structure")
    return [x for x in rows if isinstance(x,dict)]
def usable(v):
    if v is None:return False
    if isinstance(v,str):return v.strip() not in EMPTY
    if isinstance(v,(dict,list)):return bool(v)
    return True
def parse_walk(v):
    if v is None or isinstance(v,bool):return None
    if isinstance(v,(int,float)): n=float(v)
    else:
        m=re.search(r"(?<!\d)(\d+(?:\.\d+)?)",unicodedata.normalize("NFKC",str(v)).replace(",",""))
        if not m:return None
        n=float(m.group(1))
    return n if 1<=n<=120 else None
def norm(v):return unicodedata.normalize("NFKC",str(v)).strip().lower() if v is not None else ""
def suumo_id(v):
    m=ID_RE.search(unicodedata.normalize("NFKC",str(v))) if v else None
    return m.group(1).lower() if m else None
def identity_keys(row):
    out=[]
    for f in ("sourceUrl","detailUrl","url","canonicalUrl"):
        x=suumo_id(row.get(f))
        if x:out.append(("suumo_listing_id",x))
    src=norm(row.get("source") or "suumo")
    for f in ("sourceId","listingId"):
        v=norm(row.get(f))
        if v:out.append((f"{src}:{f}",v))
    v=norm(row.get("propertyId") or row.get("id"))
    if v:out.append(("property_id",v))
    return list(dict.fromkeys(out))
def primary_id(row):
    k=identity_keys(row)
    return f"{k[0][0]}={k[0][1]}" if k else "(unknown)"
def objects(row):return [("record",row)]+[(k,row[k]) for k in DETAILS if isinstance(row.get(k),dict)]
def first(row,fields):
    for name,obj in objects(row):
        for f in fields:
            v=obj.get(f)
            if usable(v):return f"{name}.{f}",v
    return None,None
def inspect_row(row):
    walks=[]
    for name,obj in objects(row):
        for f in WALK_FIELDS:
            n=parse_walk(obj.get(f))
            if n is not None:walks.append((f"{name}.{f}",obj.get(f),n))
    wp,raww,w=(walks[0] if walks else (None,None,None))
    rp,raw=first(row,RAW_FIELDS);sp,source=first(row,SOURCE_FIELDS)
    av=[{"path":f"{name}.{f}","value":obj.get(f)} for name,obj in objects(row) for f in AVAIL_FIELDS if f in obj]
    false=bool(av) and all(x["value"] is False for x in av)
    true=any(x["value"] is True for x in av)
    if walks:cat="walk_value_but_unavailable_flag" if false else "walk_value_without_availability_flag" if not av else "walk_value_found"
    elif usable(raw):cat="raw_transport_without_walk"
    elif true:cat="available_flag_without_walk"
    else:cat="no_walk_evidence"
    stationpath,station=first(row,("targetStation","station"))
    return {"id":primary_id(row),"identityKeys":identity_keys(row),"walkMinutes":w,"walkPath":wp,
      "rawWalkValue":raww,"allWalkValues":[{"path":p,"raw":r,"minutes":n} for p,r,n in walks],
      "rawTransportPath":rp,"transportRaw":raw,"walkSourcePath":sp,"targetStationWalkSource":source,
      "stationPath":stationpath,"targetStation":station,"availabilityValues":av,"category":cat,
      "propertyId":row.get("propertyId"),"sourceId":row.get("sourceId"),"listingId":row.get("listingId"),
      "sourceUrl":row.get("sourceUrl") or row.get("detailUrl") or row.get("url"),
      "detailFetched":row.get("detailFetched"),"detailFetchStatus":row.get("detailFetchStatus"),
      "detailDataVersion":row.get("detailDataVersion")}
def index_rows(rows):
    ix=defaultdict(list)
    for i,r in enumerate(rows):
        for k in identity_keys(r):ix[k].append(i)
    return ix
def match(row,ix):
    for k in identity_keys(row):
        hits=ix.get(k,[])
        if len(hits)==1:return hits[0],"unique"
        if len(hits)>1:return hits,"ambiguous"
    return [], "unmatched"
def summarize(rows):
    audits=[inspect_row(r) for r in rows];counts=Counter(a["category"] for a in audits);n=len(rows)
    cats=("walk_value_but_unavailable_flag","walk_value_without_availability_flag","raw_transport_without_walk","available_flag_without_walk","no_walk_evidence")
    return {"total":n,"validWalkCount":sum(a["walkMinutes"] is not None for a in audits),
      "validWalkPct":round(100*sum(a["walkMinutes"] is not None for a in audits)/n,2) if n else 0,
      "categoryCounts":dict(counts),"examples":{c:[a for a in audits if a["category"]==c][:15] for c in cats}}
def compare(discovered,houses):
    ix=index_rows(discovered);pairs=[];un=[];amb=[]
    for i,h in enumerate(houses):
        m,status=match(h,ix)
        if status=="unique":pairs.append((i,m))
        elif status=="ambiguous":amb.append(primary_id(h))
        else:un.append(primary_id(h))
    dmissing=[];hmissing=[];conf=[]
    for hi,di in pairs:
        ha,da=inspect_row(houses[hi]),inspect_row(discovered[di]);hw,dw=ha["walkMinutes"],da["walkMinutes"]
        if dw is not None and hw is None:dmissing.append({"id":primary_id(houses[hi]),"discoveredWalk":dw,"discoveredPath":da["walkPath"],"houseDetailFetched":houses[hi].get("detailFetched"),"houseStatus":houses[hi].get("status")})
        elif hw is not None and dw is None:hmissing.append({"id":primary_id(houses[hi]),"housesWalk":hw,"housesPath":ha["walkPath"]})
        elif hw is not None and dw is not None and hw!=dw:conf.append({"id":primary_id(houses[hi]),"housesWalk":hw,"discoveredWalk":dw})
    return {"matchedUniquePairCount":len(pairs),"unmatchedHouseCount":len(un),"ambiguousHouseCount":len(amb),
      "unmatchedHouseExamples":un[:20],"ambiguousHouseExamples":amb[:20],
      "discoveredHasWalkButHousesMissing":dmissing,"housesHasWalkButDiscoveredMissing":hmissing,
      "walkValueConflicts":conf,"discoveredIdentityKeyCount":len(ix),"housesIdentityKeyCount":len(index_rows(houses))}
def main():
    d=load_rows("discovered_listings.json");h=load_rows("houses.json");ds=summarize(d);hs=summarize(h);cmp=compare(d,h)
    report={"readOnly":True,"discovered":ds,"houses":hs,"crossFileComparison":cmp,
      "interpretation":["Diagnostic only; no DB mutation.","Missing walk does not prove source page lacks access info.",
      "Ambiguous identity matches are not successful matches.","Building footprint and total floor area are distinct."]}
    print("=== Station Walk Audit (READ ONLY) ===")
    for name,x in (("discovered_listings.json",ds),("houses.json",hs)):
        print(f"\n{name}\n  total: {x['total']}\n  valid walk: {x['validWalkCount']}/{x['total']} ({x['validWalkPct']}%)\n  categories: {x['categoryCounts']}")
    print("\n=== Cross-file comparison ===\n"+json.dumps(cmp,ensure_ascii=False,indent=2))
    print("\n=== Representative examples ===\n"+json.dumps({"discovered":ds["examples"],"houses":hs["examples"]},ensure_ascii=False,indent=2))
    print("\n=== Full JSON report ===\n"+json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__":main()
