# Splunk Network Graph — styling guide

**Re-pasting the SPL alone does not change colors or node size.** The SPL
only supplies data columns. Dashboard Studio applies styling only when the
**Network Graph visualization options** map those columns — a separate config
from the search query.

## Troubleshooting

### "Error in 'EvalCommand': The expression is malformed"

Usually caused by bad newline escaping when the SPL is embedded in dashboard JSON
(literal `\n` text instead of real line breaks). Regenerate with:

```bash
python3 splunk/topo_to_spl.py --topo topology.clab.yaml \
  --dashboard splunk/topology_network_graph.dashboard.json
```

**Workaround:** skip the JSON import — create a blank Dashboard Studio dashboard,
add a Network Graph viz, paste `topology_network_graph.spl` into the **data source
search** (not the dashboard source), then wire the viz options from Step 2 below.

## Quick fix (recommended): import pre-wired dashboard

Regenerate and import the dashboard JSON (SPL + viz wiring included):

```bash
python3 splunk/topo_to_spl.py --topo topology.clab.yaml \
  -o splunk/topology_network_graph.spl \
  --dashboard splunk/topology_network_graph.dashboard.json
```

In Splunk:

1. **Dashboards** → **Create dashboard** → **Dashboard Studio**
2. Click **Edit** → open **Source code** (</> icon or bottom panel)
3. Select all, paste contents of `topology_network_graph.dashboard.json`
4. **Save**

You should see colored nodes with floating DC/plane labels over the graph.

## Floating labels (DC and planes)

The dashboard uses four `splunk.markdown` panels in **absolute layout**, layered
on top of the Network Graph. Use `splunk.markdown` (not `splunk.text` — that type
is not accepted by Dashboard Studio schema validation and blocks **Apply**).

| Label | Default position |
|-------|------------------|
| DC-1 | lower left (x=60) |
| DC-2 | lower right (x=1090) |
| Scale Across Plane-1 | center upper (y=80) |
| Scale Across Plane-2 | center lower (y=420) |

Node display names (`nodeTexts`) come from `DISPLAY_LABELS` in `topo_to_spl.py`.
Routers are already short so they render as their hostname (`r01` … `r08`);
only the hosts are relabelled by role (`dc01-trn` → `training`). Hostnames
remain in `source` for tooltips and for the traffic panel's `$router$` token.

Shared label styling: white bold text (`fontColor` / `customFontSize` 20),
`fontSize: "custom"`, markdown `**label**`. Label and graph panels both use
the same `backgroundColor` (`PANEL_BACKGROUND` in `topo_to_spl.py`, default
`#212529` for enterprise dark) so markdown overlays blend with the network graph
canvas instead of rendering opaque black when set to `"transparent"`.

After moving nodes, drag the markdown panels in Edit mode or adjust `position` in
`layout.layoutDefinitions.layout_1.structure`.

## Fix an existing dashboard (manual)

### Step 1 — verify the data (not the graph yet)

Run the SPL in Search. In the results **Statistics** tab you should see:

| source | nodeRole | nodeSize |
|--------|----------|----------|
| r01 | sar_p00 | 28 |
| dc01-trn | trn | 18 |

If those columns look correct, the SPL is fine — the viz config is what's missing.

### Step 2 — edit the **visualization**, not the search

1. Open dashboard in **Edit** mode
2. Click the **Network Graph panel** itself (the graph, not the magnifying-glass search icon)
3. Scroll to **Source code** in the Configuration panel
4. Add a `context` block and update `options` under `"type": "splunk.networkGraph"`:

```json
"context": {
  "nodeColorsEditorConfig": [
    { "match": "sar_p00", "value": "#009CEB" },
    { "match": "sar_p01", "value": "#4fa484" },
    { "match": "trn", "value": "#af575a" },
    { "match": "inf", "value": "#f8be44" }
  ]
},
"options": {
  "nodeColorValues": "> primary | seriesByName('nodeRole')",
  "nodeColors": "> nodeColorValues | matchValue(nodeColorsEditorConfig)",
  "nodeSize": "> primary | seriesByName('nodeSize')",
  "nodeTexts": "> primary | seriesByName('nodeTexts')",
  "nodeIcons": "> primary | seriesByName('nodeIcons')",
  "nodeIconColors": "> primary | seriesByName('nodeIconColors')",
  "linkColorValues": "> primary | seriesByName('linkRole')",
  "linkColors": "> linkColorValues | matchValue(linkColorsEditorConfig)",
  "linkWidth": "> primary | seriesByName('linkWidths')"
}
```

5. **Save** the dashboard (browser refresh alone is not enough)

### Step 3 — UI alternative for colors only

Configuration → **Color and style** → **Node color data**:

1. Select **Dynamic coloring** (not Static)
2. Field: `nodeRole`
3. Match method: **Matches** — map `sar_p00`, `sar_p01`, `trn`, `inf` to hex colors

Node size has **no UI field picker**; use `"nodeSize": 24` (static) or
`"> primary | seriesByName('nodeSize')"` in the Source editor.

## Smaller nodes

| Approach | Source editor value |
|----------|---------------------|
| All nodes same size | `"nodeSize": 24` |
| Per-node from SPL | `"nodeSize": "> primary | seriesByName('nodeSize')"` |
| Scale from nodeValues | `"nodeSize": "> primary | seriesByName('nodeValues') \| lerp(lerpConfigNode)"` + context `{ "outputMin": 16, "outputMax": 32 }` |

Default is 48 px. Range: 8–200 px.

## Node label font size

Splunk's Network Graph has no documented UI control for label font size. Try
adding this in the viz Source editor `options`:

```json
"nodeTextFontSize": 10
```

If that has no effect in your Splunk version (undocumented / version-dependent),
use shorter display labels while keeping full hostnames in tooltips:

```json
"tooltipHeaderField": "> primary | seriesByName('source')"
```

Regenerate SPL with compact labels:

```bash
python3 splunk/topo_to_spl.py --topo topology.clab.yaml --short-labels \
  -o splunk/topology_network_graph.spl
```

Short label examples: `d0/p0/s0`, `d0/trn`, `d1/inf`. Hover a node to see the
full hostname from the `source` field.

## Icons

| Role | Built-in icon | Notes |
|------|---------------|-------|
| SAR routers | `networkDevice` | Closest built-in to a router |
| Hosts (trn/inf) | `servers` | Same rack icon for both |

**Cisco criss-cross router arrows:** not in Splunk's 74 built-in icons. Closest
alternatives: `networkDevice`, `nodeTopology`, `arrowsFourRightLeftUpDown`.

**Custom icon:** Network Graph supports remote URLs or SVG data URIs per node.
Host a Cisco router SVG and set `nodeIcons` in SPL to the URL/data-URI string
for SAR rows only. See [Splunk Network Graph docs — Node icon support](https://help.splunk.com/en/splunk-cloud-platform/create-dashboards-and-reports/dashboard-studio/10.5.2605/visualizations/network-graph).

Built-in icon names include: `networkDevice`, `servers`, `nodeTopology`,
`arrowsFourRightLeftUpDown`, `arrowsFourTowardUp`, `ethernetPort`, etc.

## Color legend

| Role | Color | Example nodes |
|------|-------|---------------|
| Plane-0 SAR | `#009CEB` | `dc0x-p00-sar0x` |
| Plane-1 SAR | `#4fa484` | `dc0x-p01-sar0x` |
| Training host | `#af575a` | `*-trn` |
| Inference host | `#f8be44` | `*-inf` |

**Fabric link colors (SRv6-TE ODN):** each scale-across bundle has its own
`linkRole` — `plane0-link0` … `plane0-link3` (Plane-1 / p00) and
`plane1-link0` … `plane1-link3` (Plane-2 / p01). Bundle order matches
`topology.clab.yaml`. Defaults: link0–2 training color, link3 inference color
(75/25 ODN split). Customize in `linkColorsEditorConfig` or
`FABRIC_LINK_COLOR_DEFAULTS` in `topo_to_spl.py`.

**Host link colors:** each host access link gets its own `linkRole` so you can
color them individually in `linkColorsEditorConfig`:

| Role pattern | Endpoint |
|--------------|----------|
| `training0-link0` | `dc01-trn` → `r01` (Plane-1) |
| `training0-link1` | `dc01-trn` → `r02` (Plane-1) |
| `training0-link2` | `dc01-trn` → `r03` (Plane-2) |
| `training0-link3` | `dc01-trn` → `r04` (Plane-2) |
| `inference0-link0` … `link3` | `dc01-inf` (same router order) |
| `training1-link0` … `link3` | `dc02-trn` → `r05` … `r08` |
| `inference1-link0` … `link3` | `dc02-inf` → `r05` … `r08` |

The trailing index is `plane * 2 + pair`, derived from the `ROUTERS` table in
`topo_to_spl.py` rather than from the hostname.

Edit colors in the dashboard JSON under `linkColorsEditorConfig`, or set defaults
in `HOST_LINK_COLOR_DEFAULTS` in `topo_to_spl.py`.

## Click-through demo dashboard

`splunk/topology_network_graph.demo.json` is a separate, self-contained
dashboard with three tabs meant to be screenshotted in order. Regenerate with:

```bash
python3 splunk/topo_to_spl.py --topo topology.clab.yaml \
  --demo-dashboard splunk/topology_network_graph.demo.json
```

| Tab | Links | Traffic panel |
|-----|-------|---------------|
| 1 — Topology | all 24, neutral gray `#bdbdbc` | no |
| 2 — SRv6-TE steering | 20, colored by traffic class | no |
| 3 — SRv6-TE + live traffic | identical to tab 2 | yes |

On tab 2 the link colors match the **host node** they serve, so the eye can
follow host → access link → WAN bundle without a legend:

| | Color | Links |
|---|---|---|
| Training | `#a873dd` (matches `*-trn` nodes) | 8 access links + 6 of 8 WAN bundles |
| Inference | `#7dcff2` (matches `*-inf` nodes) | 4 access links + 2 of 8 WAN bundles |

The WAN is dual-plane with four collapsed router-to-router edges per plane,
each edge representing a 4×800G ECMP bundle drawn as a single line. Tabs 2 and
3 share one color map by construction, so the graph cannot drift between the
two screenshots.

### Which bundles carry inference

`DEMO_INFERENCE_BUNDLES` names them by **router pair**, not by link index, so
the steered path reads straight off the table:

```python
DEMO_INFERENCE_BUNDLES = (("r03", "r07"), ("r04", "r08"))
```

Both sit in Plane-2, giving two disjoint inference paths — `dc01-inf → r03 →
r07 → dc02-inf` and `dc01-inf → r04 → r08 → dc02-inf`. Every bundle not listed
carries training, so the 75/25 split (2 of 8) falls out of the table rather
than being configured separately. `FABRIC_ODN_TRAINING_BUNDLES` is no longer
involved on the demo path; it still drives the *main* dashboard's index-based
fallback in `link_color_for_role()`.

A host access link takes its class color only if the router it lands on
actually terminates a bundle of that class, so an access link can never imply a
steered path that does not exist. Every router carries at least one training
bundle, so all eight training access links stay colored — that falls out of the
same rule rather than being a special case.

### Unsteered links are dropped, not grayed

On tabs 2 and 3 a link in neither class is **removed from the graph**, not
drawn neutral. Alongside colored links a gray one reads as "this path is down"
rather than "this path is not part of the story". That removes exactly four
links: each inference host dual-homes onto four routers but only two of them
carry an inference bundle, so `dc01-inf → r01`, `dc01-inf → r02`,
`dc02-inf → r05` and `dc02-inf → r06` disappear. Tab 1 keeps all 24, since it
is the fabric as built.

Node rows survive filtering regardless, so dropping a link never removes the
node at either end. All of this is derived from the edge list at generation
time, so re-pointing a bundle in `topology.clab.yaml` or editing
`DEMO_INFERENCE_BUNDLES` keeps both the colors and the dropped set honest.

Colors come from `DEMO_BASE_LINK_COLOR` / `DEMO_TRAINING_COLOR` /
`DEMO_INFERENCE_COLOR` in `topo_to_spl.py`; the latter two are read from
`ROLE_STYLE` so they track the host node colors automatically.

Note the time range picker is a global input and therefore shows on all three
tabs, even though only tab 3 needs it — `ds_link_traffic` reads
`$global_time.*$` and Dashboard Studio will not render the panel without it.

### Tab heights

Tab 3 budgets 680px so the graph and the whole traffic panel fit without
scrolling: a 420px graph (`DEMO_TRAFFIC_GRAPH_HEIGHT`), a 20px gap
(`GRAPH_CHART_GAP`), and a 240px panel (`DEMO_TRAFFIC_CHART_HEIGHT`). These
constants are demo-only; the main dashboard keeps its 480 + 20 + 280 layout.

### Text overlay positions

The "Scale Across Plane-N" and "DC-1/DC-2" labels are separate blocks in the
absolute layout, so they do not follow the graph when its height changes.
Each entry in `LABEL_OVERLAYS` therefore carries a `positions` map keyed by
graph height, tuned by eye:

- `GRAPH_FULL_HEIGHT` (700) — graph alone, tabs 1 and 2
- `GRAPH_LAYOUT_HEIGHT` (480) — graph above the traffic panel, main dashboard

Heights with no entry are derived from the nearest one by `_rescale_label()`,
which is how tab 3's 420px positions are produced. That derivation is only
valid within one fit regime. `_graph_fit()` shows why: the viz scales its
drawing to the block preserving aspect ratio, and with a 710×310 node bounding
box the crossover sits near 525px. Below it the graph is height-limited (fills
the height, letterboxed left and right, so shrinking the height narrows it
too); above it is width-limited. 420 and 480 are both height-limited, so
scaling between them holds; scaling from either to 700 does not, which is why
700 is pinned explicitly.

To nudge a derived height, add an explicit entry for it under `positions` —
that pins it and skips the derivation. Note `GRAPH_HORIZONTAL_PADDING` /
`GRAPH_VERTICAL_PADDING` feed both `_graph_fit()` and the viz's
`nodeHorizontal/VerticalPadding` options, so they cannot drift apart.

Adjust values in `topo_to_spl.py` and regenerate; editing the JSON directly is
lost on the next run. `--patch-dashboard` deliberately leaves label positions
alone, so hand-tuning the main dashboard in Splunk survives.

## Traffic panel: which interfaces appear

Clicking a node charts `out_octets` for that router, one series per interface.
Two filters narrow it to the fabric:

| Filter | Drops |
|--------|-------|
| `peak_octets > 0` | Configured but shut ports (they report 0 forever) |
| `NOT match(name, ...)` | Everything that is not a fabric link — currently `.../16`, `.../17` (host access) and `.../32` (RR uplink) |

Only scale-across fabric links are charted. Host access links carry the offered
load rather than the fabric's response to it, and RR uplinks carry control
plane only; neither says anything about how SRv6-TE spread the traffic.

The excluded names are derived at generation time by `non_fabric_interfaces()`
in `topo_to_spl.py`, which classifies by interface description:

| Description | Kind |
|-------------|------|
| `to <peer> link<N>` | fabric — charted |
| `<host>-l<N>` | host access — excluded |
| `to <rr>` | route reflector — excluded |

So re-cabling keeps the filter correct; just regenerate. To chart everything
again, drop the `AND NOT match(...)` line from `LINK_TRAFFIC_SPL_TEMPLATE`.

## Regenerate after topology changes

```bash
python3 splunk/topo_to_spl.py --topo topology.clab.yaml \
  -o splunk/topology_network_graph.spl \
  --patch-dashboard splunk/topology_network_graph.dashboard.json
```

Use `--patch-dashboard` to refresh the embedded SPL query, link/node color
config, and node positions while preserving your layout and label tweaks.
Use `--dashboard` only for a fresh JSON from scratch.

Styling source of truth in code:
- `splunk/colors.cfg` — node palette (purple, light_blue, blue, …)
- `FABRIC_LINK_COLOR_DEFAULTS` / `HOST_LINK_COLOR_DEFAULTS` in `topo_to_spl.py` — per-link colors
- `topology_network_graph.dashboard.json` — final layout; patch merges generator updates in place

Parallel fabric links between the same SAR pair are **collapsed** into one edge
by default (`linkValues` holds the bundle member count). Fabric links use
**2×** the `linkWidths` of host access links. Pass `--expand` to emit every
physical link as its own row.

Node sizes in generated output: SAR routers 32px, hosts 30px.

Reference: [Splunk Network Graph docs](https://help.splunk.com/en/splunk-cloud-platform/create-dashboards-and-reports/dashboard-studio/10.5.2605/visualizations/network-graph)
