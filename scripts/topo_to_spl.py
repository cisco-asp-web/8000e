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
one edge whose linkValues encodes the member count (ECMP bundle size). Fabric
links render at 2× the width of host access links. Pass --expand to draw every
physical link separately.

Usage:
  ./topo_to_spl.py [--topo topology.clab.yaml] [--expand] [-o out.spl]
  ./topo_to_spl.py --demo-dashboard scripts/topology_network_graph.demo.json
  ./topo_to_spl.py --demo-state training -o training.spl --dashboard training.json
"""

import argparse
import os
import re
from collections import OrderedDict

import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)


def load_colors_cfg(path=None):
    """Parse scripts/colors.cfg (name: #hex) for shared palette references."""
    path = path or os.path.join(_SCRIPT_DIR, "colors.cfg")
    colors = {}
    if not os.path.isfile(path):
        return colors
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            name, _, value = line.partition(":")
            colors[name.strip()] = value.strip()
    return colors


PALETTE = load_colors_cfg()

# ---- role styling -----------------------------------------------------------
# icons: Splunk built-in names from the Network Graph docs. There is no Cisco
# router glyph; networkDevice is the closest built-in. Custom SVGs via data
# URI or remote URL are also supported (see NETWORK_GRAPH.md).
ROLE_STYLE = {
    "sar_p00": {"color": PALETTE.get("blue", "#009CEB"), "icon": "networkDevice", "icon_color": "#FFFFFF", "value": "10", "size": "32"},
    "sar_p01": {"color": "#4fa484", "icon": "networkDevice", "icon_color": "#FFFFFF", "value": "10", "size": "32"},
    "trn":     {"color": PALETTE.get("purple", "#a873dd"), "icon": "servers", "icon_color": "#FFFFFF", "value": "5", "size": "30"},
    "inf":     {"color": PALETTE.get("light_blue", "#7dcff2"), "icon": "servers", "icon_color": "#FFFFFF", "value": "5", "size": "30"},
}

# Link widths (host access links slightly thicker than baseline 1).
HOST_LINK_WIDTH = 1.5
FABRIC_LINK_WIDTH = 2
FABRIC_ODN_TRAINING_BUNDLES = 3   # legacy ODN fallback when a role has no explicit color

# Per-link colors — source of truth aligned with topology_network_graph.dashboard.json.
# Dashboard linkColorsEditorConfig overrides these at render time via linkRole matchValue.
FABRIC_LINK_COLOR_DEFAULTS = {
    "plane0-link0": "#c290f4",
    "plane0-link1": "#7dcff2",
    "plane0-link2": "#c290f4",
    "plane0-link3": "#c290f4",
    "plane1-link0": "#c290f4",
    "plane1-link1": "#c290f4",
    "plane1-link2": "#c290f4",
    "plane1-link3": "#7dcff2",
}
HOST_LINK_COLOR_DEFAULTS = {
    "training0-link0": "#c290f4",
    "training0-link1": "#c290f4",
    "training0-link2": "#c290f4",
    "training0-link3": "#c290f4",
    "inference0-link0": "#bdbdbc",
    "inference0-link1": "#7dcff2",
    "inference0-link2": "#bdbdbc",
    "inference0-link3": "#7dcff2",
    "training1-link0": "#a873dd",
    "training1-link1": "#a873dd",
    "training1-link2": "#a873dd",
    "training1-link3": "#c290f4",
    "inference1-link0": "#bdbdbc",
    "inference1-link1": "#7dcff2",
    "inference1-link2": "#7dcff2",
    "inference1-link3": "#bdbdbc",
}
LINK_COLOR_DEFAULTS = {**FABRIC_LINK_COLOR_DEFAULTS, **HOST_LINK_COLOR_DEFAULTS}

# Demo traffic visualization (see --demo-dashboard / --demo-state).
DEMO_STATES = ("idle", "training", "inference")
DEMO_IDLE_COLOR = "#bdbdbc"
DEMO_TRAINING_ACTIVE_COLOR = "#c290f4"
DEMO_TAB_LABELS = {
    "idle": "1 — Idle (no traffic)",
    "training": "2 — Training traffic",
    "inference": "3 — Training + inference",
}
# Fallback when a link role is missing from LINK_COLOR_DEFAULTS (SPL-only / legacy).
TRAINING_LINK_COLOR = PALETTE.get("red", "#af575a")
INFERENCE_LINK_COLOR = PALETTE.get("yellow", "#f8be44")

# Host access link roles: {kind}{dc}-link{0-3} (plane/sar index).
HOST_LINK_HOSTS = {
    "dc00-host00-trn": ("training", 0),
    "dc00-host01-inf": ("inference", 0),
    "dc01-host00-trn": ("training", 1),
    "dc01-host01-inf": ("inference", 1),
}


def link_color_for_role(role):
    if role in LINK_COLOR_DEFAULTS:
        return LINK_COLOR_DEFAULTS[role]
    if role.startswith("plane"):
        link_idx = int(role.rsplit("-link", 1)[1])
        if link_idx < FABRIC_ODN_TRAINING_BUNDLES:
            return TRAINING_LINK_COLOR
        return INFERENCE_LINK_COLOR
    if role.startswith("training"):
        return TRAINING_LINK_COLOR
    if role.startswith("inference"):
        return INFERENCE_LINK_COLOR
    return PALETTE.get("gray", "#9E9E9E")


def _link_role_sort_key(role):
    if role.startswith("plane"):
        return (0, role)
    if role.startswith("training"):
        return (1, role)
    if role.startswith("inference"):
        return (2, role)
    return (3, role)


def short_label(name):
    """Compact node label for the graph (full name remains in source/tooltip)."""
    m = re.match(r"dc(\d+)-p(\d+)-sar(\d+)", name)
    if m:
        return f"d{m.group(1)}/p{m.group(2)}/s{m.group(3)}"
    if name.endswith("-trn"):
        dc = "0" if name.startswith("dc0") else "1"
        return f"d{dc}/trn"
    if name.endswith("-inf"):
        dc = "0" if name.startswith("dc0") else "1"
        return f"d{dc}/inf"
    return name


# Friendly node labels shown on the graph (source/tooltip keeps hostname).
DISPLAY_LABELS = {
    "dc00-p00-sar00": "r01",
    "dc00-p00-sar01": "r02",
    "dc01-p00-sar00": "r05",
    "dc01-p00-sar01": "r06",
    "dc00-p01-sar00": "r03",
    "dc00-p01-sar01": "r04",
    "dc01-p01-sar00": "r07",
    "dc01-p01-sar01": "r08",
    "dc00-host00-trn": "training",
    "dc00-host01-inf": "inference",
    "dc01-host00-trn": "training",
    "dc01-host01-inf": "inference",
}


def node_display_text(name, short_labels):
    if short_labels:
        return short_label(name)
    return DISPLAY_LABELS.get(name, name)


def classify(name):
    """Return the role key for a node name."""
    if name.endswith("-trn"):
        return "trn"
    if name.endswith("-inf"):
        return "inf"
    if "sar" in name:
        return "sar_p01" if "-p01-" in name else "sar_p00"
    return "sar_p00"


def data_center(name):
    """Cluster label. DC0 side = dc00-*/dc0-*; DC1 side = dc01-*/dc1-*."""
    if name.startswith("dc01") or name.startswith("dc1-"):
        return "DC1"
    return "DC0"


def is_route_reflector(name):
    return "xrd-rr" in name


def link_category(a, b, roles):
    ra, rb = roles[a], roles[b]
    if "trn" in (ra, rb) or "inf" in (ra, rb):
        return "host"
    return "fabric"


def plane_key(name):
    return "p01" if "-p01-" in name else "p00"


class FabricLinkRoleAssigner:
    """Assign plane{0|1}-link{0-3} roles to scale-across fabric bundles."""

    def __init__(self):
        self._bundle_idx = {"p00": 0, "p01": 0}
        self._last_bundle = {"p00": None, "p01": None}

    def classify(self, src, dst):
        plane = plane_key(src)
        plane_idx = 1 if plane == "p01" else 0
        bundle = tuple(sorted((src, dst)))
        if bundle != self._last_bundle[plane]:
            link_idx = self._bundle_idx[plane]
            self._bundle_idx[plane] += 1
            self._last_bundle[plane] = bundle
        else:
            link_idx = self._bundle_idx[plane] - 1
        role = f"plane{plane_idx}-link{link_idx}"
        return role, link_color_for_role(role)


def fabric_link_color(role):
    return link_color_for_role(role)


def host_endpoint(src, dst):
    if src in HOST_LINK_HOSTS:
        return src
    if dst in HOST_LINK_HOSTS:
        return dst
    raise ValueError(f"host link missing known host endpoint: {src} -> {dst}")


def sar_endpoint(src, dst, host):
    return dst if src == host else src


def host_link_index(sar):
    """Map SAR endpoint to link0-3: p00-sar00=0, p00-sar01=1, p01-sar00=2, p01-sar01=3."""
    plane_off = 0 if plane_key(sar) == "p00" else 2
    sar_off = 0 if sar.endswith("sar00") else 1
    return plane_off + sar_off


def host_link_role(src, dst):
    host = host_endpoint(src, dst)
    kind, dc_idx = HOST_LINK_HOSTS[host]
    sar = sar_endpoint(src, dst, host)
    link_idx = host_link_index(sar)
    return f"{kind}{dc_idx}-link{link_idx}"


def host_link_color(role):
    return link_color_for_role(role)


def load_router_interfaces(config_dir=None):
    """Parse SAR startup configs: router -> {ifname: description}."""
    config_dir = config_dir or os.path.join(_REPO_ROOT, "config")
    by_router = {}
    if not os.path.isdir(config_dir):
        return by_router
    for fn in sorted(os.listdir(config_dir)):
        if not fn.endswith(".cfg") or "xrd-rr" in fn:
            continue
        hostname = fn[:-4]
        ifname = None
        by_router[hostname] = {}
        with open(os.path.join(config_dir, fn)) as fh:
            for raw in fh:
                if raw.startswith("interface "):
                    ifname = raw.split()[1].strip()
                    continue
                if ifname and raw.startswith(" description "):
                    desc = raw.split("description ", 1)[1].strip()
                    # First top-level description wins (bundle members re-list interfaces).
                    by_router[hostname].setdefault(ifname, desc)
    return by_router


def telemetry_router(src, dst, category):
    """Router hostname whose OpenConfig counters back a graph link row."""
    if category == "host":
        if src in HOST_LINK_HOSTS:
            return dst
        if dst in HOST_LINK_HOSTS:
            return src
    return src


def host_description_key(host):
    """Primary IOS-XR description prefix for a topology host (no -lN suffix)."""
    if host.endswith("-trn"):
        base = host[:-4]
    elif host.endswith("-inf"):
        base = host[:-4]
    else:
        return host
    m = re.match(r"dc(\d+)-(.*)", base)
    if not m:
        return base
    dc_num, rest = m.group(1), m.group(2)
    if dc_num in ("0", "00"):
        return f"dc00-{rest}"
    if dc_num in ("1", "01"):
        return f"dc01-{rest}"
    return base


def host_description_candidates(host):
    """Description prefixes seen in IOS-XR (dc1 vs dc01 on sar01, etc.)."""
    keys = []
    primary = host_description_key(host)
    keys.append(primary)
    if primary.startswith("dc01-"):
        keys.append("dc1-" + primary[5:])
    if primary.startswith("dc00-"):
        rest = primary[5:]
        if host.startswith("dc0-") and not host.startswith("dc00"):
            keys.append(f"dc0-{rest}")
    seen = set()
    ordered = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def resolve_link_ifname(router, peer, link_role, category, router_ifs):
    """OpenConfig interface name on router for a topology link row."""
    if not link_role or not router:
        return ""
    if category == "fabric":
        m = re.search(r"link(\d+)$", link_role)
        if not m:
            return ""
        needle = f"to {peer} link{m.group(1)}"
        for ifname, desc in router_ifs.get(router, {}).items():
            if needle in desc:
                return ifname
        return ""
    host = peer if peer in HOST_LINK_HOSTS else router
    suffix = "l0" if router.endswith("sar00") else "l1"
    for key in host_description_candidates(host):
        desc_key = f"{key}-{suffix}"
        for ifname, desc in router_ifs.get(router, {}).items():
            if desc == desc_key:
                return ifname
    return ""


def build_link_color_matches(existing=None):
    """linkColorsEditorConfig entries; preserve existing match colors when patching."""
    by_match = {e["match"]: e["value"] for e in (existing or [])}
    matches = []
    for role in sorted(LINK_COLOR_DEFAULTS, key=_link_role_sort_key):
        matches.append({"match": role, "value": by_match.get(role, LINK_COLOR_DEFAULTS[role])})
    return matches


def _training_active_roles():
    roles = set()
    for plane_idx in (0, 1):
        for link_idx in range(FABRIC_ODN_TRAINING_BUNDLES):
            roles.add(f"plane{plane_idx}-link{link_idx}")
    for dc_idx in (0, 1):
        for link_idx in range(4):
            roles.add(f"training{dc_idx}-link{link_idx}")
    return roles


def _inference_active_roles():
    roles = set()
    for plane_idx in (0, 1):
        roles.add(f"plane{plane_idx}-link{FABRIC_ODN_TRAINING_BUNDLES}")
    for dc_idx in (0, 1):
        for link_idx in range(4):
            roles.add(f"inference{dc_idx}-link{link_idx}")
    return roles


TRAINING_ACTIVE_ROLES = _training_active_roles()
INFERENCE_ACTIVE_ROLES = _inference_active_roles()


def demo_link_color_map(state):
    """Per-linkRole colors for idle / training / inference demo scenarios."""
    if state == "inference":
        return dict(LINK_COLOR_DEFAULTS)
    colors = {role: DEMO_IDLE_COLOR for role in LINK_COLOR_DEFAULTS}
    if state == "training":
        for role in TRAINING_ACTIVE_ROLES:
            colors[role] = DEMO_TRAINING_ACTIVE_COLOR
    return colors


def build_link_color_matches_from_map(color_map):
    return [
        {"match": role, "value": color_map[role]}
        for role in sorted(color_map, key=_link_role_sort_key)
    ]


def apply_demo_colors(rows, state):
    color_map = demo_link_color_map(state)
    for row in rows:
        if row["linkRole"]:
            row["linkColors"] = color_map[row["linkRole"]]
    return rows


def load_topology(path, exclude_rr=True):
    with open(path) as fh:
        doc = yaml.safe_load(fh)
    node_names = [
        n for n in doc["topology"]["nodes"].keys()
        if not (exclude_rr and is_route_reflector(n))
    ]
    links = []
    for link in doc["topology"].get("links", []):
        a = link["endpoints"][0].split(":", 1)[0]
        b = link["endpoints"][1].split(":", 1)[0]
        if exclude_rr and (is_route_reflector(a) or is_route_reflector(b)):
            continue
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


def build_rows(node_names, edges, roles, short_labels=False, router_ifs=None):
    """
    Return a list of column-dicts, one per SPL row.
    Definition rows (empty target) first, then edge rows.
    """
    router_ifs = router_ifs if router_ifs is not None else load_router_interfaces()
    rows = []

    # --- node definition rows: guarantee every node gets styled -------------
    for n in node_names:
        st = ROLE_STYLE[roles[n]]
        rows.append({
            "source": n, "target": "",
            "nodeTexts": node_display_text(n, short_labels), "type": data_center(n), "nodeRole": roles[n],
            "nodeColors": st["color"], "nodeIcons": st["icon"],
            "nodeIconColors": st["icon_color"], "nodeValues": st["value"],
            "nodeSize": st["size"],
            "linkRole": "", "linkColors": "", "linkValues": "", "linkWidths": "",
            "linkRouter": "", "linkIfname": "",
        })

    # --- link rows: styling columns describe the source node ----------------
    fabric_links = FabricLinkRoleAssigner()
    for src, dst, count, cat in edges:
        st = ROLE_STYLE[roles[src]]
        if cat == "fabric":
            link_role, link_color = fabric_links.classify(src, dst)
        else:
            link_role = host_link_role(src, dst)
            link_color = host_link_color(link_role)
        t_router = telemetry_router(src, dst, cat)
        peer = dst if t_router == src else src
        link_ifname = resolve_link_ifname(t_router, peer, link_role, cat, router_ifs)
        rows.append({
            "source": src, "target": dst,
            "nodeTexts": node_display_text(src, short_labels), "type": data_center(src), "nodeRole": roles[src],
            "nodeColors": st["color"], "nodeIcons": st["icon"],
            "nodeIconColors": st["icon_color"], "nodeValues": st["value"],
            "nodeSize": st["size"],
            "linkRole": link_role, "linkColors": link_color,
            "linkValues": str(count),
            "linkWidths": str(FABRIC_LINK_WIDTH if cat == "fabric" else HOST_LINK_WIDTH),
            "linkRouter": t_router, "linkIfname": link_ifname,
        })
    return rows


# column order in the emitted eval / table
COLUMNS = ["source", "target", "nodeTexts", "type", "nodeRole", "nodeColors", "nodeIcons",
           "nodeIconColors", "nodeValues", "nodeSize", "linkRole", "linkColors", "linkValues",
           "linkWidths", "linkRouter", "linkIfname"]

NODE_COLOR_MATCHES = [
    {"match": "sar_p00", "value": PALETTE.get("blue", "#009CEB")},
    {"match": "sar_p01", "value": "#4fa484"},
    {"match": "trn", "value": PALETTE.get("purple", "#a873dd")},
    {"match": "inf", "value": PALETTE.get("light_blue", "#7dcff2")},
]

LINK_COLOR_MATCHES = build_link_color_matches()

# Preset node positions: diagonal SAR pairs, DC1 left / DC2 right, hosts on flanks.
NODE_POSITIONS = {
    "dc00-host00-trn": {"x": 50,  "y": 120},
    "dc00-host01-inf": {"x": 50,  "y": 240},
    "dc00-p00-sar00":  {"x": 200, "y": 30},
    "dc00-p00-sar01":  {"x": 280, "y": 100},
    "dc00-p01-sar00":  {"x": 200, "y": 270},
    "dc00-p01-sar01":  {"x": 280, "y": 340},
    "dc01-p00-sar00":  {"x": 550, "y": 30},
    "dc01-p00-sar01":  {"x": 630, "y": 100},
    "dc01-p01-sar00":  {"x": 550, "y": 270},
    "dc01-p01-sar01":  {"x": 630, "y": 340},
    "dc01-host00-trn": {"x": 760, "y": 120},
    "dc01-host01-inf": {"x": 760, "y": 240},
}

# Floating text labels (absolute layout positions over the graph).
LABEL_FONT_SIZE = 20
PANEL_BACKGROUND = "#212529"

GRAPH_LAYOUT_HEIGHT = 480
DS_TOPOLOGY_ID = "ds_topology"
DS_LINKS_ID = "ds_topology_links"
DS_NODES_ID = "ds_topology_nodes"
TRAFFIC_CHART_VIZ_ID = "viz_link_traffic"
TRAFFIC_DS_ID = "ds_link_traffic"
TRAFFIC_CHART_HEIGHT = 280
CLICK_DEBUG_VIZ_ID = "viz_click_debug"  # retired; still popped so old JSON stays clean

# ds_link_traffic reads $global_time.*$, so the input backing that token must exist or
# Dashboard Studio refuses to render the panel ("Set token value to render visualization").
GLOBAL_TIME_INPUT_ID = "input_global_time"
GLOBAL_TIME_INPUT = {
    "options": {"defaultValue": "-60m,now", "token": "global_time"},
    "title": "Time range",
    "type": "input.timerange",
}

# Outbound-only query verified in Search (see stream-to-splunk.md). Avoid inbound+out
# split here — metric_name is not reliably present after mstats for match().
# mstats is a generating command: it must stay the first command, so the $router$
# token is guarded by its default rather than a preceding `where`.
# Splunk's network graph only emits click events for nodes, never links, so this is
# scoped to a router and split BY name to give one series per interface.
# rate() needs 2+ samples inside every span bucket; at a 30s MDT interval that is not
# guaranteed, so the delta is computed explicitly from latest() instead.
# Shut interfaces report out_octets=0 forever, so peak_octets>0 drops them without
# needing a per-router list of configured ports.
LINK_TRAFFIC_SPL = """
| mstats latest(_value) AS octets
  WHERE index=mdt_metrics
    metric_name="openconfig-interfaces:interfaces/interface.state/counters/out_octets"
    source="$router$"
  span=30s BY name
| where match(name, "^EightHundredGigE")
| eventstats max(octets) AS peak_octets BY name
| where peak_octets > 0
| sort 0 name, _time
| streamstats current=f window=1 last(octets) AS prev_octets last(_time) AS prev_time BY name
| eval secs=_time-prev_time
| eval mbps=if(isnull(prev_octets) OR secs<=0 OR octets<prev_octets, null(),
               round((octets-prev_octets)*8/(secs*1000000), 3))
| timechart span=30s limit=0 avg(mbps) BY name
""".strip()

# A node click emits the node id under row.source.value.
LINK_TRAFFIC_EVENT_HANDLERS = [
    {
        "type": "drilldown.setToken",
        "options": {
            "tokens": [
                {"token": "router", "key": "row.source.value"},
            ]
        },
    }
]

DASHBOARD_TOKEN_DEFAULTS = {
    "router": {"value": "__none__"},
}


def link_traffic_token_defaults(rows=None):
    """Preselect the first router so the chart renders before any click."""
    defaults = {k: dict(v) for k, v in DASHBOARD_TOKEN_DEFAULTS.items()}
    for row in rows or []:
        if row.get("linkRouter"):
            defaults["router"] = {"value": row["linkRouter"]}
            break
    return defaults


LINK_SPL_COLUMNS = ["source", "target", "linkRole", "linkColors", "linkValues", "linkWidths",
                    "linkRouter", "linkIfname"]
NODE_SPL_COLUMNS = ["node", "nodeTexts", "type", "nodeRole", "nodeColors", "nodeIcons",
                    "nodeIconColors", "nodeValues", "nodeSize"]


def emit_subset_spl(rows, columns):
    """makeresults SPL from explicit row dicts (no post-filter on combined topology)."""
    if not rows:
        return "| makeresults count=0 | table " + ", ".join(columns) + "\n"
    n = len(rows)
    lines = ["| makeresults", "| eval "]
    evals = []
    for col in columns:
        joined = ",".join(str(r.get(col, "")) for r in rows)
        evals.append(f'    {col}="{joined}"')
    evals.append(f"    row_index=mvrange(0,{n})")
    lines.append(",\n".join(evals))
    lines.append("| mvexpand row_index")
    lines.append("| eval ")
    splits = [f"    {col}=mvindex(split({col},\",\"),row_index)" for col in columns]
    lines.append(",\n".join(splits))
    if "nodeValues" in columns:
        lines.append("| eval nodeValues=tonumber(nodeValues), nodeSize=tonumber(nodeSize)")
    if "linkValues" in columns:
        lines.append("| eval linkValues=if(len(linkValues)==0, null(), tonumber(linkValues)), "
                     "linkWidths=if(len(linkWidths)==0, null(), tonumber(linkWidths))")
    lines.append("| table " + ", ".join(columns))
    return "\n".join(lines) + "\n"


def rows_for_link_spl(rows):
    return [{col: r[col] for col in LINK_SPL_COLUMNS} for r in rows if r["target"]]


def rows_for_node_spl(rows):
    out = []
    for r in rows:
        if r["target"]:
            continue
        nr = {col: r.get(col, "") for col in NODE_SPL_COLUMNS}
        nr["node"] = r["source"]
        out.append(nr)
    return out


def emit_link_spl_from_rows(rows):
    """Link rows only — primary data source for Network Graph."""
    return emit_subset_spl(rows_for_link_spl(rows), LINK_SPL_COLUMNS)


def emit_node_spl_from_rows(rows):
    """Node styling rows — nodeSource data source for Network Graph."""
    return emit_subset_spl(rows_for_node_spl(rows), NODE_SPL_COLUMNS)


def emit_link_spl(topology_spl):
    """Legacy wrapper: filter combined topology SPL (prefer emit_link_spl_from_rows)."""
    return (
        topology_spl.strip()
        + "\n| where len(target)>0\n"
        + "| table source, target, linkRole, linkColors, linkValues, linkWidths, linkRouter, linkIfname\n"
    )


def emit_node_spl(topology_spl):
    """Legacy wrapper: filter combined topology SPL (prefer emit_node_spl_from_rows)."""
    return (
        topology_spl.strip()
        + "\n| where isnull(target) OR target=\"\"\n"
        + "| eval node=source\n"
        + "| table node, nodeTexts, type, nodeRole, nodeColors, nodeIcons, nodeIconColors, nodeValues, nodeSize\n"
    )


def _topology_split_data_sources(rows, links_id=DS_LINKS_ID, nodes_id=DS_NODES_ID,
                                 links_name="topology_links", nodes_name="topology_nodes"):
    return {
        links_id: {
            "name": links_name,
            "options": {"query": emit_link_spl_from_rows(rows).strip()},
            "type": "ds.search",
        },
        nodes_id: {
            "name": nodes_name,
            "options": {"query": emit_node_spl_from_rows(rows).strip()},
            "type": "ds.search",
        },
    }


def _traffic_chart_layout_y():
    """Y position for chart below the graph."""
    return GRAPH_LAYOUT_HEIGHT + 20


def _link_traffic_chart_viz():
    return {
        "dataSources": {"primary": TRAFFIC_DS_ID},
        "options": {
            "backgroundColor": PANEL_BACKGROUND,
            "legendDisplay": "right",
            "yAxisTitleText": "Mbps",
        },
        "showLastUpdated": True,
        "showProgressBar": True,
        "title": "Outbound Mbps by interface — $router$",
        "type": "splunk.line",
    }


def _link_traffic_data_source():
    return {
        "name": "link_traffic",
        "options": {
            "query": LINK_TRAFFIC_SPL,
            "queryParameters": {
                "earliest": "$global_time.earliest$",
                "latest": "$global_time.latest$",
            },
        },
        "type": "ds.search",
    }


def ensure_link_traffic_panel(dash, rows=None, topology_spl=None):
    """Traffic chart below graph; tokens set from Network Graph link click."""
    defaults = dash.setdefault("defaults", {})
    tokens = defaults.setdefault("tokens", {}).setdefault("default", {})
    for stale in ("ifname", "link_role", "dbg_source", "dbg_target", "dbg_value", "dbg_name"):
        tokens.pop(stale, None)
    tokens.update(link_traffic_token_defaults(rows))

    dash.setdefault("inputs", {})[GLOBAL_TIME_INPUT_ID] = dict(GLOBAL_TIME_INPUT)

    visualizations = dash.setdefault("visualizations", {})
    visualizations.pop("viz_link_header", None)
    visualizations.pop("viz_link_picker", None)
    visualizations[TRAFFIC_CHART_VIZ_ID] = _link_traffic_chart_viz()
    visualizations.pop(CLICK_DEBUG_VIZ_ID, None)

    data_sources = dash.setdefault("dataSources", {})
    data_sources[TRAFFIC_DS_ID] = _link_traffic_data_source()
    data_sources.pop("ds_link_picker", None)

    layout = dash.setdefault("layout", {})
    global_inputs = layout.setdefault("globalInputs", [])
    if GLOBAL_TIME_INPUT_ID not in global_inputs:
        global_inputs.append(GLOBAL_TIME_INPUT_ID)

    layout_defs = layout.setdefault("layoutDefinitions", {})
    layout = layout_defs.get("layout_1")
    if not layout:
        return
    structure = layout.setdefault("structure", [])
    structure[:] = [
        b for b in structure
        if b.get("item") not in ("viz_link_header", "viz_link_picker", CLICK_DEBUG_VIZ_ID)
    ]
    by_item = {block.get("item"): block for block in structure if block.get("type") == "block"}

    graph = by_item.get("viz_topology_graph")
    if graph:
        graph["position"]["h"] = GRAPH_LAYOUT_HEIGHT

    chart_y = _traffic_chart_layout_y()
    chart_pos = {"x": 0, "y": chart_y, "w": 1200, "h": TRAFFIC_CHART_HEIGHT}
    block = by_item.get(TRAFFIC_CHART_VIZ_ID)
    if block is None:
        structure.append({"item": TRAFFIC_CHART_VIZ_ID, "type": "block", "position": chart_pos})
    else:
        block["position"] = chart_pos


def configure_topology_graph_viz(viz, existing_links=None):
    """Wire Network Graph to combined topology SPL + link-click Set tokens."""
    link_matches = build_link_color_matches(existing_links)
    viz["context"]["linkColorsEditorConfig"] = link_matches
    viz["context"]["nodeColorsEditorConfig"] = NODE_COLOR_MATCHES
    viz["dataSources"] = {
        "primary": DS_TOPOLOGY_ID,
    }
    viz["eventHandlers"] = LINK_TRAFFIC_EVENT_HANDLERS
    opts = viz.setdefault("options", {})
    opts.update({
        "backgroundColor": PANEL_BACKGROUND,
        "layout": "force",
        "linkColorValues": "> primary | seriesByName('linkRole')",
        "linkColors": "> linkColorValues | matchValue(linkColorsEditorConfig)",
        "linkDistance": 80,
        "linkStyle": "straight",
        "linkWidth": "> primary | seriesByName('linkWidths')",
        "nodeColorValues": "> primary | seriesByName('nodeRole')",
        "nodeColors": "> nodeColorValues | matchValue(nodeColorsEditorConfig)",
        "nodeDragPositions": NODE_POSITIONS,
        "nodeHorizontalPadding": 40,
        "nodeIconColors": "> primary | seriesByName('nodeIconColors')",
        "nodeIcons": "> primary | seriesByName('nodeIcons')",
        "nodeSize": "> primary | seriesByName('nodeSize')",
        "nodeTextFontSize": 12,
        "nodeTexts": "> primary | seriesByName('nodeTexts')",
        "nodeVerticalPadding": 20,
        "showDirection": "none",
        "showZoomControls": True,
        "tooltipHeaderField": "> primary | seriesByName('source')",
    })
    opts.pop("nodeIds", None)


LABEL_OVERLAYS = [
    {
        "id": "viz_label_dc0",
        "text": "DC-1",
        "position": {"x": 60, "y": 340, "w": 60, "h": 32},
    },
    {
        "id": "viz_label_dc1",
        "text": "DC-2",
        "position": {"x": 1090, "y": 340, "w": 80, "h": 32},
    },
    {
        "id": "viz_label_plane0",
        "text": "Scale Across Plane-1",
        "position": {"x": 520, "y": 80, "w": 210, "h": 32},
    },
    {
        "id": "viz_label_plane1",
        "text": "Scale Across Plane-2",
        "position": {"x": 520, "y": 420, "w": 210, "h": 32},
    },
]


def _text_overlay_viz(spec):
    return {
        "options": {
            "backgroundColor": PANEL_BACKGROUND,
            "customFontSize": LABEL_FONT_SIZE,
            "fontColor": "#FFFFFF",
            "fontSize": "custom",
            "markdown": f"**{spec['text']}**",
        },
        "showLastUpdated": False,
        "showProgressBar": False,
        "type": "splunk.markdown",
    }


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
    lines.append("| eval nodeValues=tonumber(nodeValues), nodeSize=tonumber(nodeSize)")
    lines.append("| eval linkValues=if(len(linkValues)==0, null(), tonumber(linkValues)), "
                 "linkWidths=if(len(linkWidths)==0, null(), tonumber(linkWidths))")
    lines.append("| eval target=if(len(target)==0, null(), target)")
    lines.append("| table " + ", ".join(COLUMNS))
    return "\n".join(lines) + "\n"


def patch_dashboard_json(path, spl, rows, title="Scale Across Topology"):
    """Update SPL query + link/node color config; preserve layout and manual viz edits."""
    import json
    with open(path) as fh:
        dash = json.load(fh)
    existing_links = dash.get("visualizations", {}).get("viz_topology_graph", {}).get(
        "context", {}
    ).get("linkColorsEditorConfig")
    spl = spl.strip()
    dash["dataSources"]["ds_topology"] = {
        "name": "topology",
        "options": {"query": spl},
        "type": "ds.search",
    }
    dash["dataSources"].pop(DS_LINKS_ID, None)
    dash["dataSources"].pop(DS_NODES_ID, None)
    configure_topology_graph_viz(dash["visualizations"]["viz_topology_graph"], existing_links)
    ensure_link_traffic_panel(dash, rows, topology_spl=spl)
    with open(path, "w") as fh:
        json.dump(dash, fh, indent=4, ensure_ascii=False)
        fh.write("\n")


def _topology_graph_viz(viz_id, primary_ds_id, link_matches, graph_title):
    viz = {
        "context": {
            "linkColorsEditorConfig": link_matches,
            "nodeColorsEditorConfig": NODE_COLOR_MATCHES,
        },
        "dataSources": {
            "primary": primary_ds_id,
        },
        "eventHandlers": LINK_TRAFFIC_EVENT_HANDLERS,
        "options": {
            "backgroundColor": PANEL_BACKGROUND,
            "layout": "force",
            "linkColorValues": "> primary | seriesByName('linkRole')",
            "linkColors": "> linkColorValues | matchValue(linkColorsEditorConfig)",
            "linkDistance": 80,
            "linkStyle": "straight",
            "linkWidth": "> primary | seriesByName('linkWidths')",
            "nodeColorValues": "> primary | seriesByName('nodeRole')",
            "nodeColors": "> nodeColorValues | matchValue(nodeColorsEditorConfig)",
            "nodeDragPositions": NODE_POSITIONS,
            "nodeHorizontalPadding": 40,
            "nodeIconColors": "> primary | seriesByName('nodeIconColors')",
            "nodeIcons": "> primary | seriesByName('nodeIcons')",
            "nodeSize": "> primary | seriesByName('nodeSize')",
            "nodeTextFontSize": 12,
            "nodeTexts": "> primary | seriesByName('nodeTexts')",
            "nodeVerticalPadding": 20,
            "showDirection": "none",
            "showZoomControls": True,
            "tooltipHeaderField": "> primary | seriesByName('source')",
        },
        "showLastUpdated": False,
        "showProgressBar": False,
        "title": graph_title,
        "type": "splunk.networkGraph",
    }
    return viz


def _graph_layout_structure(graph_viz_id, include_traffic_panel=False):
    structure = [
        {
            "item": graph_viz_id,
            "position": {"h": GRAPH_LAYOUT_HEIGHT if include_traffic_panel else 700,
                         "w": 1200, "x": 0, "y": 0},
            "type": "block",
        },
    ]
    for spec in LABEL_OVERLAYS:
        pos = spec["position"]
        structure.append({
            "item": spec["id"],
            "position": {"h": pos["h"], "w": pos["w"], "x": pos["x"], "y": pos["y"]},
            "type": "block",
        })
    if include_traffic_panel:
        chart_y = _traffic_chart_layout_y()
        structure.append({
            "item": TRAFFIC_CHART_VIZ_ID,
            "position": {"h": TRAFFIC_CHART_HEIGHT, "w": 1200, "x": 0, "y": chart_y},
            "type": "block",
        })
    return structure


def emit_dashboard(spl, rows, title="Scale Across Topology", link_color_matches=None,
                   viz_id="viz_topology_graph", graph_title=None):
    """Dashboard Studio JSON with viz options wired to SPL data fields."""
    import json

    spl = spl.strip()
    link_matches = link_color_matches if link_color_matches is not None else LINK_COLOR_MATCHES
    graph_title = graph_title or title
    visualizations = {}
    for spec in LABEL_OVERLAYS:
        visualizations[spec["id"]] = _text_overlay_viz(spec)
    visualizations[viz_id] = _topology_graph_viz(
        viz_id, DS_TOPOLOGY_ID, link_matches, graph_title
    )
    visualizations[TRAFFIC_CHART_VIZ_ID] = _link_traffic_chart_viz()

    data_sources = {
        DS_TOPOLOGY_ID: {
            "name": "topology",
            "options": {"query": spl},
            "type": "ds.search",
        },
        TRAFFIC_DS_ID: _link_traffic_data_source(),
    }

    dash = {
        "title": title,
        "description": "Import via Dashboard Studio → Edit → Source code. "
                       "Viz options map nodeColors/nodeSize from SPL.",
        "inputs": {GLOBAL_TIME_INPUT_ID: dict(GLOBAL_TIME_INPUT)},
        "defaults": {
            "tokens": {"default": link_traffic_token_defaults(rows)},
            "dataSources": {
                "ds.search": {
                    "options": {
                        "queryParameters": {"earliest": "-24h@h", "latest": "now"}
                    }
                }
            }
        },
        "visualizations": visualizations,
        "dataSources": data_sources,
        "layout": {
            "globalInputs": [GLOBAL_TIME_INPUT_ID],
            "layoutDefinitions": {
                "layout_1": {
                    "structure": _graph_layout_structure(viz_id, include_traffic_panel=True),
                    "type": "absolute",
                }
            },
            "tabs": {
                "items": [{"label": title, "layoutId": "layout_1"}]
            },
        },
    }
    return json.dumps(dash, indent=2, ensure_ascii=False) + "\n"


def emit_demo_dashboard(base_rows, title="Scale Across Topology (Demo)"):
    """One dashboard JSON with idle / training / inference tabs."""
    import json

    visualizations = {}
    for spec in LABEL_OVERLAYS:
        visualizations[spec["id"]] = _text_overlay_viz(spec)

    data_sources = {}
    layout_definitions = {}
    tab_items = []

    for state in DEMO_STATES:
        viz_id = f"viz_topology_{state}"
        ds_id = f"ds_topology_{state}"
        layout_id = f"layout_{state}"
        state_rows = apply_demo_colors([dict(r) for r in base_rows], state)
        spl = emit_spl(state_rows).strip()
        link_matches = build_link_color_matches_from_map(demo_link_color_map(state))
        visualizations[viz_id] = _topology_graph_viz(
            viz_id, ds_id, link_matches, DEMO_TAB_LABELS[state]
        )
        data_sources[ds_id] = {
            "name": f"topology_{state}",
            "options": {"query": spl},
            "type": "ds.search",
        }
        layout_definitions[layout_id] = {
            "structure": _graph_layout_structure(viz_id),
            "type": "absolute",
        }
        tab_items.append({"label": DEMO_TAB_LABELS[state], "layoutId": layout_id})

    dash = {
        "title": title,
        "description": "Demo dashboard: tab through idle → training → inference link colors. "
                       "Import via Dashboard Studio → Edit → Source code.",
        "inputs": {},
        "defaults": {
            "dataSources": {
                "ds.search": {
                    "options": {
                        "queryParameters": {"earliest": "-24h@h", "latest": "now"}
                    }
                }
            }
        },
        "visualizations": visualizations,
        "dataSources": data_sources,
        "layout": {
            "layoutDefinitions": layout_definitions,
            "tabs": {"items": tab_items},
        },
    }
    return json.dumps(dash, indent=2, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topo", default="topology.clab.yaml")
    ap.add_argument("--expand", action="store_true",
                    help="draw every physical link separately (default: collapsed bundles)")
    ap.add_argument("--include-rr", action="store_true",
                    help="include route reflectors and their links")
    ap.add_argument("--short-labels", action="store_true",
                    help="use compact nodeTexts (d0/p0/s0); full name in source/tooltip")
    ap.add_argument("-o", "--out", help="write SPL here (default: stdout)")
    ap.add_argument("--dashboard", metavar="FILE",
                    help="write Dashboard Studio JSON (use --patch-dashboard to merge into existing)")
    ap.add_argument("--demo-dashboard", metavar="FILE",
                    help="write one 3-tab demo dashboard (idle → training → inference)")
    ap.add_argument("--demo-state", choices=DEMO_STATES,
                    help="apply demo link colors to SPL/dashboard output (idle|training|inference)")
    ap.add_argument("--patch-dashboard", metavar="FILE",
                    help="update query/colors/positions in existing dashboard JSON in place")
    args = ap.parse_args()

    node_names, links = load_topology(args.topo, exclude_rr=not args.include_rr)
    roles = {n: classify(n) for n in node_names}
    expand_links = args.expand
    edges = build_edges(links, roles, collapse=not expand_links)
    rows = build_rows(node_names, edges, roles, short_labels=args.short_labels)

    if args.demo_state:
        apply_demo_colors(rows, args.demo_state)

    spl = emit_spl(rows)

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(spl)
        mode = "expanded" if expand_links else "collapsed"
        demo_note = f", demo={args.demo_state}" if args.demo_state else ""
        print(f"wrote {args.out}: {len(node_names)} nodes, {len(edges)} edges "
              f"({mode}), {len(rows)} SPL rows{demo_note}")
    elif not args.dashboard and not args.demo_dashboard and not args.patch_dashboard:
        print(spl, end="")

    if args.dashboard:
        link_matches = None
        if args.demo_state:
            link_matches = build_link_color_matches_from_map(demo_link_color_map(args.demo_state))
        with open(args.dashboard, "w") as fh:
            fh.write(emit_dashboard(spl, rows, link_color_matches=link_matches))
        print(f"wrote {args.dashboard}")

    if args.demo_dashboard:
        with open(args.demo_dashboard, "w") as fh:
            fh.write(emit_demo_dashboard(rows))
        print(f"wrote {args.demo_dashboard} (3 tabs: {', '.join(DEMO_STATES)})")

    if args.patch_dashboard:
        patch_dashboard_json(args.patch_dashboard, spl, rows)
        print(f"patched {args.patch_dashboard}")


if __name__ == "__main__":
    main()
