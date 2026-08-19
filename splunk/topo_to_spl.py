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
  ./topo_to_spl.py --demo-dashboard splunk/topology_network_graph.demo.json
  ./topo_to_spl.py --demo-state srv6te -o srv6te.spl --dashboard srv6te.json
"""

import argparse
import os
import re
from collections import OrderedDict

import yaml

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)


def load_colors_cfg(path=None):
    """Parse splunk/colors.cfg (name: #hex) for shared palette references."""
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

# Demo click-through (see --demo-dashboard / --demo-state). Three tabs, meant to
# be screenshotted in order:
#   topology  the fabric on its own, every link neutral
#   srv6te    links colored by the traffic class SRv6-TE steers onto them
#   traffic   same colors, plus the live per-interface traffic panel
DEMO_STATES = ("topology", "srv6te", "traffic")
# Neutral link color for the base view. Light enough to read against
# PANEL_BACKGROUND (#212529) without competing with the node colors.
DEMO_BASE_LINK_COLOR = "#bdbdbc"
# Steered-link colors match the host node whose traffic they carry, so the eye
# follows host -> access link -> WAN bundle without needing a legend.
DEMO_TRAINING_COLOR = ROLE_STYLE["trn"]["color"]
DEMO_INFERENCE_COLOR = ROLE_STYLE["inf"]["color"]
DEMO_TAB_LABELS = {
    "topology": "1 — Topology",
    "srv6te": "2 — SRv6-TE steering",
    "traffic": "3 — SRv6-TE + live traffic",
}
# Only this tab carries the traffic panel; the first two stay pure topology.
DEMO_TRAFFIC_STATE = "traffic"
# Demo only: which collapsed WAN bundles carry inference, named by router pair
# rather than by link index so the steered path reads directly off this table.
# Both sit in Plane-2, giving inference one path per DC-1 inference-facing
# router: dc01-inf -> r03 -> r07 -> dc02-inf and dc01-inf -> r04 -> r08 ->
# dc02-inf. Every bundle not listed here carries training, which keeps the
# 75/25 split (2 of 8 bundles) without hardcoding the ratio anywhere.
DEMO_INFERENCE_BUNDLES = (("r03", "r07"), ("r04", "r08"))
# Fallback when a link role is missing from LINK_COLOR_DEFAULTS (SPL-only / legacy).
TRAINING_LINK_COLOR = PALETTE.get("red", "#af575a")
INFERENCE_LINK_COLOR = PALETTE.get("yellow", "#f8be44")

# Fabric routers: name -> (dc_index, plane_index, pair_index). The hostnames
# deliberately carry no topology, so a router's place in the fabric is declared
# here: dc_index 0/1 = DC-1/DC-2, plane_index 0/1 = Plane-1/Plane-2, and
# pair_index distinguishes the two routers a host dual-homes onto in one plane.
ROUTERS = {
    "r01": (0, 0, 0),
    "r02": (0, 0, 1),
    "r03": (0, 1, 0),
    "r04": (0, 1, 1),
    "r05": (1, 0, 0),
    "r06": (1, 0, 1),
    "r07": (1, 1, 0),
    "r08": (1, 1, 1),
}

# Route reflectors are plane-scoped, not per-DC: each peers with all four
# routers of one plane across both data centers. name -> plane_index.
ROUTE_REFLECTORS = {"rr01": 0, "rr02": 1}

# Host access link roles: {kind}{dc}-link{0-3} (plane/pair index).
HOST_LINK_HOSTS = {
    "dc01-trn": ("training", 0),
    "dc01-inf": ("inference", 0),
    "dc02-trn": ("training", 1),
    "dc02-inf": ("inference", 1),
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
    if name in ROUTERS:
        dc, plane, pair = ROUTERS[name]
        return f"d{dc + 1}/p{plane + 1}/s{pair}"
    if name in HOST_LINK_HOSTS:
        kind, dc = HOST_LINK_HOSTS[name]
        return f"d{dc + 1}/{kind[:3]}"
    return name


# Friendly node labels shown on the graph (source/tooltip keeps hostname).
# Router names are already short, so only the hosts are relabelled by role.
DISPLAY_LABELS = {
    "dc01-trn": "training",
    "dc01-inf": "inference",
    "dc02-trn": "training",
    "dc02-inf": "inference",
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
    return "sar_p01" if plane_index(name) else "sar_p00"


def data_center(name):
    """Cluster label used as the SPL `type` column: DC1 or DC2."""
    if name in ROUTERS:
        return f"DC{ROUTERS[name][0] + 1}"
    if name in HOST_LINK_HOSTS:
        return f"DC{HOST_LINK_HOSTS[name][1] + 1}"
    return "DC1"  # route reflectors attach to DC-1 routers


def is_route_reflector(name):
    return name in ROUTE_REFLECTORS


def link_category(a, b, roles):
    ra, rb = roles[a], roles[b]
    if "trn" in (ra, rb) or "inf" in (ra, rb):
        return "host"
    return "fabric"


def plane_index(name):
    """0 = Plane-1, 1 = Plane-2. Unknown nodes fall back to plane 0."""
    if name in ROUTERS:
        return ROUTERS[name][1]
    return ROUTE_REFLECTORS.get(name, 0)


class FabricLinkRoleAssigner:
    """Assign plane{0|1}-link{0-3} roles to scale-across fabric bundles."""

    def __init__(self):
        self._bundle_idx = [0, 0]
        self._last_bundle = [None, None]

    def classify(self, src, dst):
        plane = plane_index(src)
        bundle = tuple(sorted((src, dst)))
        if bundle != self._last_bundle[plane]:
            link_idx = self._bundle_idx[plane]
            self._bundle_idx[plane] += 1
            self._last_bundle[plane] = bundle
        else:
            link_idx = self._bundle_idx[plane] - 1
        role = f"plane{plane}-link{link_idx}"
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
    """Map router endpoint to link0-3, ordered plane-major then pair."""
    _dc, plane, pair = ROUTERS.get(sar, (0, 0, 0))
    return plane * 2 + pair


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
        if not fn.endswith(".cfg"):
            continue
        hostname = fn[:-4]
        # Skips route reflectors and non-device configs (nso.cfg, sid-lists.cfg).
        if hostname not in ROUTERS:
            continue
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
    # Host access links are described as "<host>-l0"/"-l1", where the suffix is
    # the router's pair_index within the plane.
    host = peer if peer in HOST_LINK_HOSTS else router
    desc_key = f"{host}-l{ROUTERS.get(router, (0, 0, 0))[2]}"
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


def _link_router(row):
    """The router endpoint of a link row, whichever side it is on."""
    for endpoint in (row.get("source"), row.get("target")):
        if endpoint in ROUTERS:
            return endpoint
    return None


def _bundle_pair(row):
    """Unordered router pair for a WAN link row, or None for anything else."""
    a, b = row.get("source"), row.get("target")
    if a in ROUTERS and b in ROUTERS:
        return frozenset((a, b))
    return None


def demo_role_classes(rows):
    """Split link roles into (training, inference) sets for the demo.

    WAN bundles are classified by router pair against DEMO_INFERENCE_BUNDLES;
    everything else is training. A host access link then inherits the class of
    the router it lands on, but only if that router actually terminates a
    bundle of that class -- an access link never implies a steered path that
    does not exist. Roles in neither set are unsteered and render neutral (or
    are dropped, see demo_rows).

    Derived from the edge list at generation time, so re-pointing a bundle in
    topology.clab.yaml or in DEMO_INFERENCE_BUNDLES keeps the access links
    honest without further edits.
    """
    inference_pairs = {frozenset(pair) for pair in DEMO_INFERENCE_BUNDLES}
    training, inference = set(), set()
    trn_routers, inf_routers = set(), set()

    for row in rows or []:
        role, pair = row.get("linkRole"), _bundle_pair(row)
        if not role or pair is None:
            continue
        if pair in inference_pairs:
            inference.add(role)
            inf_routers |= set(pair)
        else:
            training.add(role)
            trn_routers |= set(pair)

    for row in rows or []:
        role = row.get("linkRole")
        if not role or _bundle_pair(row) is not None:
            continue
        router = _link_router(row)
        if role.startswith("inference") and router in inf_routers:
            inference.add(role)
        elif role.startswith("training") and router in trn_routers:
            training.add(role)
    return training, inference


def demo_link_color_map(state, rows=None):
    """Per-linkRole colors for one demo state.

    "topology" leaves every link neutral. The other two share one color map:
    tab 3 differs from tab 2 only by adding the traffic panel, so the graph
    itself must look identical between them.

    Needs `rows` to classify anything; without them every role comes back
    neutral.
    """
    if state == "topology":
        return {role: DEMO_BASE_LINK_COLOR for role in LINK_COLOR_DEFAULTS}

    training, inference = demo_role_classes(rows)
    colors = {}
    for role in LINK_COLOR_DEFAULTS:
        if role in training:
            colors[role] = DEMO_TRAINING_COLOR
        elif role in inference:
            colors[role] = DEMO_INFERENCE_COLOR
        else:
            colors[role] = DEMO_BASE_LINK_COLOR
    return colors


def build_link_color_matches_from_map(color_map):
    return [
        {"match": role, "value": color_map[role]}
        for role in sorted(color_map, key=_link_role_sort_key)
    ]


def demo_rows(base_rows, state):
    """Rows for one demo state: colored, with unsteered links dropped.

    Tab 1 is the fabric as built, so every link stays. On the steered tabs a
    link carrying neither class would render neutral next to colored ones and
    read as "off" rather than "not part of this story", so it is removed
    instead. That is the inference access links landing on routers with no
    inference bundle -- each inference host dual-homes onto four routers but
    only two of them carry inference.

    Node rows (no linkRole) always survive, so dropping a link never removes
    the node at either end.
    """
    color_map = demo_link_color_map(state, base_rows)
    out = []
    for row in base_rows:
        row = dict(row)
        role = row["linkRole"]
        if role:
            color = color_map[role]
            if state != "topology" and color == DEMO_BASE_LINK_COLOR:
                continue
            row["linkColors"] = color
        out.append(row)
    return out


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

# Preset node positions: diagonal router pairs, DC-1 left / DC-2 right,
# hosts on the flanks.
NODE_POSITIONS = {
    "dc01-trn": {"x": 50,  "y": 120},
    "dc01-inf": {"x": 50,  "y": 240},
    "r01":      {"x": 200, "y": 30},
    "r02":      {"x": 280, "y": 100},
    "r03":      {"x": 200, "y": 270},
    "r04":      {"x": 280, "y": 340},
    "r05":      {"x": 550, "y": 30},
    "r06":      {"x": 630, "y": 100},
    "r07":      {"x": 550, "y": 270},
    "r08":      {"x": 630, "y": 340},
    "dc02-trn": {"x": 760, "y": 120},
    "dc02-inf": {"x": 760, "y": 240},
}

# Floating text labels (absolute layout positions over the graph).
LABEL_FONT_SIZE = 20
PANEL_BACKGROUND = "#212529"

# Graph block geometry. Height is full when the graph owns the tab and shorter
# when it shares the tab with the traffic panel; LABEL_OVERLAYS carries one
# tuned position set per height. The padding values must match the
# nodeHorizontal/VerticalPadding viz options or _graph_fit() will be wrong.
GRAPH_WIDTH = 1200
GRAPH_HORIZONTAL_PADDING = 40
GRAPH_VERTICAL_PADDING = 20
GRAPH_FULL_HEIGHT = 700
GRAPH_LAYOUT_HEIGHT = 480
GRAPH_CHART_GAP = 20
# Demo tab 3 only: shorter than the main dashboard so the graph and the whole
# traffic panel fit without scrolling (420 + 20 + 240 = 680).
DEMO_TRAFFIC_GRAPH_HEIGHT = 420
DEMO_TRAFFIC_CHART_HEIGHT = 240
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
# Only scale-across fabric links belong here: host access links carry the
# offered load rather than the fabric's response to it, and RR uplinks carry
# control plane only. The panel is about how SRv6-TE spreads load across the
# fabric. Excluded names are derived from the configs by
# non_fabric_interfaces() rather than hardcoded, so re-cabling keeps the filter
# correct.
LINK_TRAFFIC_SPL_TEMPLATE = """
| mstats latest(_value) AS octets
  WHERE index=mdt_metrics
    metric_name="openconfig-interfaces:interfaces/interface.state/counters/out_octets"
    source="$router$"
  span=30s BY name
| where match(name, "^EightHundredGigE"){exclude}
| eventstats max(octets) AS peak_octets BY name
| where peak_octets > 0
| sort 0 name, _time
| streamstats current=f window=1 last(octets) AS prev_octets last(_time) AS prev_time BY name
| eval secs=_time-prev_time
| eval mbps=if(isnull(prev_octets) OR secs<=0 OR octets<prev_octets, null(),
               round((octets-prev_octets)*8/(secs*1000000), 3))
| timechart span=30s limit=0 avg(mbps) BY name
""".strip()


def non_fabric_interfaces(router_ifs=None):
    """Interface names on any router that are not scale-across fabric links.

    Fabric links are described "to <peer> link<N>". The two exceptions are host
    access links, "<host>-l<pair_index>" (see resolve_link_ifname), and the
    route-reflector uplinks, "to <rr>" with no link suffix.
    """
    router_ifs = router_ifs if router_ifs is not None else load_router_interfaces()
    names = set()
    for ifaces in router_ifs.values():
        for ifname, desc in ifaces.items():
            host = re.match(r"(.+)-l\d+$", desc)
            if host and host.group(1) in HOST_LINK_HOSTS:
                names.add(ifname)
                continue
            rr = re.match(r"to (\S+)$", desc)
            if rr and rr.group(1) in ROUTE_REFLECTORS:
                names.add(ifname)
    return sorted(names)


def link_traffic_spl(router_ifs=None):
    """Traffic-panel SPL with everything but fabric links filtered out."""
    excluded = non_fabric_interfaces(router_ifs)
    exclude = ""
    if excluded:
        exclude = ('\n    AND NOT match(name, "^(' + "|".join(excluded) + ')$")')
    return LINK_TRAFFIC_SPL_TEMPLATE.format(exclude=exclude)


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
    return GRAPH_LAYOUT_HEIGHT + GRAPH_CHART_GAP


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
            "query": link_traffic_spl(),
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
    chart_pos = {"x": 0, "y": chart_y, "w": GRAPH_WIDTH, "h": TRAFFIC_CHART_HEIGHT}
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
        "nodeHorizontalPadding": GRAPH_HORIZONTAL_PADDING,
        "nodeIconColors": "> primary | seriesByName('nodeIconColors')",
        "nodeIcons": "> primary | seriesByName('nodeIcons')",
        "nodeSize": "> primary | seriesByName('nodeSize')",
        "nodeTextFontSize": 12,
        "nodeTexts": "> primary | seriesByName('nodeTexts')",
        "nodeVerticalPadding": GRAPH_VERTICAL_PADDING,
        "showDirection": "none",
        "showZoomControls": True,
        "tooltipHeaderField": "> primary | seriesByName('source')",
    })
    opts.pop("nodeIds", None)


# Text overlays are separate blocks in the absolute layout, so they do not
# follow the graph when its height changes. Each label therefore carries a
# position per graph height, tuned by eye. Heights with no entry are derived
# from the nearest one via _rescale_label(); add an entry here to pin a height
# instead of deriving it.
LABEL_OVERLAYS = [
    {
        "id": "viz_label_dc0",
        "text": "DC-1",
        "positions": {
            GRAPH_FULL_HEIGHT:   {"x": 60, "y": 340, "w": 60, "h": 32},
            GRAPH_LAYOUT_HEIGHT: {"x": 80, "y": 220, "w": 60, "h": 32},
        },
    },
    {
        "id": "viz_label_dc1",
        "text": "DC-2",
        "positions": {
            GRAPH_FULL_HEIGHT:   {"x": 1090, "y": 340, "w": 80, "h": 32},
            GRAPH_LAYOUT_HEIGHT: {"x": 1080, "y": 220, "w": 80, "h": 32},
        },
    },
    {
        "id": "viz_label_plane0",
        "text": "Scale Across Plane-1",
        "positions": {
            GRAPH_FULL_HEIGHT:   {"x": 520, "y": 80, "w": 210, "h": 32},
            GRAPH_LAYOUT_HEIGHT: {"x": 480, "y": 20, "w": 210, "h": 32},
        },
    },
    {
        "id": "viz_label_plane1",
        "text": "Scale Across Plane-2",
        "positions": {
            GRAPH_FULL_HEIGHT:   {"x": 520, "y": 420, "w": 210, "h": 32},
            GRAPH_LAYOUT_HEIGHT: {"x": 540, "y": 440, "w": 210, "h": 32},
        },
    },
]


def _graph_fit(height):
    """Where the network graph lands its node bbox inside a GRAPH_WIDTH block.

    The viz fits the drawing to the block preserving aspect ratio, so there is
    a crossover: below roughly 525px it is height-limited (fills the height,
    letterboxed left and right) and above it is width-limited. Returns
    (scale, origin_x, origin_y) for the node coordinate space.
    """
    xs = [p["x"] for p in NODE_POSITIONS.values()]
    ys = [p["y"] for p in NODE_POSITIONS.values()]
    bbox_w = max(xs) - min(xs)
    bbox_h = max(ys) - min(ys)
    scale = min((GRAPH_WIDTH - 2 * GRAPH_HORIZONTAL_PADDING) / bbox_w,
                (height - 2 * GRAPH_VERTICAL_PADDING) / bbox_h)
    return scale, (GRAPH_WIDTH - bbox_w * scale) / 2, (height - bbox_h * scale) / 2


def _rescale_label(pos, from_height, to_height):
    """Move a label tuned at one graph height to another, about its center.

    Only trustworthy between heights on the same side of the _graph_fit()
    crossover. Across it the drawing switches which axis constrains it and the
    labels need re-tuning by eye — pin them in LABEL_OVERLAYS instead.
    """
    scale_from, ox_from, oy_from = _graph_fit(from_height)
    scale_to, ox_to, oy_to = _graph_fit(to_height)
    k = scale_to / scale_from
    cx = ox_to + (pos["x"] + pos["w"] / 2 - ox_from) * k
    cy = oy_to + (pos["y"] + pos["h"] / 2 - oy_from) * k
    return {"x": round(cx - pos["w"] / 2), "y": round(cy - pos["h"] / 2),
            "w": pos["w"], "h": pos["h"]}


def _label_position(spec, graph_height):
    """Overlay position for a graph height, tuned if known else derived."""
    positions = spec["positions"]
    if graph_height in positions:
        return positions[graph_height]
    nearest = min(positions, key=lambda h: abs(h - graph_height))
    return _rescale_label(positions[nearest], nearest, graph_height)


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
            "nodeHorizontalPadding": GRAPH_HORIZONTAL_PADDING,
            "nodeIconColors": "> primary | seriesByName('nodeIconColors')",
            "nodeIcons": "> primary | seriesByName('nodeIcons')",
            "nodeSize": "> primary | seriesByName('nodeSize')",
            "nodeTextFontSize": 12,
            "nodeTexts": "> primary | seriesByName('nodeTexts')",
            "nodeVerticalPadding": GRAPH_VERTICAL_PADDING,
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


def _graph_layout_structure(graph_viz_id, include_traffic_panel=False,
                            graph_height=None, chart_height=None):
    if graph_height is None:
        graph_height = GRAPH_LAYOUT_HEIGHT if include_traffic_panel else GRAPH_FULL_HEIGHT
    if chart_height is None:
        chart_height = TRAFFIC_CHART_HEIGHT
    structure = [
        {
            "item": graph_viz_id,
            "position": {"h": graph_height, "w": GRAPH_WIDTH, "x": 0, "y": 0},
            "type": "block",
        },
    ]
    for spec in LABEL_OVERLAYS:
        pos = _label_position(spec, graph_height)
        structure.append({
            "item": spec["id"],
            "position": {"h": pos["h"], "w": pos["w"], "x": pos["x"], "y": pos["y"]},
            "type": "block",
        })
    if include_traffic_panel:
        chart_y = graph_height + GRAPH_CHART_GAP
        structure.append({
            "item": TRAFFIC_CHART_VIZ_ID,
            "position": {"h": chart_height, "w": GRAPH_WIDTH, "x": 0, "y": chart_y},
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
    """Click-through demo: topology -> SRv6-TE steering -> live traffic.

    Each tab gets its own graph viz and data source so the three states can be
    screenshotted without touching anything. Tabs 2 and 3 render an identical
    graph; only the traffic panel is added.
    """
    import json

    visualizations = {}
    for spec in LABEL_OVERLAYS:
        visualizations[spec["id"]] = _text_overlay_viz(spec)

    data_sources = {}
    layout_definitions = {}
    tab_items = []

    for state in DEMO_STATES:
        with_traffic = state == DEMO_TRAFFIC_STATE
        viz_id = f"viz_topology_{state}"
        ds_id = f"ds_topology_{state}"
        layout_id = f"layout_{state}"
        state_rows = demo_rows(base_rows, state)
        spl = emit_spl(state_rows).strip()
        link_matches = build_link_color_matches_from_map(demo_link_color_map(state, base_rows))
        visualizations[viz_id] = _topology_graph_viz(
            viz_id, ds_id, link_matches, DEMO_TAB_LABELS[state]
        )
        data_sources[ds_id] = {
            "name": f"topology_{state}",
            "options": {"query": spl},
            "type": "ds.search",
        }
        layout_definitions[layout_id] = {
            "structure": _graph_layout_structure(
                viz_id,
                include_traffic_panel=with_traffic,
                graph_height=DEMO_TRAFFIC_GRAPH_HEIGHT if with_traffic else None,
                chart_height=DEMO_TRAFFIC_CHART_HEIGHT if with_traffic else None,
            ),
            "type": "absolute",
        }
        tab_items.append({"label": DEMO_TAB_LABELS[state], "layoutId": layout_id})

    # The traffic panel is placed on the last tab's layout only, but its viz,
    # data source and $router$ default are dashboard-scoped.
    visualizations[TRAFFIC_CHART_VIZ_ID] = _link_traffic_chart_viz()
    data_sources[TRAFFIC_DS_ID] = _link_traffic_data_source()

    dash = {
        "title": title,
        "description": "Click-through demo: topology → SRv6-TE steering → live traffic. "
                       "Import via Dashboard Studio → Edit → Source code.",
        # ds_link_traffic reads $global_time.*$, so this input must exist even
        # though only the third tab uses it.
        "inputs": {GLOBAL_TIME_INPUT_ID: dict(GLOBAL_TIME_INPUT)},
        "defaults": {
            "tokens": {"default": link_traffic_token_defaults(base_rows)},
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
            "layoutDefinitions": layout_definitions,
            "tabs": {"items": tab_items},
        },
    }
    return json.dumps(dash, indent=2, ensure_ascii=False) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topo", default=os.path.join(_REPO_ROOT, "topology.clab.yaml"),
                    help="containerlab topology (default: repo root topology.clab.yaml)")
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
                    help="write one 3-tab click-through demo dashboard "
                         "(topology → SRv6-TE steering → live traffic)")
    ap.add_argument("--demo-state", choices=DEMO_STATES,
                    help="apply one demo state's link colors to SPL/dashboard "
                         "output (%s)" % "|".join(DEMO_STATES))
    ap.add_argument("--patch-dashboard", metavar="FILE",
                    help="update query/colors/positions in existing dashboard JSON in place")
    args = ap.parse_args()

    node_names, links = load_topology(args.topo, exclude_rr=not args.include_rr)
    roles = {n: classify(n) for n in node_names}
    expand_links = args.expand
    edges = build_edges(links, roles, collapse=not expand_links)
    rows = build_rows(node_names, edges, roles, short_labels=args.short_labels)

    # Keep the unfiltered rows: the color map is derived from the full edge
    # list, not from whatever survived filtering.
    demo_base_rows = rows
    if args.demo_state:
        rows = demo_rows(rows, args.demo_state)

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
            link_matches = build_link_color_matches_from_map(
                demo_link_color_map(args.demo_state, demo_base_rows))
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
