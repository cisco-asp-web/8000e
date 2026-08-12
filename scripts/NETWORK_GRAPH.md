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
python3 scripts/topo_to_spl.py --topo topology.clab.yaml \
  --dashboard scripts/topology_network_graph.dashboard.json
```

**Workaround:** skip the JSON import — create a blank Dashboard Studio dashboard,
add a Network Graph viz, paste `topology_network_graph.spl` into the **data source
search** (not the dashboard source), then wire the viz options from Step 2 below.

## Quick fix (recommended): import pre-wired dashboard

Regenerate and import the dashboard JSON (SPL + viz wiring included):

```bash
python3 scripts/topo_to_spl.py --topo topology.clab.yaml \
  -o scripts/topology_network_graph.spl \
  --dashboard scripts/topology_network_graph.dashboard.json
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

Node display names (`nodeTexts`) come from `DISPLAY_LABELS` in `topo_to_spl.py`
(e.g. `dc1-router1`, `dc2-training`). Hostnames remain in `source` for tooltips.

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
| dc00-p00-sar00 | sar_p00 | 28 |
| dc00-host00-trn | trn | 18 |

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
python3 scripts/topo_to_spl.py --topo topology.clab.yaml --short-labels \
  -o scripts/topology_network_graph.spl
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
| `training0-link0` | DC0 training → p00 sar00 |
| `training0-link1` | DC0 training → p00 sar01 |
| `training0-link2` | DC0 training → p01 sar00 |
| `training0-link3` | DC0 training → p01 sar01 |
| `inference0-link0` … `link3` | DC0 inference (same SAR order) |
| `training1-link0` … `link3` | DC1 training |
| `inference1-link0` … `link3` | DC1 inference |

Edit colors in the dashboard JSON under `linkColorsEditorConfig`, or set defaults
in `HOST_LINK_COLOR_DEFAULTS` in `topo_to_spl.py`.

## Regenerate after topology changes

```bash
python3 scripts/topo_to_spl.py --topo topology.clab.yaml \
  -o scripts/topology_network_graph.spl \
  --patch-dashboard scripts/topology_network_graph.dashboard.json
```

Use `--patch-dashboard` to refresh the embedded SPL query, link/node color
config, and node positions while preserving your layout and label tweaks.
Use `--dashboard` only for a fresh JSON from scratch.

Styling source of truth in code:
- `scripts/colors.cfg` — node palette (purple, light_blue, blue, …)
- `FABRIC_LINK_COLOR_DEFAULTS` / `HOST_LINK_COLOR_DEFAULTS` in `topo_to_spl.py` — per-link colors
- `topology_network_graph.dashboard.json` — final layout; patch merges generator updates in place

Parallel fabric links between the same SAR pair are **collapsed** into one edge
by default (`linkValues` holds the bundle member count). Fabric links use
**2×** the `linkWidths` of host access links. Pass `--expand` to emit every
physical link as its own row.

Node sizes in generated output: SAR routers 32px, hosts 30px.

Reference: [Splunk Network Graph docs](https://help.splunk.com/en/splunk-cloud-platform/create-dashboards-and-reports/dashboard-studio/10.5.2605/visualizations/network-graph)
