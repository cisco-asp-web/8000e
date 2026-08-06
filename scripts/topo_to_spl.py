#!/usr/bin/env python3
"""
topo_to_spl.py -- containerlab topology -> Splunk Network Graph SPL

Reads topology.clab.yaml and emits an SPL `makeresults` query that renders
the lab in the Dashboard Studio "Network Graph" visualization, following the
mvexpand/split idiom from Splunk's docs:

  https://help.splunk.com/.../visualizations/network-graph

Model (from that doc):
  * one result row per link; source/target are the two endpoints.
  * node styling columns (nodeColors, nodeIcons, ...) describe the row's
    *source* node. A node is styled wherever it appears as a source.
  * to guarantee every node is styled -- including ones that would only ever
    be a link *target* -- we emit a leading "definition" row per node
    (source=<node>, target="") carrying that node's canonical styling, then
    the link rows after. Definition rows first means the target[] array never
    ends on an empty value (split() can drop a trailing empty).

By default parallel fabric links between the same SAR pair are collapsed into
one edge whose linkValues/linkWidths encode the member count (the ECMP bundle
size). Pass --expand to draw every physical link separately.

Usage:
  ./topo_to_spl.py [--topo topology.clab.yaml] [--expand] [-o out.spl]
"""

import argparse
import os
from collections import OrderedDict

import yaml

# ---- role styling -----------------------------------------------------------
# icons kept to names proven in the Splunk doc example (servers / portrait);
# roles are distinguished by colour so an unknown icon name can't blank a node.
ROLE_STYLE = {
    "sar": {"color": "#00BCEB", "icon": "servers",  "icon_color": "#FFFFFF", "value": "8"},
    "rr":  {"color": "#8E44AD", "icon": "servers",  "icon_color": "#FFFFFF", "value": "6"},
    "trn": {"color": "#6CC04A", "icon": "portrait", "icon_color": "#FFFFFF", "value": "4"},
    "inf": {"color": "#F2A900", "icon": "portrait", "icon_color": "#FFFFFF", "value": "4"},
}

# link colouring by category
LINK_STYLE = {
    "fabric": "#00BCEB",   # SAR <-> SAR scale-across bundle
    "host":   "#9E9E9E",   # host <-> SAR access link
    "rr":     "#8E44AD",   # SAR <-> route-reflector
}


def classify(name):
    """Return the role key for a node name."""
    if name.endswith("-trn"):
        return "trn"
    if name.endswith("-inf"):
        return "inf"
    if "xrd-rr" in name:
        return "rr"
    if "sar" in name:
        return "sar"
    return "sar"  # default; topology has no others


def data_center(name):
    """Cluster label. DC0 side = dc00-*/dc0-*/RRs; DC1 side = dc01-*/dc1-*."""
    if name.startswith("dc01") or name.startswith("dc1-"):
        return "DC1"
    return "DC0"  # dc00-*, dc0-*, and the p0x-xrd-rr reflectors


def link_category(a, b, roles):
    ra, rb = roles[a], roles[b]
    if "rr" in (ra, rb):
        return "rr"
    if "trn" in (ra, rb) or "inf" in (ra, rb):
        return "host"
    return "fabric"


def load_topology(path):
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    node_names = list(doc["topology"]["nodes"].keys())
    links = []
    for link in doc["topology"].get("links", []):
        a = link["endpoints"][0].split(":", 1)[0]
        b = link["endpoints"][1].split(":", 1)[0]
        links.append((a, b))
    return node_names, links


def build_edges(links, roles, collapse):
    """Return ordered list of (src, dst, count, category)."""
    if not collapse:
        return [(a, b, 1, link_category(a, b, roles)) for a, b in links]

    counts = OrderedDict()
    for a, b in links:
        key = tuple(sorted((a, b)))
        counts[key] = counts.get(key, 0) + 1
    return [(a, b, n, link_category(a, b, roles)) for (a, b), n in counts.items()]


def build_rows(node_names, edges, roles):
    """
    Return a list of column-dicts, one per SPL row.
    Definition rows (empty target) first, then edge rows.
    """
    rows = []

    # --- node definition rows: guarantee every node gets styled -------------
    for n in node_names:
        st = ROLE_STYLE[roles[n]]
        rows.append({
            "source": n, "target": "",
            "nodeTexts": n, "type": data_center(n),
            "nodeColors": st["color"], "nodeIcons": st["icon"],
            "nodeIconColors": st["icon_color"], "nodeValues": st["value"],
            "linkColors": "", "linkValues": "", "linkWidths": "",
        })

    # --- link rows: styling columns describe the source node ----------------
    for src, dst, count, cat in edges:
        st = ROLE_STYLE[roles[src]]
        rows.append({
            "source": src, "target": dst,
            "nodeTexts": src, "type": data_center(src),
            "nodeColors": st["color"], "nodeIcons": st["icon"],
            "nodeIconColors": st["icon_color"], "nodeValues": st["value"],
            "linkColors": LINK_STYLE[cat],
            "linkValues": str(count),
            "linkWidths": "6" if count > 1 else "2",
        })
    return rows


# column order in the emitted eval / table
COLUMNS = ["source", "target", "nodeTexts", "type", "nodeColors", "nodeIcons",
           "nodeIconColors", "nodeValues", "linkColors", "linkValues", "linkWidths"]


def emit_spl(rows):
    n = len(rows)
    lines = ["| makeresults", "| eval "]
    evals = []
    for col in COLUMNS:
        joined = ",".join(r[col] for r in rows)
        evals.append(f'    {col}="{joined}"')
    evals.append(f"    row_index=mvrange(0,{n})")
    lines.append(",\n".join(evals))
    lines.append("| mvexpand row_index")
    lines.append("| eval ")
    splits = [f"    {col}=mvindex(split({col},\",\"),row_index)" for col in COLUMNS]
    lines.append(",\n".join(splits))
    lines.append("| table " + ", ".join(COLUMNS))
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topo", default="topology.clab.yaml")
    ap.add_argument("--expand", action="store_true",
                    help="draw every physical link (default: collapse ECMP bundles)")
    ap.add_argument("-o", "--out", help="write SPL here (default: stdout)")
    args = ap.parse_args()

    node_names, links = load_topology(args.topo)
    roles = {n: classify(n) for n in node_names}
    edges = build_edges(links, roles, collapse=not args.expand)
    rows = build_rows(node_names, edges, roles)
    spl = emit_spl(rows)

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(spl)
        mode = "expanded" if args.expand else "collapsed"
        print(f"wrote {args.out}: {len(node_names)} nodes, {len(edges)} edges "
              f"({mode}), {len(rows)} SPL rows")
    else:
        print(spl, end="")


if __name__ == "__main__":
    main()
