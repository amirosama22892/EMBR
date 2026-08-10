# Gridworks EMBR

**Engineered Model for Buried-cable Ratings**

EMBR is an in-house ampacity calculator for underground power cables. It implements the IEC 60287 / Neher-McGrath thermal circuit methodology in a lightweight Python server (standard library plus ReportLab for PDF export) with a browser-based frontend. Three calculation engines cover MV trefoil (15, 25, and 35 kV), DC (solar/battery), and LVAC (power feeders).

EMBR is validated against Cymcap 8.1 Rev 2 across a 62-scenario matrix — 58 of 58 valid scenarios pass within ±5%, with the remaining 4 excluded due to confirmed Cymcap model errors (not EMBR errors).

---

## Quickstart

EMBR supports Python 3.7+; Python 3.12 is the tested deployment version. The calculation engines and HTTP server use only the standard library; the PDF export endpoint uses ReportLab, the sole runtime dependency.

```
pip install -r requirements.txt
python embr-server.py
```

Open [http://localhost:8080](http://localhost:8080) in any browser. The UI auto-calculates on every input change.

Run the regression and engineering verification suites before deployment:

```bash
python test_input_validation.py
python tb880_verification.py
python mv_iec_crosscheck.py
npm ci
npm test
```

> **Note:** The header logo expects `gridworks_logo.png` in the same directory as `embr-server.py`. The app works fine without it — you just won't see the logo.

---

## File Structure

```
EMBR/
├── embr-server.py                  # Python backend — all three engines + PDF report writer
├── embr.html                       # Single-file frontend (HTML/CSS/JS)
├── render.yaml                     # Render Blueprint
├── DEPLOYMENT.md                   # Render setup, smoke test, rollback, troubleshooting
├── gridworks_logo.png              # Header logo
├── favicon.ico                     # Icon for browser tab
├── requirements.txt                # Runtime dependency list
├── test_input_validation.py        # HTTP/API regression suite
├── test_frontend.js                # Browser integration suite (jsdom)
├── tb880_verification.py           # CIGRE TB 880 verification
└── mv_iec_crosscheck.py            # Independent 15/25 kV IEC cross-check
```

---

## Deploy to Render

The repository includes a Render Blueprint. In Render, choose **New > Blueprint**, connect this repository, and apply `render.yaml`. The build installs dependencies and runs all Python verification suites before Render starts the service. Render then checks `GET /healthz` before routing traffic.

No database, disk, secret, or manually configured `PORT` is required. The free instance plan is selected in the Blueprint for an easy first launch; choose an always-on paid plan if cold starts are unacceptable.

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full deployment checklist, manual setup values, smoke tests, access considerations, environment variables, troubleshooting, and rollback steps.

---

## Calculation Engines

### MV Trefoil (15 / 25 / 35 kV)

Covers 15, 25, and 35 kV MV-105 TR-XLPE aluminum cables in touching trefoil, direct burial or in conduit. The 15/25 kV libraries use Southwire SPEC 81102 / 81142 constructions; the 35 kV library uses Priority Wire #4070-17. Operating voltage is user-selectable and cannot exceed the selected cable's insulation class.

Thermal model:

- Conductor AC resistance with IEC 60287-1-1 skin and proximity effects
- Concentric neutral shield loss (λ₁) with ICEA S-94-649 helical lay correction, capped at literature values (6% for 1/3 CN, 3% for 1/6 CN)
- Dielectric loss per IEC 60287
- T1 (insulation), T3 (jacket) from cable geometry
- T4 (external) with two-zone dryout model: Neher-McGrath equivalent radius for a 2 ft × 2 ft backfill envelope, separate dry and native soil resistivities
- Geometric path-fraction mutual heating: the image-method ray is split at the rectangular backfill boundary (D_exit ≈ Y/2 = 305 mm for touching trefoil) rather than the NM equivalent circle, so the inner zone uses backfill ρ and the outer zone uses native ρ
- Separate backfill per circuit: for multi-circuit direct burial, uses the half-diagonal (431 mm) as the self-T4 boundary instead of the NM equivalent circle (670 mm), matching Cymcap "Multiple Ductbanks/Backfills"
- NM cyclic Dx split: T4_self_eff = T4_transient + μ × T4_steady, where μ = 0.3·LF + 0.7·LF² (IEC daily loss factor)
- Inter-circuit mutual heating with centre-circuit model and loss factor μ
- Conduit installations: IEC duct air gap + duct wall thermal resistance, with LF² applied only to the soil portion of T4

Fixed parameters: max conductor temperature 90°C, voltage 34.5 kV, frequency 60 Hz, 5% ampacity buffer.

### DC

Covers DC circuits (solar strings, battery feeders) using single conductors in conduit or direct burial. Conductor sizes 4/0 AWG through 1000 kcmil in copper or aluminum, with THWN, XHHW-2, USE-2, or PV Wire 2 kV insulation.

Thermal model:

- DC resistance from NEC Table 8 at 20°C reference (corrected from the 75°C values published in the table)
- No skin, proximity, dielectric, or shield losses
- Per-cable eccentric air gap model for conduit installations: individual cable positions within the duct, eccentric annular convection per Raithby & Hollands (1975) / Kuehn & Goldstein (1976)
- IEC Deq factors for cable groups in conduit (1.65d for 2 cables, 2.15d for 3, 2.50d for 4, 2.45d for 6)
- 1–3 circuits per conduit (2, 4, or 6 cables)
- Direct burial: image-method mutual heating between touching cables

### LVAC

Covers low-voltage AC feeder circuits with configurable phase topology: single-phase (2 cables), split-phase (3 cables), three-phase delta (3 cables), or three-phase wye (4 cables with adjustable neutral loading factor).

Same cable library and thermal model as the DC engine, plus:

- IEC 60287-1-1 skin and proximity effects at 60 Hz
- Neutral cable mutual heating contribution scaled by neutral loading factor

---

## API Reference

The server exposes two POST endpoints. Both accept and return JSON (except PDF export, which returns binary).

### `POST /api/calculate`

Runs an ampacity calculation. The frontend calls this on every input change.

**Common parameters (all engines):**

| Parameter | Type | Description |
|---|---|---|
| `systemType` | string | `"mv"`, `"dc"`, or `"lvac"` |
| `installType` | string | `"direct"` or `"conduit"` |
| `burialDepth` | number | Burial depth in inches |
| `soilTemp` | number | Ambient soil temperature in °C |
| `soilRhoNative` | number | Native soil resistivity in K·m/W (frontend sends C·cm/W ÷ 100) |
| `soilRhoDry` | number | Dryout zone resistivity in K·m/W |
| `useDryout` | boolean | Whether to apply two-zone dryout model |
| `loadFactor` | number | Load factor 0.1–1.0 |
| `numCircuits` | number | Number of parallel circuits/conduits |
| `circuitSpacing` | number | Centre-to-centre spacing in inches (when numCircuits > 1) |
| `conduitType` | string | `"pvc40"` or `"hdpe"` (conduit only) |
| `conduitSize` | number | 0-indexed into CONDUIT_SIZES array: 0=2", 1=2.5", 2=3", 3=4", 4=5", 5=6", 6=8" |

**MV-specific:**

| Parameter | Type | Description |
|---|---|---|
| `cableSize` | string | A key from the 15, 25, or 35 kV MV library (for example `"15kv_500"`, `"25kv_750"`, or legacy 35 kV key `"500"`) |
| `voltage_kv` | number | Line-to-line operating voltage in kV; must not exceed the selected cable's voltage class |

**DC-specific:**

| Parameter | Type | Description |
|---|---|---|
| `cableSize` | string | `"4/0"` through `"1000"` |
| `material` | string | `"cu"` or `"al"` |
| `insulation` | string | `"thwn"`, `"xhhw"`, `"use2"`, or `"pv2kv"` |
| `conductorsPerConduit` | number | 1–3 (circuits per conduit; each circuit = 2 cables) |

**LVAC-specific:**

| Parameter | Type | Description |
|---|---|---|
| `cableSize` | string | `"4/0"` through `"1000"` |
| `material` | string | `"cu"` or `"al"` |
| `insulation` | string | `"thwn"`, `"xhhw"`, `"use2"`, or `"pv2kv"` |
| `phaseConfig` | string | `"single_phase"`, `"split_phase"`, `"three_phase_delta"`, or `"three_phase_wye"` |
| `neutralFactor` | number | 0.0 (balanced) to 1.0 (full load) — only for split-phase and wye |

**Response** (all engines): JSON object with `ampacity`, `ampacityRaw`, `conductorTemp`, `cableLabel`, thermal resistances (T1, T4t, T4a, T4w), and engine-specific detail fields (Rac/Rdc, ys, yp, l1, Wd, etc.).

### `POST /api/export-pdf`

Generates a one-page PDF ampacity study report.

**Request body:**

```json
{
  "params": { ... },
  "result": { ... },
  "project": {
    "projectNumber": "GW-2026-001",
    "projectName": "Solar Farm Interconnect",
    "engineerName": "J. Smith, PE"
  }
}
```

**Response:** Raw PDF bytes (`Content-Type: application/pdf`), or JSON `{"error": "..."}` on failure.

---

## Validation

### Cymcap 8.1 Rev 2

Validated against Cymcap 8.1 Rev 2 using a 62-scenario matrix covering a range of cable sizes, installation types, soil conditions, load factors, and multi-circuit configurations.

| Engine | Scenarios | Pass (±5%) | Excluded | Notes |
|---|---|---|---|---|
| MV Trefoil | 28 | 28/28 (100%) | 0 | |
| DC | 20 | 19/19 (100%) | 1 | DC-03: Cymcap uses wrong duct type |
| LVAC | 14 | 11/11 (100%) | 3 | LV-03/05/06: Cymcap model errors |
| **Total** | **62** | **58/58 (100%)** | **4** | |

The 4 excluded scenarios have confirmed Cymcap model setup errors (wrong conduit material, missing cables, wrong cable size). See `Cymcap Model Corrections.docx` for details. Once corrected and re-exported, they can be re-validated.

**Validation matrix:** `docs/EMBR_Cymcap_15-25kV_Matrix.xlsx` contains the retained validation data and the added voltage-class workbooks.

**Report:** A published summary is at `docs/EMBR_Cymcap_Validation_Report.pdf` (also linked from the app's About page).

**Unit note:** Soil resistivity in the validation matrix is in C·cm/W. EMBR internally uses K·m/W (divide by 100). The frontend performs this conversion automatically.

### CIGRE TB 880 verification

EMBR's IEC 60287 engine is additionally verified against CIGRE Technical Brochure 880 (2022), *Power cable rating examples for calculation tool verification*, which publishes fully worked IEC 60287 examples including every intermediate value. For the case studies within EMBR's single-core / trefoil / direct-buried domain, EMBR reproduces the published values to their full precision:

- **Case 4 (33 kV land cable)** — full current rating reproduced end-to-end (conductor AC resistance, dielectric loss, screen loss factor, T1–T4, and the 537.5 A rating) to within 1×10⁻⁶ %.
- **Case 1 (132 kV trefoil)** — the IEC 60287 conductor-loss core reproduced exactly. Case 1's full thermal/shield chain (laminated foil, Milliken conductor) uses constructions outside EMBR's cable families, so only its loss core is verified.

The verification is implemented as `tb880_verification.py` (run `python tb880_verification.py`) and executes in CI on every deploy. The full report is at `docs/EMBR_TB880_Verification_Report.pdf`. Only TB 880's numeric reference values are used (CIGRE copyright); the brochure text is not reproduced.

---

## Features

- **Live cross-section diagram** — SVG rendering updates on every input change, showing cable arrangement, conduit, dryout zone, burial depth, and circuit spacing
- **Soil temperature lookup** — built-in geocoded lookup using the Open-Meteo Historical Archive API (peak daily soil temperature at 28–100 cm depth over 5 years)
- **PDF export** — one-page professional report with project info, input parameters, results breakdown, installation graphic, and thermal circuit detail
- **Configuration save/load** — export and import `.json` configuration files for repeatable studies
- **Minimal dependencies** — Python standard library plus ReportLab (PDF export only); single-file server, single-file frontend. A CI guard keeps the dependency surface to an explicit allowlist.

---

## Configuration Files

EMBR configurations are saved as JSON files with the format identifier `gridworks-embr-config`. The frontend can also load legacy files with format identifiers `gridworks-amber-config` and `gridworks-ampacity-config`.

## Runtime Configuration

| Variable | Required | Description |
|---|---:|---|
| `PORT` | No | Listening port. Defaults to `8080` locally; Render supplies it automatically. |
| `EMBR_ALLOWED_ORIGIN` | No | Enables CORS for one explicit origin. Unset means same-origin only. |

`GET /healthz` returns a small JSON readiness response for platform health checks.

---

## Known Limitations

- **MV cable libraries are fixed** to the documented Southwire 15/25 kV and Priority Wire 35 kV constructions. Other MV cables require complete construction data in `MV_CABLES` and fresh engineering validation.
- **15/25 kV Cymcap validation is pending.** Those libraries pass an independent term-by-term IEC 60287 cross-check, but the published 62-scenario Cymcap matrix covers the 35 kV library. Treat 15/25 kV outputs as engineering estimates until absolute validation is complete.
- **Trefoil only** for MV — flat formation is not implemented.
- **DC/LVAC direct burial mutual heating** uses the same geometric path-fraction model as the MV engine (splitting the image-method ray at the rectangular backfill boundary). This only activates when dryout is enabled; with dryout off, a uniform native resistivity is used for the full path.
- **Conduit fill calculations** are approximate (31% for 1–2 cables, 40% for 3+). The frontend disables undersized conduit options but does not enforce NEC Chapter 9 exact fill tables.
- **No emergency or cyclic rating** — steady-state only.

---

## Changelog

### v1.2 — July 2026

- Added 15 kV and 25 kV Southwire cable libraries alongside the existing 35 kV library.
- Added selectable operating voltage with cable-class validation.
- Added an independent term-by-term IEC 60287 cross-check for the 15/25 kV rating chain.
- Preserved the validated 35 kV results; absolute Cymcap validation for the new voltage classes remains pending.

### v1.1 — July 2026

- Hardened API input validation, request body limits, error handling, static-file serving, and same-origin behavior.
- Switched to a threaded HTTP server with request timeouts.
- Added frontend integration coverage, latest-request-wins calculation updates, safer DOM rendering, and keyboard support for soil presets.
- Added the home-page screenshot carousel.

### v1.0 — 2026 (Production release)

First production-ready release; EMBR leaves beta.

- New home page and module navigation: **Calculator** (the tool), **About** (description, engines, methodology, and the Cymcap and CIGRE TB 880 validation documents), and **Release Notes** (version history).
- Independent verification against **CIGRE TB 880** added: `tb880_verification.py` reproduces the published reference values for the in-domain cases (full Case 4 rating; Case 1 loss core), runs in CI on every deploy, and is documented in `docs/EMBR_TB880_Verification_Report.pdf`.
- Version bumped to v1.0 and the BETA badge removed.

### v0.5 — June 2026

PDF report generation rewritten on ReportLab.

- Cross-section grade and depth view is now consistent across MV, DC and LVAC. The on-screen diagram's world extents use a fixed margin independent of the dry-out zone, so the grade line and depth dimension render identically whether dry-out is on or off and regardless of which tool is selected.
- Browser tab title reflects the selected tool ("MV | EMBR", "DC | EMBR", "LVAC | EMBR").
- Added IEEE 442 soil/backfill presets (Natural Sand, Silty Clay, Crushed Stone) as dropdown options directly on the Native Soil ρ and Dry-Out Zone ρ fields of each tool. The Native field offers 2%-moisture values and the Dry-Out field offers 0%-moisture values (read from IEEE 442 Fig. 2). Choosing a preset in either field auto-fills the paired field with the same soil's other value; both fields remain free-entry, and a typed custom value leaves the paired field untouched.
- Replaced the hand-rolled raw-PDF writer (`_PDFWriter`) with a thin top-left-origin canvas wrapper (`_Canvas`) over ReportLab, cutting ~700 lines of byte-level PDF code and using real font metrics instead of approximated text widths.
- Report layout is a faithful reproduction of the previous one-page study; no content or section changes.
- Installation cross-section now mirrors the on-screen interface (`drawXS` in `embr.html`) 1:1: the graphic is built in the same SVG coordinate space and uniformly scaled into the report, so proportions, scaling and element placement match the screen exactly. Cable, conductor and conduit colors are kept identical to the UI; only the background and accent colors are mapped to the print-friendly palette. The graphic box height follows the on-screen aspect ratio.
- Long parameter values (e.g. LVAC cable labels) auto-shrink to stay within their column.
- ReportLab is now a runtime dependency (PDF export only) and is imported lazily, so the calculation engines and HTTP server still run on the standard library alone; if ReportLab is absent, only PDF export is affected and it returns a clear error instead of failing to start. The CI deploy guard changed from "zero dependencies" to an explicit package allowlist so dependency creep stays a reviewed decision.
- ReportLab became the sole runtime dependency for PDF export. The calculation engines and HTTP server remain standard-library-only.

### v0.3 — May 2026 (Validated Release)

58/58 valid scenarios pass against Cymcap 8.1 Rev 2.

- NEC Table 8 DC resistance corrected from 75°C to true 20°C reference
- IEC daily loss factor μ = 0.3·LF + 0.7·LF² for all MV T4 and inter-circuit mutual
- CN shield loss λ₁ with ICEA helical lay correction and literature caps
- Geometric path-fraction mutual T4 model (D_exit ≈ Y/2 for rectangular backfill)
- NM cyclic Dx split for steady-state vs cyclic dryout boundary
- Separate backfill per circuit for multi-circuit direct burial
- Per-cable eccentric air gap model for DC/LVAC conduit installations
- IEC Deq factors for cable groups (2, 3, 4, 6 cables)
- THWN insulation thermal resistivity corrected (5.0 → 3.5 K·m/W)
- PDF export endpoint wired up
- Clean shutdown on Ctrl+C

### v0.1 — March 2026 (Initial)

Basic three-engine implementation with simplified thermal models.
