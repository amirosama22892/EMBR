#!/usr/bin/env python3
"""
Gridworks EMBR — Engineered Model for Buried-cable Ratings
====================================================================
Multi-system: MV Trefoil, DC, LVAC
IEC 60287 / Neher-McGrath methodology

v1.2 (July 2026):
  Added 15 kV and 25 kV cable libraries (Southwire SPEC 81102 / 81142, 100% IL
  TR-XLPE, 1/3 CN) and a selectable line-to-line operating voltage driving
  dielectric loss. Validation blocks a cable rated below the operating voltage.
  35 kV results unchanged; 15/25 kV verified by an independent IEC 60287
  cross-check (mv_iec_crosscheck.py, in CI); Cymcap validation still pending.

v1.1 — Patch (July 2026):
  Patched input verification bugs; same-origin serving; frontend robustness
  and accessibility fixes; home-page screenshot carousel. Calculation engine
  unchanged (TB 880 / Cymcap validation still current).

v1.0 — Validated release (May 2026):
  58/58 scenarios validated against Cymcap 8.1 Rev 2 (±5% or better).
  MV Trefoil 28/28, DC 19/19, LVAC 11/11.

  Key improvements over v0.1:
  - DC/LVAC conductor resistance: NEC Table 8 values corrected from
    75°C to true 20°C reference (were 21.6% too high for Cu, 22.2% for Al).
  - Loss factor: IEC daily μ = 0.3·LF + 0.7·LF² for all MV T4 and
    inter-circuit mutual; duct air and wall see peak temperature.
  - CN shield loss (λ₁): helical lay correction per ICEA S-94-649,
    capped at literature values (6% for 1/3 CN, 3% for 1/6 CN).
  - MV backfill mutual heating: geometric path-fraction model splits
    image-method ray at rectangular backfill boundary (D_exit ≈ Y/2).
  - NM cyclic Dx split: T4_self_eff = T4_trans + μ·T4_steady for
    separate treatment of steady-state vs cyclic dryout boundary.
  - Separate backfill per circuit for multi-circuit direct burial,
    matching Cymcap "Multiple Ductbanks/Backfills" installation type.
  - DC/LVAC air gap: IEC 60287-2-1 simplified group air-gap model
    (Table 4). A per-cable eccentric model is present but disabled,
    pending re-validation (see the DC/LVAC air-gap note below).

Dependencies — Python standard library, plus ReportLab for PDF report
generation (see requirements.txt). The calculation engines and HTTP server
are stdlib-only; only the PDF export endpoint requires ReportLab.
Run:  python embr-server.py
Open: http://localhost:8080
"""

import io
import json
import math
import os
import sys
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

# Make vendored dependencies importable when present. The deploy workflow
# installs requirements into a local "vendor/" directory that ships with the
# app (see .github/workflows/deploy.yml), so ReportLab is available on hosts
# where packages are not otherwise installed. No effect for local development
# or build-based deploys, where the packages are already on sys.path.
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_APP_DIR, "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

# =============================================================================
# CONSTANTS
# =============================================================================

# Dry-out zone dimensions (default 2'x2' square box)
DRYOUT_X_M = 0.6096   # short dimension, meters (2 ft)
DRYOUT_Y_M = 0.6096   # long dimension, meters (2 ft)

# Neher-McGrath cyclic rating: thermal penetration depth (Dx)
# Soil within Dx responds to instantaneous (peak) cable load;
# soil beyond Dx only sees time-averaged losses (multiplied by mu).
# Dx = 1.02 * sqrt(86400 * delta), delta = thermal diffusivity [m^2/s].
# NM standard delta = 0.5e-6 m/s gives Dx ~ 212 mm, matching Cymcap 8.1.
NM_THERMAL_DIFFUSIVITY = 0.5e-6   # m^2/s, Neher-McGrath default
NM_DX = 1.02 * math.sqrt(86400 * NM_THERMAL_DIFFUSIVITY)  # ~0.212 m

# Separate-backfill model: half-diagonal of the rectangular backfill zone.
# When each circuit has its own backfill trench (Cymcap "Multiple
# Ductbanks/Backfills"), the effective boundary distance for the self-
# heating two-zone split is the half-diagonal of the rectangle, not the
# NM equivalent circle.  For a 2 ft × 2 ft square the half-diagonal is
# 431 mm vs the NM circle of 670 mm — matching Cymcap's effective ρ_Ts
# within 1.5 % across all multi-circuit direct-burial scenarios.
BACKFILL_HALF_DIAG = math.sqrt((DRYOUT_X_M / 2) ** 2 + (DRYOUT_Y_M / 2) ** 2)


def _neher_mcgrath_rb(x, y):
    """Neher-McGrath (1957) Appendix II, equations 58-60.
    Computes the equivalent thermal radius rb for a rectangular
    envelope of dimensions x by y (same units returned).
    
    The method places rb between the inscribed circle (r1 = x/2)
    and the circumscribing circle (r2 = sqrt(x^2+y^2)/2), weighted
    by the fraction of the annular area occupied by the rectangle.
    """
    r1 = x / 2.0                                    # eq. 58
    r2 = math.sqrt(x * x + y * y) / 2.0             # eq. 59
    if r2 <= r1 or r1 <= 0:
        return r1
    area_bank = x * y
    area_r1 = math.pi * r1 * r1
    area_r2 = math.pi * r2 * r2
    ring = area_r2 - area_r1
    if ring <= 0:
        return r1
    frac = (area_bank - area_r1) / ring              # eq. 60 proportion
    # log(rb) = log(r1) + frac * log(r2/r1)
    rb = r1 * (r2 / r1) ** frac
    return rb

# =============================================================================
# MV CABLE LIBRARY — Priority Wire #4070-17
# 35kV MV-105 TR-XLPE, 100% IL, Aluminum, XLPE Jacket
# Dimensions: Priority Wire datasheet #4070-17 (08-2024)
# Conductor Rdc: ASTM B400-23 Table 1 × 1.02 lay factor (Neher-McGrath eq. 10)
# Conductor diameters: ASTM B400-23 Table 1 (4/0-1000), PW datasheet (1250-1500)
# =============================================================================
MV_CABLES = {
    "4/0": {
        "label": "4/0 AWG AL (1/2 CN)", "conductor_diameter_mm": 12.07,
        "insulation_diameter_mm": 32.26, "cn_wires": 13, "cn_wire_diameter_mm": 2.59,
        "cn_wire_r_dc_20": 3.277e-3, "jacket_thickness_mm": 1.52,
        "overall_diameter_mm": 44.70, "r_dc_20": 2.7441e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/2",
    },
    "500": {
        "label": "500 kcmil AL (1/3 CN)", "conductor_diameter_mm": 18.69,
        "insulation_diameter_mm": 38.86, "cn_wires": 16, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 51.31, "r_dc_20": 1.1612e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3",
    },
    "750": {
        "label": "750 kcmil AL (1/3 CN)", "conductor_diameter_mm": 23.06,
        "insulation_diameter_mm": 43.43, "cn_wires": 24, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 56.64, "r_dc_20": 7.7303e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3",
    },
    "1000": {
        "label": "1000 kcmil AL (1/6 CN)", "conductor_diameter_mm": 26.92,
        "insulation_diameter_mm": 47.24, "cn_wires": 16, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 60.71, "r_dc_20": 5.7894e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/6",
    },
    "1250": {
        "label": "1250 kcmil AL (1/6 CN)", "conductor_diameter_mm": 31.75,
        "insulation_diameter_mm": 53.09, "cn_wires": 20, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 66.29, "r_dc_20": 4.6181e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/6",
    },
    "1500": {
        "label": "1500 kcmil AL (1/6 CN)", "conductor_diameter_mm": 34.80,
        "insulation_diameter_mm": 56.13, "cn_wires": 24, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 69.34, "r_dc_20": 3.8819e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/6",
    },
    # ---- Southwire 15 kV & 25 kV, 100% IL TR-XLPE, 1/3 CN (SPEC 81102 / 81142) ----
    "15kv_4/0": {
        "label": "15 kV 4/0 AWG AL (1/3 CN)", "conductor_diameter_mm": 12.65,
        "insulation_diameter_mm": 24.51, "cn_wires": 11, "cn_wire_diameter_mm": 1.63,
        "cn_wire_r_dc_20": 8.283e-3, "jacket_thickness_mm": 1.27,
        "overall_diameter_mm": 30.3, "r_dc_20": 2.7441e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 15,
    },
    "15kv_250": {
        "label": "15 kV 250 kcmil AL (1/3 CN)", "conductor_diameter_mm": 14.17,
        "insulation_diameter_mm": 26.26, "cn_wires": 13, "cn_wire_diameter_mm": 1.63,
        "cn_wire_r_dc_20": 8.283e-3, "jacket_thickness_mm": 1.27,
        "overall_diameter_mm": 32.05, "r_dc_20": 2.3224e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 15,
    },
    "15kv_350": {
        "label": "15 kV 350 kcmil AL (1/3 CN)", "conductor_diameter_mm": 16.79,
        "insulation_diameter_mm": 29.39, "cn_wires": 18, "cn_wire_diameter_mm": 1.63,
        "cn_wire_r_dc_20": 8.283e-3, "jacket_thickness_mm": 1.27,
        "overall_diameter_mm": 35.18, "r_dc_20": 1.6589e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 15,
    },
    "15kv_500": {
        "label": "15 kV 500 kcmil AL (1/3 CN)", "conductor_diameter_mm": 20.04,
        "insulation_diameter_mm": 32.64, "cn_wires": 16, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 1.27,
        "overall_diameter_mm": 39.24, "r_dc_20": 1.1612e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 15,
    },
    "15kv_750": {
        "label": "15 kV 750 kcmil AL (1/3 CN)", "conductor_diameter_mm": 24.59,
        "insulation_diameter_mm": 37.41, "cn_wires": 24, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 45.54, "r_dc_20": 7.7303e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 15,
    },
    "15kv_1000": {
        "label": "15 kV 1000 kcmil AL (1/3 CN)", "conductor_diameter_mm": 28.37,
        "insulation_diameter_mm": 41.2, "cn_wires": 20, "cn_wire_diameter_mm": 2.59,
        "cn_wire_r_dc_20": 3.277e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 50.44, "r_dc_20": 5.7894e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 15,
    },
    "25kv_4/0": {
        "label": "25 kV 4/0 AWG AL (1/3 CN)", "conductor_diameter_mm": 12.65,
        "insulation_diameter_mm": 29.34, "cn_wires": 11, "cn_wire_diameter_mm": 1.63,
        "cn_wire_r_dc_20": 8.283e-3, "jacket_thickness_mm": 1.27,
        "overall_diameter_mm": 35.13, "r_dc_20": 2.7441e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 25,
    },
    "25kv_250": {
        "label": "25 kV 250 kcmil AL (1/3 CN)", "conductor_diameter_mm": 14.17,
        "insulation_diameter_mm": 31.09, "cn_wires": 13, "cn_wire_diameter_mm": 1.63,
        "cn_wire_r_dc_20": 8.283e-3, "jacket_thickness_mm": 1.27,
        "overall_diameter_mm": 36.88, "r_dc_20": 2.3224e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 25,
    },
    "25kv_350": {
        "label": "25 kV 350 kcmil AL (1/3 CN)", "conductor_diameter_mm": 16.79,
        "insulation_diameter_mm": 33.71, "cn_wires": 18, "cn_wire_diameter_mm": 1.63,
        "cn_wire_r_dc_20": 8.283e-3, "jacket_thickness_mm": 1.27,
        "overall_diameter_mm": 39.5, "r_dc_20": 1.6589e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 25,
    },
    "25kv_500": {
        "label": "25 kV 500 kcmil AL (1/3 CN)", "conductor_diameter_mm": 20.04,
        "insulation_diameter_mm": 36.96, "cn_wires": 16, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 45.08, "r_dc_20": 1.1612e-4, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 25,
    },
    "25kv_750": {
        "label": "25 kV 750 kcmil AL (1/3 CN)", "conductor_diameter_mm": 24.59,
        "insulation_diameter_mm": 41.73, "cn_wires": 24, "cn_wire_diameter_mm": 2.05,
        "cn_wire_r_dc_20": 5.211e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 49.86, "r_dc_20": 7.7303e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 25,
    },
    "25kv_1000": {
        "label": "25 kV 1000 kcmil AL (1/3 CN)", "conductor_diameter_mm": 28.37,
        "insulation_diameter_mm": 46.28, "cn_wires": 20, "cn_wire_diameter_mm": 2.59,
        "cn_wire_r_dc_20": 3.277e-3, "jacket_thickness_mm": 2.03,
        "overall_diameter_mm": 55.52, "r_dc_20": 5.7894e-5, "alpha_20": 0.00403,
        "ks": 0.435, "kp": 0.37, "cn_fraction": "1/3", "voltage_class_kv": 25,
    },
}

# =============================================================================
# DC / LV CABLE LIBRARY
# Single conductors: THWN/THHN, XHHW-2, USE-2/RHW-2
# Sizes: 4/0 AWG through 1000 kcmil, copper and aluminum
#
# Dimensions from NEC Chapter 9 Table 5 (THWN-2/THHN) and Table 5A (XHHW)
# DC resistance from NEC Chapter 9 Table 8
# OD includes insulation, no metallic sheath or jacket (single building wire)
#
# Insulation thermal resistivity:
#   PVC (THWN/THHN): 5.0 K·m/W
#   XLPE (XHHW-2, USE-2/RHW-2): 3.5 K·m/W
# =============================================================================

# DC resistance at 20°C in Ω/m — from NEC Chapter 9 Table 8, uncoated stranded
# NEC Table 8 lists resistance at 75°C; values below are converted to 20°C:
#   R_20 = R_75 / (1 + α_20 × 55)
# Copper: ÷ 1.2162,  Aluminum: ÷ 1.2217
# Copper
DC_R_CU = {
    "4/0":  1.6402e-4,   # 0.0608 Ω/kft @75°C → 0.04999 @20°C
    "250":  1.3893e-4,   # 0.0515 Ω/kft @75°C → 0.04235 @20°C
    "300":  1.1573e-4,   # 0.0429 Ω/kft @75°C → 0.03528 @20°C
    "350":  9.9007e-5,   # 0.0367 Ω/kft @75°C → 0.03018 @20°C
    "400":  8.6597e-5,   # 0.0321 Ω/kft @75°C → 0.02639 @20°C
    "500":  6.9601e-5,   # 0.0258 Ω/kft @75°C → 0.02121 @20°C
    "600":  5.7731e-5,   # 0.0214 Ω/kft @75°C → 0.01760 @20°C
    "750":  4.6131e-5,   # 0.0171 Ω/kft @75°C → 0.01406 @20°C
    "1000": 3.4801e-5,   # 0.0129 Ω/kft @75°C → 0.01061 @20°C
}
# Aluminum
DC_R_AL = {
    "4/0":  2.6856e-4,   # 0.100  Ω/kft @75°C → 0.08186 @20°C
    "250":  2.2747e-4,   # 0.0847 Ω/kft @75°C → 0.06933 @20°C
    "300":  1.8987e-4,   # 0.0707 Ω/kft @75°C → 0.05787 @20°C
    "350":  1.6248e-4,   # 0.0605 Ω/kft @75°C → 0.04952 @20°C
    "400":  1.4207e-4,   # 0.0529 Ω/kft @75°C → 0.04330 @20°C
    "500":  1.1387e-4,   # 0.0424 Ω/kft @75°C → 0.03471 @20°C
    "600":  9.4801e-5,   # 0.0353 Ω/kft @75°C → 0.02890 @20°C
    "750":  7.5733e-5,   # 0.0282 Ω/kft @75°C → 0.02308 @20°C
    "1000": 5.6934e-5,   # 0.0212 Ω/kft @75°C → 0.01735 @20°C
}

# Temperature coefficients at 20°C
ALPHA_CU = 0.00393
ALPHA_AL = 0.00403

# Conductor diameters (mm) — compact stranded, from NEC Table 5
COND_DIA = {
    "4/0":  {"cu": 13.41, "al": 13.41},   # 0.528"
    "250":  {"cu": 14.61, "al": 14.61},
    "300":  {"cu": 15.98, "al": 15.98},
    "350":  {"cu": 17.25, "al": 17.25},
    "400":  {"cu": 18.44, "al": 18.44},
    "500":  {"cu": 20.65, "al": 20.65},
    "600":  {"cu": 22.61, "al": 22.61},
    "750":  {"cu": 25.35, "al": 25.35},
    "1000": {"cu": 29.26, "al": 29.26},
}

# Cable overall diameter (mm) by insulation type — 600V class
# From NEC Chapter 9 Table 5 (THWN-2/THHN) and Table 5A (XHHW)
CABLE_OD = {
    "4/0":  {"thwn": 17.78, "xhhw": 17.27, "use2": 19.43},
    "250":  {"thwn": 19.43, "xhhw": 18.90, "use2": 21.08},
    "300":  {"thwn": 20.83, "xhhw": 20.32, "use2": 22.48},
    "350":  {"thwn": 22.10, "xhhw": 21.59, "use2": 23.75},
    "400":  {"thwn": 23.32, "xhhw": 22.78, "use2": 24.94},
    "500":  {"thwn": 25.48, "xhhw": 24.94, "use2": 27.10},
    "600":  {"thwn": 27.74, "xhhw": 26.92, "use2": 29.36},
    "750":  {"thwn": 30.23, "xhhw": 29.46, "use2": 31.88},
    "1000": {"thwn": 34.04, "xhhw": 33.27, "use2": 35.69},
}

# Cable overall diameter (mm) — 2kV PV Wire class
# Priority Wire #5010-01 (AL) datasheet dimensions; Cu derived from
# same insulation thickness applied to NEC Table 5 Cu conductor diameters.
# Insulation: XLPE (shrinkback resistant), 90°C, direct burial
# Insulation thickness by size range:
#   4/0:       0.105" (2.67 mm)
#   250-500:   0.120" (3.05 mm)
#   600-1000:  0.135" (3.43 mm)
CABLE_OD_2KV_AL = {
    "4/0":  17.53,   # 0.69"  — Priority Wire datasheet
    "250":  19.30,   # 0.76"
    "300":  20.57,   # 0.81"
    "350":  21.84,   # 0.86"
    "400":  22.86,   # 0.90"
    "500":  24.89,   # 0.98"
    "600":  27.43,   # 1.08"
    "750":  29.97,   # 1.18"
    "1000": 33.78,   # 1.33"
}
CABLE_OD_2KV_CU = {
    # Cu conductor dia (NEC Table 5 compact) + 2× Priority Wire 2kV insulation thickness
    "4/0":  18.74,   # 13.41 + 2×2.67 = 18.74 mm (0.738")
    "250":  20.71,   # 14.61 + 2×3.05 = 20.71 mm (0.815")
    "300":  22.08,   # 15.98 + 2×3.05 = 22.08 mm (0.869")
    "350":  23.35,   # 17.25 + 2×3.05 = 23.35 mm (0.919")
    "400":  24.54,   # 18.44 + 2×3.05 = 24.54 mm (0.966")
    "500":  26.75,   # 20.65 + 2×3.05 = 26.75 mm (1.053")
    "600":  29.47,   # 22.61 + 2×3.43 = 29.47 mm (1.160")
    "750":  32.21,   # 25.35 + 2×3.43 = 32.21 mm (1.268")
    "1000": 36.12,   # 29.26 + 2×3.43 = 36.12 mm (1.422")
}

# DC resistance at 20°C for 2kV AL PV cable — from Priority Wire datasheet
# Ω/kft ÷ 304.8 = Ω/m
DC_R_AL_2KV = {
    "4/0":  2.693e-4,   # 0.0821 Ω/kft
    "250":  2.280e-4,   # 0.0695 Ω/kft
    "300":  1.900e-4,   # 0.0579 Ω/kft
    "350":  1.627e-4,   # 0.0496 Ω/kft
    "400":  1.424e-4,   # 0.0434 Ω/kft
    "500":  1.142e-4,   # 0.0348 Ω/kft
    "600":  9.514e-5,   # 0.0290 Ω/kft
    "750":  7.611e-5,   # 0.0232 Ω/kft
    "1000": 5.709e-5,   # 0.0174 Ω/kft
}

# Insulation properties
INSULATION_TYPES = {
    "thwn":  {"label": "THWN/THHN",        "rho_t": 3.5, "max_temp": 75.0},
    "xhhw":  {"label": "XHHW-2",           "rho_t": 3.5, "max_temp": 90.0},
    "use2":  {"label": "USE-2 / RHW-2",    "rho_t": 3.5, "max_temp": 90.0},
    "pv2kv": {"label": "PV Wire 2kV XLPE", "rho_t": 3.5, "max_temp": 90.0},
}

# MV insulation properties (unchanged)
MV_INS_RHO_T = 3.5
MV_INS_TAN_DELTA = 0.001
MV_INS_EPSILON = 2.3
MV_VOLTAGE_KV = 34.5
MV_JACKET_RHO_T = 3.5
MV_FREQ = 60

# Conduit data
# rho_t: thermal resistivity of conduit wall (K·m/W)
# U, V, Y: IEC 60287-2-1:2023 Table 4 constants for duct air gap
CONDUIT_TYPES = {
    "pvc40": {"label": "PVC Schedule 40", "rho_t": 6.0, "U": 1.87, "V": 0.312, "Y": 0.0037},
    "hdpe":  {"label": "HDPE",            "rho_t": 3.5, "U": 1.87, "V": 0.312, "Y": 0.0037},
}
CONDUIT_SIZES = [
    {"label": '2"',   "id_mm": 52.5,  "od_mm": 60.3},
    {"label": '2.5"', "id_mm": 62.7,  "od_mm": 73.0},
    {"label": '3"',   "id_mm": 77.9,  "od_mm": 88.9},
    {"label": '4"',   "id_mm": 102.3, "od_mm": 114.3},
    {"label": '5"',   "id_mm": 128.2, "od_mm": 141.3},
    {"label": '6"',   "id_mm": 154.1, "od_mm": 168.3},
    {"label": '8"',   "id_mm": 202.7, "od_mm": 219.1},
]


# =============================================================================
# SHARED THERMAL FUNCTIONS
# =============================================================================

def dc_resistance_at_temp(r20, alpha, temp):
    return r20 * (1 + alpha * (temp - 20))


def T4_soil_single(De_m, L, rho_native, rho_dry, use_dryout):
    """External thermal resistance for a single heat source at depth L.
    Optionally applies two-zone dry-out model using the Neher-McGrath
    Appendix II equivalent thermal diameter for the dry-out envelope.
    """
    if De_m <= 0 or L <= 0:
        return 0.0
    if use_dryout:
        # Equivalent thermal diameter of dry-out zone
        # Neher-McGrath (1957) Appendix II, equations 58-60
        rb = _neher_mcgrath_rb(DRYOUT_X_M, DRYOUT_Y_M)
        D_x = 2.0 * rb
        T_dry = 0.0
        if D_x > De_m:
            T_dry = (rho_dry / (2 * math.pi)) * math.log(D_x / De_m)
        T_native = 0.0
        u = 2 * L / D_x
        if u > 1:
            T_native = (rho_native / (2 * math.pi)) * math.log(
                u + math.sqrt(u * u - 1))
        return T_dry + T_native
    else:
        # No dry-out: uniform soil, exact expression
        u = 2 * L / De_m
        if u > 1:
            return (rho_native / (2 * math.pi)) * math.log(
                u + math.sqrt(u * u - 1))
        return 0.0


def T4_duct_air(group_od_mm, conduit_id_mm, theta_cable, theta_ambient, conduit_type=None):
    """IEC 60287-2-1:2023 Section 4.2.7.2, Table 4.
    Thermal resistance of air gap between cable group and duct wall.
    T4' = U / (1 + 0.1 * (V + Y*theta_m) * De)
    where De is the external diameter of the cable group (mm).
    U, V, Y are constants from Table 4 depending on duct type.
    """
    De = group_od_mm   # mm, as per IEC formula
    if De <= 0 or conduit_id_mm <= De:
        return 0.0
    theta_m = (theta_cable + theta_ambient) / 2
    # Get U, V, Y from conduit type; default to plastic duct values
    if conduit_type and isinstance(conduit_type, dict):
        U = conduit_type.get("U", 1.87)
        V = conduit_type.get("V", 0.312)
        Y = conduit_type.get("Y", 0.0037)
    else:
        U = 1.87
        V = 0.312
        Y = 0.0037
    return U / (1 + 0.1 * (V + Y * theta_m) * De)


def T4_duct_wall(conduit, conduit_type):
    """Thermal resistance of conduit wall."""
    Do, Di = conduit["od_mm"], conduit["id_mm"]
    if Di <= 0 or Do <= Di:
        return 0.0
    return (conduit_type["rho_t"] / (2 * math.pi)) * math.log(Do / Di)


def mutual_heating_factor(n_circuits, spacing_m, depth_m):
    """Mutual heating from parallel circuits."""
    if n_circuits <= 1:
        return 0.0
    positions = [i * spacing_m for i in range(n_circuits)]
    centre = n_circuits // 2
    F = 0.0
    for j in range(n_circuits):
        if j == centre:
            continue
        d_direct = abs(positions[j] - positions[centre])
        d_image = math.sqrt(4 * depth_m ** 2 + d_direct ** 2)
        if d_direct > 0:
            F += math.log(d_image / d_direct)
    return F


# =============================================================================
# MV TREFOIL ENGINE
# =============================================================================

def mv_trefoil_od(cable):
    return cable["overall_diameter_mm"] * (1 + 2 / math.sqrt(3))

def mv_skin_effect(r_dc, ks=1.0):
    """IEC 60287-1-1 skin effect factor.
    ks = skin effect coefficient: 1.0 for round stranded, 0.435 for compact round.
    """
    xs2 = (8 * math.pi * MV_FREQ * ks / r_dc) * 1e-7
    xs4 = xs2 ** 2
    return xs4 / (192 + 0.8 * xs4)

def mv_proximity_effect(r_dc, dc_m, s_m, kp=1.0):
    """IEC 60287-1-1 proximity effect factor.
    kp = proximity effect coefficient: 1.0 for round stranded, 0.37 for compact round.
    """
    xp2 = (8 * math.pi * MV_FREQ * kp / r_dc) * 1e-7
    xp4 = xp2 ** 2
    yp_base = xp4 / (192 + 0.8 * xp4)
    r = dc_m / s_m
    return yp_base * r ** 2 * (0.312 * r ** 2 + 1.18 / (yp_base + 0.27))

def mv_cable_class_kv(cable):
    """Insulation voltage class (kV) of an MV cable; legacy 35 kV set has none set."""
    return cable.get("voltage_class_kv", 35)


# Nominal operating (line-to-line) voltage used for dielectric loss when the
# request does not specify one, keyed by insulation class.
_MV_DEFAULT_OPERATING = {15: 13.8, 25: 24.0, 35: 34.5}


def mv_operating_voltage(params, cable):
    """Operating line-to-line voltage (kV) for dielectric loss: the request's
    voltage_kv if given, else the class nominal."""
    v = params.get("voltage_kv")
    if v is None:
        return _MV_DEFAULT_OPERATING.get(mv_cable_class_kv(cable), MV_VOLTAGE_KV)
    return float(v)


def mv_dielectric_loss(cable, voltage_kv=MV_VOLTAGE_KV):
    d_c = cable["conductor_diameter_mm"] / 1000
    d_ins = cable["insulation_diameter_mm"] / 1000
    if d_c <= 0 or d_ins <= d_c:
        return 0.0
    C = (MV_INS_EPSILON / (18 * math.log(d_ins / d_c))) * 1e-9
    U0 = (voltage_kv * 1000) / math.sqrt(3)
    return 2 * math.pi * MV_FREQ * C * U0 ** 2 * MV_INS_TAN_DELTA

def mv_shield_loss(cable, r_ac, theta_shield=None):
    """Loss factor λ₁ for concentric neutral (CN) wires, multi-point bonded.

    Uses the IEC 60287-1-1 §2.3 circulating-current formula with two
    corrections for CN wire geometry:

    1. HELICAL LAY — CN wires spiral around the cable, increasing their
       effective resistance.  Lay pitch from ICEA S-94-649:
         max_lay = 2.5 + 25 × d_wire  (inches)
       Helical factor = sqrt(1 + (π × D_cn / lay_pitch)²)

    2. LITERATURE CAP — the IEC formula was derived for continuous
       tubular sheaths and over-predicts for discrete CN wires at large
       conductor sizes (Rs/Rac ratio grows because CN wire count is set
       by fault current, not conductor area).  Cap at published maxima:
         1/3 CN and larger: λ₁ ≤ 0.06  (6%)
         1/6 CN (reduced):  λ₁ ≤ 0.03  (3%)
       Per EPRI Underground Transmission Systems Reference Book and
       IEEE ICC cable ampacity working group published ranges.
    """
    r_s = cable["cn_wire_r_dc_20"] / cable["cn_wires"]
    ts = theta_shield if theta_shield is not None else 60.0
    r_s_t = r_s * (1 + 0.00393 * (ts - 20))

    # Helical lay correction per ICEA S-94-649
    d_wire_in = cable["cn_wire_diameter_mm"] / 25.4
    lay_pitch_mm = (2.5 + 25 * d_wire_in) * 25.4
    d_cn_mean_mm = cable["insulation_diameter_mm"] + cable["cn_wire_diameter_mm"]
    helical = math.sqrt(1 + (math.pi * d_cn_mean_mm / lay_pitch_mm) ** 2)
    r_s_h = r_s_t * helical

    d_cn_mean = d_cn_mean_mm / 1000
    s_phase = cable["overall_diameter_mm"] / 1000
    x_s = 2 * math.pi * MV_FREQ * 2e-7 * math.log(2 * s_phase / d_cn_mean)
    lam = (r_s_h / r_ac) / (1 + (r_s_h / x_s) ** 2)

    # Cap at literature values for CN cables
    cn_fraction = cable.get("cn_fraction", "1/3")
    if cn_fraction == "1/6":
        cap = 0.03
    else:
        cap = 0.06
    return max(0.0, min(lam, cap))

def mv_T1(cable):
    d_c = cable["conductor_diameter_mm"]
    d_cn_mean = cable["insulation_diameter_mm"] + cable["cn_wire_diameter_mm"]
    if d_c <= 0 or d_cn_mean <= d_c:
        return 0.0
    return (MV_INS_RHO_T / (2 * math.pi)) * math.log(d_cn_mean / d_c)

def mv_T3(cable):
    d_jacket = cable["overall_diameter_mm"]
    d_under = d_jacket - 2 * cable["jacket_thickness_mm"]
    if d_under <= 0 or d_jacket <= d_under:
        return 0.0
    return (MV_JACKET_RHO_T / (2 * math.pi)) * math.log(d_jacket / d_under)

def _mutual_exit_distance(s, L, X=None, Y=None):
    """Distance from cable centre where the mutual image-method path
    exits the rectangular backfill envelope.

    The image path runs from the cable at (0, 0) toward the trefoil
    neighbor's image at (s, 2L).  We find where this ray first crosses
    the rectangle boundary (±X/2, ±Y/2).  For touching trefoil the
    path is nearly vertical (s ≈ 35 mm vs 2L ≈ 1800+ mm) so it exits
    through the top face at ≈ Y/2 ≈ 305 mm — roughly half the NM
    equivalent circle (670 mm), which corrects the ~15 % mutual T4
    overestimate seen with the concentric two-zone model.
    """
    if X is None:
        X = DRYOUT_X_M
    if Y is None:
        Y = DRYOUT_Y_M
    half_x, half_y = X / 2.0, Y / 2.0
    dy = 2.0 * L
    t_top   = half_y / dy   if dy > 0 else float('inf')
    t_right = half_x / s    if s  > 0 else float('inf')
    t = min(t_top, t_right)
    return math.sqrt((s * t) ** 2 + (dy * t) ** 2)


def mv_T4_earth_trefoil(cable, L, rho_native, rho_dry, use_dryout,
                        separate_backfill=False):
    """External thermal resistance for trefoil touching, direct burial.
    Includes self-heating T4 plus mutual heating from 2 trefoil neighbors.

    Self T4 boundary:
      - Default: NM equivalent circle (D_x = 2·rb ≈ 670 mm).
      - separate_backfill=True: half-diagonal of the backfill rectangle
        (≈ 431 mm for 2 ft × 2 ft).  Used when each circuit has its own
        trench, matching Cymcap "Multiple Ductbanks/Backfills".

    Mutual T4 uses the geometric exit distance through the rectangular
    backfill envelope along the actual image-method path direction.
    For touching trefoil this is ≈ Y/2 = 305 mm (path exits through
    the top face), properly accounting for the non-radial geometry.
    """
    d_e = cable["overall_diameter_mm"] / 1000
    s = d_e   # trefoil touching: spacing = cable OD
    if d_e <= 0 or L <= 0:
        return 0.0

    if separate_backfill and use_dryout:
        # Each circuit has its own backfill trench.  Use the half-
        # diagonal as the effective boundary for the self two-zone split.
        D_x = BACKFILL_HALF_DIAG
        T_dry = (rho_dry / (2 * math.pi)) * math.log(D_x / d_e) if D_x > d_e else 0.0
        u = 2 * L / D_x
        T_nat = (rho_native / (2 * math.pi)) * math.log(
            u + math.sqrt(u * u - 1)) if u > 1 else 0.0
        T4_self = T_dry + T_nat
    else:
        T4_self = T4_soil_single(d_e, L, rho_native, rho_dry, use_dryout)

    d_image = math.sqrt((2 * L) ** 2 + s ** 2)

    if use_dryout:
        # Geometric path-fraction mutual: split at the distance where
        # the image-method ray exits the rectangular backfill zone.
        D_exit = _mutual_exit_distance(s, L)
        # Inner zone (spacing to exit) — backfill resistivity
        T4_mut_inner = 0.0
        if D_exit > s:
            T4_mut_inner = (rho_dry / (2 * math.pi)) * math.log(D_exit / s)
        # Outer zone (exit to image) — native resistivity
        T4_mut_outer = 0.0
        if d_image > D_exit:
            T4_mut_outer = (rho_native / (2 * math.pi)) * math.log(d_image / D_exit)
        T4_mutual = T4_mut_inner + T4_mut_outer
    else:
        T4_mutual = (rho_native / (2 * math.pi)) * math.log(d_image / s)

    return T4_self + 2 * T4_mutual


def mv_T4_earth_trefoil_components(cable, L, rho_native, rho_dry, use_dryout,
                                   separate_backfill=False):
    """Same as mv_T4_earth_trefoil but returns (T4_dry, T4_native) split.

    The dryout zone thermal resistance is determined by the peak cable
    surface temperature and does NOT decrease under cyclic loading.
    Only the native soil portion beyond the dryout boundary responds
    to time-averaged losses (i.e. is affected by the loss factor).

    Args:
        separate_backfill: If True, use half-diagonal boundary for self T4
            (matches Cymcap "Multiple Ductbanks/Backfills").  Same logic as
            mv_T4_earth_trefoil(separate_backfill=True).

    Returns:
        (T4_dry, T4_native) — sum equals total T4 from mv_T4_earth_trefoil.
        When use_dryout is False, T4_dry = 0 and T4_native = full T4.
    """
    d_e = cable["overall_diameter_mm"] / 1000
    s = d_e
    if d_e <= 0 or L <= 0:
        return (0.0, 0.0)

    d_image = math.sqrt((2 * L) ** 2 + s ** 2)

    if not use_dryout:
        T4_native = T4_soil_single(d_e, L, rho_native, rho_native, False)
        T4_mut = (rho_native / (2 * math.pi)) * math.log(d_image / s)
        return (0.0, T4_native + 2 * T4_mut)

    # --- Self component ---
    if separate_backfill:
        D_x = BACKFILL_HALF_DIAG
    else:
        rb = _neher_mcgrath_rb(DRYOUT_X_M, DRYOUT_Y_M)
        D_x = 2.0 * rb
    # Dry zone: cable surface to dryout boundary
    T4_self_dry = 0.0
    if D_x > d_e:
        T4_self_dry = (rho_dry / (2 * math.pi)) * math.log(D_x / d_e)
    # Native zone: dryout boundary to surface image
    T4_self_native = 0.0
    u = 2 * L / D_x
    if u > 1:
        T4_self_native = (rho_native / (2 * math.pi)) * math.log(
            u + math.sqrt(u * u - 1))

    # --- Mutual component (2 trefoil neighbors) ---
    # Use geometric exit distance (not NM equivalent circle) for mutual
    D_exit = _mutual_exit_distance(s, L)
    T4_mut_dry = 0.0
    T4_mut_native = 0.0
    if D_exit > s:
        T4_mut_dry = (rho_dry / (2 * math.pi)) * math.log(D_exit / s)
    if d_image > D_exit:
        T4_mut_native = (rho_native / (2 * math.pi)) * math.log(d_image / D_exit)

    total_dry = T4_self_dry + 2 * T4_mut_dry
    total_native = T4_self_native + 2 * T4_mut_native
    return (total_dry, total_native)

def compute_mv(params):
    cable_key = params["cableSize"]
    cable = MV_CABLES[cable_key]
    L = params["burialDepth"] * 0.0254
    voltage_kv = mv_operating_voltage(params, cable)
    Wd = mv_dielectric_loss(cable, voltage_kv)
    lf = params["loadFactor"]
    llf = lf ** 2
    # IEC 60287-1-1 daily loss factor: μ = 0.3·LF + 0.7·LF²
    # Used for inter-circuit mutual heating where the thermal path is
    # long enough for the soil to see time-averaged losses.
    mu_daily = 0.3 * lf + 0.7 * llf
    use_dryout = params.get("useDryout", True)
    rho_n = params["soilRhoNative"]
    rho_d = params["soilRhoDry"] if use_dryout else rho_n

    is_conduit = params["installType"] == "conduit"
    conduit = CONDUIT_SIZES[params.get("conduitSize", 3)] if is_conduit else None
    c_type = CONDUIT_TYPES.get(params.get("conduitType", "pvc40")) if is_conduit else None
    nC = params.get("numCircuits", 1)
    # Separate-backfill model: when multiple circuits are direct-buried,
    # each has its own trench.  Use the half-diagonal boundary for self T4.
    sep_bf = (nC > 1 and not is_conduit)

    theta_c = 90.0
    I_calc = 0.0

    for iteration in range(80):
        R_dc = dc_resistance_at_temp(cable["r_dc_20"], cable["alpha_20"], theta_c)
        ks = cable.get("ks", 1.0)
        kp = cable.get("kp", 1.0)
        ys = mv_skin_effect(R_dc, ks)
        s_m = cable["overall_diameter_mm"] / 1000
        d_m = cable["conductor_diameter_mm"] / 1000
        yp = mv_proximity_effect(R_dc, d_m, s_m, kp)
        R_ac = R_dc * (1 + ys + yp)
        t1 = mv_T1(cable)
        t3 = mv_T3(cable)

        if is_conduit and conduit and c_type:
            t_od = mv_trefoil_od(cable)
            t4_air = T4_duct_air(t_od, conduit["id_mm"], theta_c, params["soilTemp"], c_type)
            t4_wall = T4_duct_wall(conduit, c_type)
            t4_soil = T4_soil_single(conduit["od_mm"] / 1000, L, rho_n, rho_d, use_dryout)
            t4_total = t4_air + t4_wall + t4_soil
            n_ext = 3
            # Duct air gap and wall always see peak cable temperature.
            # Soil beyond conduit: NM cyclic split at Dx.
            De_cond = conduit["od_mm"] / 1000
            rho_near_c = rho_d if use_dryout else rho_n
            t4_soil_trans = (rho_near_c / (2 * math.pi)) * math.log(NM_DX / De_cond) if NM_DX > De_cond else 0.0
            t4_eff = t4_air + t4_wall + t4_soil_trans + mu_daily * (t4_soil - t4_soil_trans)
        else:
            t4_total = mv_T4_earth_trefoil(cable, L, rho_n, rho_d, use_dryout,
                                           separate_backfill=sep_bf)
            n_ext = 1
            t4_air = t4_wall = 0.0
            # Neher-McGrath cyclic rating: split T4 at thermal penetration
            # depth Dx.  Soil inside Dx sees peak (transient) losses; soil
            # outside Dx sees time-averaged losses (mu).  Trefoil mutual
            # paths are long enough that mu applies to the entire mutual
            # component — only the self transient zone is held at peak.
            d_e = cable["overall_diameter_mm"] / 1000
            rho_near = rho_d if use_dryout else rho_n
            t4_self_trans = (rho_near / (2 * math.pi)) * math.log(NM_DX / d_e) if NM_DX > d_e else 0.0
            t4_eff = t4_self_trans + mu_daily * (t4_total - t4_self_trans)

        # Estimate shield temperature from thermal resistance network.
        # Screen sits between T1 (conductor→screen) and T3+T4 (screen→ambient).
        T_outer = t3 + n_ext * t4_eff
        T_total_path = t1 + T_outer
        if T_total_path > 0:
            theta_shield = theta_c - (theta_c - params["soilTemp"]) * t1 / T_total_path
        else:
            theta_shield = 60.0
        lam1 = mv_shield_loss(cable, R_ac, theta_shield)

        T_i2r = t1 + (1 + lam1) * (t3 + n_ext * t4_eff)
        T_wd = 0.5 * t1 + t3 + n_ext * t4_eff

        # Mutual heating from parallel circuits
        # Solved self-consistently: dT_mut = W_total * rho * F / (2pi)
        # where W_total = 3*(I^2*Rac*(1+lam1) + Wd) per circuit
        # We fold the mutual I^2*R component into the thermal resistance
        # and handle the Wd mutual component as a fixed offset.
        dT_mut = 0.0
        T_mutual_factor = 0.0
        dT_wd_mutual = 0.0
        if nC > 1:
            F_m = mutual_heating_factor(nC, params.get("circuitSpacing", 48) * 0.0254, L)
            # Inter-circuit mutual heating: the image-method paths between
            # parallel circuits (spaced 36-60"+) are entirely outside the
            # backfill zone, so use native soil resistivity.
            # IEC daily loss factor μ (not raw LF²) for the long inter-circuit
            # thermal paths, matching Cymcap's treatment.
            T_mutual_factor = 3 * (1 + lam1) * rho_n * F_m * mu_daily / (2 * math.pi)
            dT_wd_mutual = 3 * Wd * rho_n * F_m * mu_daily / (2 * math.pi)

        dT_avail = 90.0 - params["soilTemp"] - Wd * T_wd - dT_wd_mutual
        T_total_eff = R_ac * (T_i2r + T_mutual_factor)
        I_calc = math.sqrt(max(0, dT_avail) / T_total_eff) if dT_avail > 0 and T_total_eff > 0 else 0.0

        # Compute actual mutual heating for reporting
        if nC > 1:
            W_total = 3 * (I_calc ** 2 * R_ac * (1 + lam1) + Wd)
            dT_mut = W_total * rho_n * F_m * mu_daily / (2 * math.pi)

        theta_new = params["soilTemp"] + I_calc ** 2 * R_ac * T_i2r + Wd * T_wd + dT_mut
        if abs(theta_new - theta_c) < 0.05:
            theta_c = theta_new
            break
        theta_c = theta_new
    else:
        raise ValueError("MV thermal iteration did not converge")

    # Final values
    R_dc = dc_resistance_at_temp(cable["r_dc_20"], cable["alpha_20"], theta_c)
    ks = cable.get("ks", 1.0)
    kp = cable.get("kp", 1.0)
    ys = mv_skin_effect(R_dc, ks)
    yp = mv_proximity_effect(R_dc, cable["conductor_diameter_mm"] / 1000,
                              cable["overall_diameter_mm"] / 1000, kp)
    R_ac = R_dc * (1 + ys + yp)
    lam1 = mv_shield_loss(cable, R_ac, theta_shield)

    return {
        "ampacity": math.floor(I_calc * 0.95 * 10) / 10,
        "ampacityRaw": round(I_calc, 1),
        "conductorTemp": round(theta_c, 1),
        "Rac": round(R_ac * 1e6, 2),
        "ys": round(ys * 100, 3),
        "yp": round(yp * 100, 3),
        "l1": round(lam1 * 100, 2),
        "Wd": round(Wd, 3),
        "tOD": round(mv_trefoil_od(cable), 1),
        "T1": round(mv_T1(cable), 4),
        "T3": round(mv_T3(cable), 4),
        "T4a": round(t4_air, 4),
        "T4w": round(t4_wall if is_conduit else 0, 4),
        "T4s": round(t4_total, 4),
        "T4t": round(t4_total, 4),
        "dTm": round(dT_mut, 1),
        "cableLabel": cable["label"],
        "systemType": "mv",
        # Cable physical parameters
        "conductorDia": round(cable["conductor_diameter_mm"], 2),
        "insulationDia": round(cable["insulation_diameter_mm"], 2),
        "insulationThk": round((cable["insulation_diameter_mm"] - cable["conductor_diameter_mm"]) / 2, 2),
        "cnWires": cable["cn_wires"],
        "cnWireDia": round(cable["cn_wire_diameter_mm"], 2),
        "jacketThk": round(cable["jacket_thickness_mm"], 2),
        "overallDia": round(cable["overall_diameter_mm"], 2),
        "Rdc20": round(cable["r_dc_20"] * 1e6, 2),
        "maxTemp": 90,
        "voltageClass": "%dkV" % mv_cable_class_kv(cable),
        "voltageKv": round(voltage_kv, 2),
        "insulationType": "TR-XLPE",
        "conductorMaterial": "Aluminum",
    }


# =============================================================================
# DC ENGINE
# =============================================================================

def get_dc_cable(size, material, insulation):
    """Build a cable data dict for DC/LVAC calculations.
    Handles both 600V insulation types and 2kV PV wire.
    """
    if insulation == "pv2kv":
        # 2kV PV Wire — use Priority Wire datasheet data
        if material == "al":
            r_table = DC_R_AL_2KV
            od_mm = CABLE_OD_2KV_AL[size]
        else:
            r_table = DC_R_CU  # Cu Rdc same regardless of voltage class
            od_mm = CABLE_OD_2KV_CU[size]
        alpha = ALPHA_CU if material == "cu" else ALPHA_AL
        ins_props = INSULATION_TYPES["pv2kv"]
        mat_label = "Cu" if material == "cu" else "Al"
        return {
            "label": size + (" kcmil " if size not in ("4/0",) else " AWG ") + mat_label + " PV 2kV",
            "r_dc_20": r_table[size],
            "alpha_20": alpha,
            "conductor_diameter_mm": COND_DIA[size][material],
            "overall_diameter_mm": od_mm,
            "insulation_rho_t": ins_props["rho_t"],
            "max_temp": ins_props["max_temp"],
        }
    else:
        # 600V insulation types (THWN, XHHW, USE-2)
        r_table = DC_R_CU if material == "cu" else DC_R_AL
        alpha = ALPHA_CU if material == "cu" else ALPHA_AL
        ins_props = INSULATION_TYPES[insulation]
        mat_label = "Cu" if material == "cu" else "Al"
        return {
            "label": size + (" kcmil " if size not in ("4/0",) else " AWG ") + mat_label + " " + ins_props["label"],
            "r_dc_20": r_table[size],
            "alpha_20": alpha,
            "conductor_diameter_mm": COND_DIA[size][material],
            "overall_diameter_mm": CABLE_OD[size][insulation],
            "insulation_rho_t": ins_props["rho_t"],
            "max_temp": ins_props["max_temp"],
        }


def dc_T1(cable):
    """Insulation thermal resistance for single building wire."""
    d_c = cable["conductor_diameter_mm"]
    d_o = cable["overall_diameter_mm"]
    if d_c <= 0 or d_o <= d_c:
        return 0.0
    return (cable["insulation_rho_t"] / (2 * math.pi)) * math.log(d_o / d_c)



# =============================================================================
# DC/LVAC AIR GAP - ACTIVE MODEL NOTE
# =============================================================================
# The model in use for cables in conduit is the IEC 60287-2-1 simplified
# concentric group air-gap formula (see T4_duct_air above), called from
# compute_dc / compute_lvac. It is the model validated against Cymcap 8.1
# (DC 19/19, LVAC 11/11 within +/-5%).
#
# The per-cable ECCENTRIC air-gap model below is DISABLED (commented out) and
# retained for a future enhancement -- it was never wired into the engines.
# Re-enabling it would raise T4_air (~9% per its derivation) and lower conduit
# ratings slightly, so it MUST be followed by re-running the Cymcap DC/LVAC
# conduit validation. See bug-review item #4.
# =============================================================================

# # =============================================================================
# # DC ENGINE — ECCENTRIC AIR GAP MODEL (per-cable thermal resistance)
# # =============================================================================
# # Cables in horizontal ducts rest at the bottom, creating an eccentric
# # geometry where the air gap varies from near-zero (below) to maximum
# # (above). The IEC 60287-2-1 simplified formula assumes concentric
# # geometry and underestimates T4_air by ~9%.
# #
# # This model uses:
# #   - Raithby & Hollands (1975) effective conductivity for natural
# #     convection between horizontal eccentric cylinders
# #   - Kuehn & Goldstein (1976) Nusselt correlation
# #   - Concentric cylinder radiation exchange
# #
# # References:
# #   Kuehn & Goldstein, "Correlating equations for natural convection
# #     heat transfer between horizontal circular cylinders", Int J Heat
# #     Mass Transfer, 1976.
# #   Raithby & Hollands, "A general method of obtaining approximate
# #     solutions to laminar and turbulent free convection problems",
# #     Advances in Heat Transfer, 1975.
# #   Morgan, "The overall convective heat transfer from smooth circular
# #     cylinders", Advances in Heat Transfer, 1975.
#
# _SIGMA_SB = 5.670374e-8   # Stefan-Boltzmann constant (W/m²·K⁴)
# _EPSILON_CABLE = 0.9       # Surface emissivity: XLPE/PVC cable jacket
# _EPSILON_DUCT = 0.9        # Surface emissivity: PVC duct inner wall
# _G_ACCEL = 9.81            # Gravitational acceleration (m/s²)
#
#
# def _air_props(theta_c):
#     """Air thermophysical properties at temperature theta_c (°C).
#     Returns (k, nu, Pr, beta) where:
#       k    = thermal conductivity (W/m·K)
#       nu   = kinematic viscosity (m²/s)
#       Pr   = Prandtl number (dimensionless)
#       beta = volumetric expansion coefficient (1/K)
#     Curve fits valid for 0–200°C, sufficient for cable rating.
#     """
#     T_K = theta_c + 273.15
#     k = 0.0241 + 7.59e-5 * theta_c
#     nu = 1.338e-5 * (T_K / 273.15) ** 1.75
#     alpha = 1.89e-5 * (T_K / 273.15) ** 1.75
#     Pr = nu / alpha
#     beta = 1.0 / T_K
#     return k, nu, Pr, beta
#
#
# def _dc_cable_positions(n_cables, duct_id_mm, cable_od_mm):
#     """Compute (x, y) positions (in mm) for n_cables cradled at the
#     bottom of a circular horizontal duct of inner diameter duct_id_mm.
#
#     Cables rest under gravity. The coordinate origin is the duct centre.
#     Returns list of (x, y) tuples in mm.
#
#     Arrangements:
#       1 cable:  centred at duct bottom
#       2 cables: touching side-by-side at duct bottom
#       4 cables: bottom row of 2, second row of 2 nested above
#       6 cables: bottom row of 3, second row of 3 nested above
#     """
#     R = duct_id_mm / 2.0     # duct inner radius
#     r = cable_od_mm / 2.0    # cable radius
#     d = cable_od_mm           # cable diameter (centre-to-centre when touching)
#
#     if n_cables == 1:
#         # Single cable rests at the bottom of the duct
#         return [(0.0, -R + r)]
#
#     if n_cables == 2:
#         # Two cables touching, resting at the bottom
#         # Each cable centre is at x = ±d/2 from duct centreline
#         # y position: cables rest in the curved bottom of the duct
#         # For cable at x-offset from centre, y = -sqrt(R² - x²) + r
#         # (cable surface touches duct inner wall)
#         x = d / 2.0
#         if x + r > R:
#             # Cables don't fit side-by-side; stack or just use what fits
#             x = R - r
#         y = -math.sqrt(max(0, (R - r) ** 2 - x ** 2))
#         return [(-x, y), (x, y)]
#
#     if n_cables == 4:
#         # Bottom row: 2 cables touching at duct bottom
#         # Top row: 2 cables nested in the valleys above
#         x_bot = d / 2.0
#         y_bot = -math.sqrt(max(0, (R - r) ** 2 - x_bot ** 2))
#         # Top row cables sit on top of bottom row, shifted inward
#         # Each top cable touches both bottom cables:
#         #   centre-to-centre distance = d (touching)
#         #   top cable x = 0 ± something, y = y_bot + sqrt(d² - (d/2)²)
#         # Actually: top cables centred at x=0 ± x_top
#         # For symmetric 2×2: top cables directly above bottom cables
#         # but shifted inward. With 2 bottom at ±d/2, 2 top at ±0:
#         # Each top cable rests on both bottom cables → touches both
#         # Height of top row: y_top = y_bot + sqrt(d² - (d/2)²) = y_bot + d*sqrt(3)/2
#         dy = d * math.sqrt(3) / 2.0
#         # Top row x: centred (each top cable touches both bottom cables)
#         # If bottom at ±d/2, top cable equidistant from both → x = 0
#         # But we need 2 top cables. They sit at ±0 → both at x=0? No.
#         # 2×2 square packing: top row at same ±d/2 but shifted up
#         x_top = d / 2.0
#         y_top = y_bot + d   # square arrangement: directly above
#         # Check fit
#         for (x, y) in [(-x_top, y_top), (x_top, y_top)]:
#             if math.sqrt(x**2 + y**2) + r > R * 1.01:
#                 # Tight fit — use diamond/rhombic arrangement instead
#                 # Top cables at x=0, staggered
#                 x_top = 0.0
#                 y_top = y_bot + dy
#                 return [(-x_bot, y_bot), (x_bot, y_bot),
#                         (-x_top - 0.01, y_top), (x_top + 0.01, y_top)]
#         return [(-x_bot, y_bot), (x_bot, y_bot),
#                 (-x_top, y_top), (x_top, y_top)]
#
#     if n_cables == 6:
#         # Bottom row: 3 cables touching at duct bottom
#         # Top row: 3 cables nested above
#         x_bot = [-(d), 0.0, d]  # centre cable at x=0
#         positions = []
#         for xb in x_bot:
#             y = -math.sqrt(max(0, (R - r) ** 2 - xb ** 2))
#             positions.append((xb, y))
#         # Top row: 3 cables nested in valleys between bottom cables
#         dy = d * math.sqrt(3) / 2.0
#         y_top_ref = positions[0][1] + dy  # above bottom row
#         x_top = [-d / 2.0, d / 2.0]
#         # For 3 top cables: at x = -d, 0, +d shifted by d/2
#         x_top3 = [-d / 2.0, d / 2.0]
#         # Only 2 valleys for 3 bottom cables, so 2 top cables fit naturally
#         # For 6 cables: 3 bottom + 3 top requires wider spacing or 3-2-1 pyramid
#         # Actually 6 cables in 2 rows: bottom 3, top 3 shifted
#         x_top3 = [-d, 0.0, d]
#         for xt in x_top3:
#             yt = y_top_ref
#             if math.sqrt(xt**2 + yt**2) + r <= R * 1.01:
#                 positions.append((xt, yt))
#             else:
#                 # Doesn't fit — place at maximum inward position
#                 xt_clamp = max(-R + r, min(R - r, xt))
#                 yt_clamp = -math.sqrt(max(0, (R - r)**2 - xt_clamp**2)) + dy
#                 positions.append((xt_clamp, yt_clamp))
#         return positions[:n_cables]
#
#     # Fallback: arrange in a circle near duct bottom
#     positions = []
#     for i in range(n_cables):
#         angle = math.pi + math.pi * (i + 0.5) / n_cables
#         x = (R - r) * 0.7 * math.cos(angle)
#         y = (R - r) * 0.7 * math.sin(angle)
#         positions.append((x, y))
#     return positions
#
#
# def _dc_T4_air_eccentric(cable_od_mm, duct_id_mm, eccentricity_mm,
#                           theta_cable_surf, theta_duct_inner):
#     """Thermal resistance of air gap for a cable at eccentric position
#     in a horizontal circular duct.
#
#     Uses Raithby & Hollands (1975) / Kuehn & Goldstein (1976)
#     effective conductivity method for natural convection between
#     eccentric horizontal cylinders, combined with radiation.
#
#     Parameters:
#         cable_od_mm:      cable (or single cable) outer diameter (mm)
#         duct_id_mm:       duct inner diameter (mm)
#         eccentricity_mm:  distance from duct centre to cable centre (mm)
#         theta_cable_surf: cable surface temperature (°C)
#         theta_duct_inner: duct inner surface temperature (°C)
#
#     Returns:
#         T4_air in K·m/W (thermal resistance per unit length)
#     """
#     r_cable = cable_od_mm / 2000.0     # cable radius (m)
#     R_duct = duct_id_mm / 2000.0       # duct inner radius (m)
#     e = eccentricity_mm / 1000.0       # eccentricity (m)
#
#     D_cable = cable_od_mm / 1000.0
#     D_duct = duct_id_mm / 1000.0
#
#     if D_cable <= 0 or D_duct <= D_cable:
#         return 0.0
#
#     dT = max(theta_cable_surf - theta_duct_inner, 0.1)
#     T_mean_C = (theta_cable_surf + theta_duct_inner) / 2.0
#     T_hot_K = theta_cable_surf + 273.15
#     T_cold_K = theta_duct_inner + 273.15
#
#     k, nu, Pr, beta = _air_props(T_mean_C)
#
#     # --- Natural Convection ---
#     # Raithby & Hollands (1975) characteristic length for concentric/
#     # eccentric cylinders (geometric mean of gap around the annulus):
#     #
#     # For eccentric geometry, the gap varies with angle:
#     #   L(θ) = R_duct - e·cos(θ) - r_cable
#     # Average gap:
#     #   L_avg = R_duct - r_cable  (eccentricity averages out over 2π)
#     # But the effective gap for convection is NOT the average — the
#     # narrow gap region dominates resistance. Use Raithby & Hollands
#     # equivalent length based on the concentric-cylinder formulation:
#     #
#     #   Lc = ln(D_duct/D_cable) / (D_cable^(-3/5) + D_duct^(-3/5))^(5/3)
#     #
#     # This Lc is independent of eccentricity — it represents the
#     # conduction-dominated limit. The eccentricity effect enters
#     # through the conduction path (Heyda formula) which we combine
#     # with the convection enhancement.
#
#     Dc_35 = D_cable ** (-3.0 / 5.0)
#     Dd_35 = D_duct ** (-3.0 / 5.0)
#     Lc = math.log(D_duct / D_cable) / (Dc_35 + Dd_35) ** (5.0 / 3.0)
#
#     # Rayleigh number based on characteristic length Lc
#     Ra_c = _G_ACCEL * beta * dT * Lc ** 3 / (nu * (nu / Pr))
#
#     # Kuehn & Goldstein (1976) Nusselt correlation:
#     #   Nu_c = max(1, 0.386 × (Pr / (0.861 + Pr))^0.25 × Ra_c^0.25)
#     if Ra_c > 0:
#         f_Pr = (Pr / (0.861 + Pr)) ** 0.25
#         Nu_c = max(1.0, 0.386 * f_Pr * Ra_c ** 0.25)
#     else:
#         Nu_c = 1.0
#
#     # Effective thermal conductivity (convection)
#     k_eff_conv = k * Nu_c
#
#     # Convective thermal resistance for concentric cylinders:
#     #   T_conv = ln(D_duct/D_cable) / (2π × k_eff)
#     T_conv_concentric = math.log(D_duct / D_cable) / (2 * math.pi * k_eff_conv)
#
#     # --- Eccentric correction on the conduction/convection path ---
#     # For an eccentric cable, the pure conduction path (Heyda 1964) is:
#     #   T_cond_ecc = (1/2πk) × cosh⁻¹((R²+r²-e²)/(2Rr))
#     #
#     # The ratio T_cond_ecc / T_cond_concentric gives the eccentricity
#     # factor. When e>0, T_cond_ecc < T_cond_concentric (shorter path
#     # on one side). However, for natural convection the eccentric
#     # geometry suppresses the convection cell, INCREASING the thermal
#     # resistance. Literature (Morgan 1975) shows net increase of 5-15%.
#     #
#     # We use a physics-based correction: the conduction limit sets the
#     # minimum resistance, and the convection enhancement (Nu_c) is
#     # reduced for eccentric geometry because the narrow gap suppresses
#     # flow circulation. The effective Nusselt number for eccentric
#     # geometry is reduced by a factor related to the gap non-uniformity.
#     #
#     # Eccentric Nusselt correction (Kuehn & Goldstein 1978, Fig. 8):
#     # At moderate eccentricity (e/(R-r) ~ 0.5-0.8), Nu drops by 10-30%
#     # relative to concentric case. We use the correlation:
#     #   Nu_ecc/Nu_conc ≈ (1 - (e/(R-r))²)^0.2
#     # which gives:
#     #   e=0 → ratio=1.0 (concentric)
#     #   e=0.5(R-r) → ratio=0.87
#     #   e=0.8(R-r) → ratio=0.71
#     #   e→(R-r) → ratio→0 (touching)
#
#     gap_max = R_duct - r_cable
#     if gap_max > 0:
#         e_ratio = min(e / gap_max, 0.999)
#     else:
#         e_ratio = 0.0
#
#     Nu_ecc_factor = (1.0 - e_ratio ** 2) ** 0.2
#
#     # Effective Nu for eccentric geometry
#     Nu_ecc = max(1.0, Nu_c * Nu_ecc_factor)
#     k_eff_conv_ecc = k * Nu_ecc
#
#     # Convective resistance (eccentric)
#     # Use Heyda conduction formula as the base, enhanced by Nu:
#     # T_conv_ecc = T_cond_ecc / Nu_ecc  where T_cond_ecc is the
#     # pure conduction resistance in eccentric annulus.
#     #
#     # Heyda formula: T = (1/2πk) × acosh((R²+r²-e²)/(2Rr))
#     heyda_arg = (R_duct**2 + r_cable**2 - e**2) / (2 * R_duct * r_cable)
#     heyda_arg = max(1.0, heyda_arg)  # clamp to valid range
#     T_cond_ecc = math.acosh(heyda_arg) / (2 * math.pi * k)
#
#     # Apply convection enhancement to eccentric conduction resistance
#     if Nu_ecc > 0:
#         T_conv_ecc = T_cond_ecc / Nu_ecc
#     else:
#         T_conv_ecc = T_cond_ecc
#
#     # Ensure eccentric convective resistance is not less than concentric
#     # (eccentricity should not help — narrow gap suppresses circulation)
#     T_conv_ecc = max(T_conv_ecc, T_conv_concentric)
#
#     # --- Radiation ---
#     # Radiation exchange between concentric cylinders (position-independent
#     # to first order; view factor ≈ 1 for inner cylinder to outer):
#     #   h_r = σ × (T_hot² + T_cold²)(T_hot + T_cold) / F_emissivity
#     #   F_emissivity = 1/ε_cable + (D_cable/D_duct)(1/ε_duct - 1)
#     F_eps = (1.0 / _EPSILON_CABLE
#              + (D_cable / D_duct) * (1.0 / _EPSILON_DUCT - 1.0))
#     h_r = (_SIGMA_SB * (T_hot_K**2 + T_cold_K**2) * (T_hot_K + T_cold_K)
#            / F_eps)
#     T_rad = 1.0 / (math.pi * D_cable * h_r) if h_r > 0 else 1e6
#
#     # --- Combined (parallel paths) ---
#     # 1/T_total = 1/T_conv + 1/T_rad
#     T_air = 1.0 / (1.0 / T_conv_ecc + 1.0 / T_rad)
#     return T_air



def compute_dc(params):
    """DC ampacity calculation.

    Conduit installations use the IEC 60287-2-1 simplified group air-gap
    model (T4_duct_air, Table 4) -- the model validated against Cymcap 8.1.
    A more detailed per-cable eccentric air-gap model (Raithby & Hollands
    1975, Kuehn & Goldstein 1976) is retained but disabled below, pending
    re-validation before it is wired in.

    Direct burial path uses image-method mutual heating.
    """
    size = params["cableSize"]
    material = params["material"]       # "cu" or "al"
    insulation = params["insulation"]   # "thwn", "xhhw", "use2"
    cable = get_dc_cable(size, material, insulation)

    L = params["burialDepth"] * 0.0254
    max_temp = cable["max_temp"]
    llf = params["loadFactor"] ** 2
    use_dryout = params.get("useDryout", False)
    rho_n = params["soilRhoNative"]
    rho_d = params["soilRhoDry"] if use_dryout else rho_n

    cond_per_duct = params.get("conductorsPerConduit", 1)  # circuits per conduit (1-3)
    total_cables = cond_per_duct * 2  # each circuit = 2 cables (+/-)
    is_conduit = params["installType"] == "conduit"
    conduit = CONDUIT_SIZES[params.get("conduitSize", 3)] if is_conduit else None
    c_type = CONDUIT_TYPES.get(params.get("conduitType", "pvc40")) if is_conduit else None

    d_cable = cable["overall_diameter_mm"]

    # IEC 60287-2-1 Table 3: equivalent thermal diameter of cable group
    # in duct.  These factors match the IEC definition used by Cymcap
    # (perimeter-equivalent circle of the cable-group convex hull, not
    # the circumscribing circle).
    #   2 cables flat:   Deq = 1.65 × d
    #   3 cables trefoil: Deq = 2.15 × d  (standard IEC value)
    #   4 cables diamond: Deq = 2.50 × d
    #   6 cables 3+3:    Deq = 2.45 × d
    _DC_DEQ_FACTOR = {2: 1.65, 3: 2.15, 4: 2.50, 6: 2.45}
    deq_factor = _DC_DEQ_FACTOR.get(total_cables, math.sqrt(total_cables))
    group_od = d_cable * deq_factor

    theta_c = max_temp
    I_calc = 0.0
    # Track worst-case air gap for reporting
    t4_air = 0.0
    t4_wall = 0.0
    t4_total = 0.0

    for iteration in range(80):
        R_dc = dc_resistance_at_temp(cable["r_dc_20"], cable["alpha_20"], theta_c)

        # DC: no skin or proximity effect
        t1 = dc_T1(cable)

        if is_conduit and conduit and c_type:
            # --- Group-OD IEC simplified air gap model ---
            # Cables in duct form a thermal group; use IEC 60287-2-1
            # Table 4 formula with IEC equivalent diameter (Deq).
            t4_wall = T4_duct_wall(conduit, c_type)
            t4_soil = T4_soil_single(conduit["od_mm"] / 1000, L, rho_n, rho_d, use_dryout)
            # IEC θm = mean air temperature in conduit.  Estimate from
            # cable surface and duct inner surface temperatures, which
            # refine each iteration.  On the first pass (I_calc=0) fall
            # back to conductor / ambient.
            W_est = I_calc ** 2 * R_dc if I_calc > 0 else 0.0
            if W_est > 0:
                theta_surf_est = theta_c - W_est * t1
                theta_duct_est = params["soilTemp"] + total_cables * W_est * (t4_wall + t4_soil)
            else:
                theta_surf_est = theta_c
                theta_duct_est = params["soilTemp"]
            t4_air = T4_duct_air(group_od, conduit["id_mm"],
                                 theta_surf_est, theta_duct_est, c_type)
            t4_total = t4_air + t4_wall + t4_soil
            # LF²: air gap and wall see peak temperature; soil sees LF²
            t4_eff = t4_air + t4_wall + t4_soil * llf
        else:
            # --- Direct burial — unchanged ---
            # Cables touching side by side, image method for mutual heating
            d_e = d_cable / 1000
            t4_self = T4_soil_single(d_e, L, rho_n, rho_d, use_dryout)
            t4_mutual_sum = 0.0
            # Geometric path-fraction mutual: split the image-method ray
            # at the rectangular backfill boundary (same approach as MV
            # engine).  Inner portion uses rho_dry, outer uses rho_native.
            for k in range(1, total_cables):
                s_k = k * d_e  # spacing to k-th cable
                d_img = math.sqrt((2 * L) ** 2 + s_k ** 2)
                if use_dryout:
                    D_exit = _mutual_exit_distance(s_k, L)
                    t4_mut_inner = 0.0
                    t4_mut_outer = 0.0
                    if D_exit > s_k:
                        t4_mut_inner = (rho_d / (2 * math.pi)) * math.log(
                            min(D_exit, d_img) / s_k)
                    if d_img > D_exit:
                        t4_mut_outer = (rho_n / (2 * math.pi)) * math.log(
                            d_img / max(D_exit, s_k))
                    t4_mutual_sum += t4_mut_inner + t4_mut_outer
                else:
                    t4_mutual_sum += (rho_n / (2 * math.pi)) * math.log(d_img / s_k)
            t4_total = t4_self + t4_mutual_sum
            t4_air = t4_wall = 0.0
            # Direct burial: entire T4 is soil, so LF² applies to all
            t4_eff = t4_total * llf

        # Thermal equation:
        # Conduit: all N cables share one duct — total heat through external
        #   path is N*I²R, so T_ext multiplied by N.
        # Direct burial: t4_total already includes self + mutual from all
        #   sibling cables (image method), so it represents the complete
        #   external thermal resistance for the worst-case cable.
        n_cond = total_cables
        if is_conduit:
            T_total = t1 + n_cond * t4_eff
        else:
            T_total = t1 + t4_eff

        # Mutual heating from parallel circuits (separate ducts/groups)
        dT_mut = 0.0
        T_mutual_factor = 0.0
        nC = params.get("numCircuits", 1)
        if nC > 1:
            F_m = mutual_heating_factor(nC, params.get("circuitSpacing", 48) * 0.0254, L)
            T_mutual_factor = n_cond * rho_d * F_m * llf / (2 * math.pi)

        dT_avail = max_temp - params["soilTemp"]
        T_total_eff = R_dc * (T_total + T_mutual_factor)
        I_calc = math.sqrt(max(0, dT_avail) / T_total_eff) if dT_avail > 0 and T_total_eff > 0 else 0.0

        # Compute actual mutual heating for reporting
        if nC > 1:
            W_total = n_cond * I_calc ** 2 * R_dc
            dT_mut = W_total * rho_d * F_m * llf / (2 * math.pi)

        theta_new = params["soilTemp"] + I_calc ** 2 * R_dc * T_total + dT_mut
        if abs(theta_new - theta_c) < 0.05:
            theta_c = theta_new
            break
        theta_c = theta_new
    else:
        raise ValueError("DC thermal iteration did not converge")

    # Final
    R_dc_final = dc_resistance_at_temp(cable["r_dc_20"], cable["alpha_20"], theta_c)

    return {
        "ampacity": math.floor(I_calc * 0.95 * 10) / 10,
        "ampacityRaw": round(I_calc, 1),
        "conductorTemp": round(theta_c, 1),
        "Rdc": round(R_dc_final * 1e6, 2),
        "T1": round(dc_T1(cable), 4),
        "T4a": round(t4_air, 4),
        "T4w": round(t4_wall if is_conduit else 0, 4),
        "T4s": round(t4_total, 4),
        "T4t": round(t4_total, 4),
        "dTm": round(dT_mut, 1),
        "cableLabel": cable["label"],
        "maxTemp": cable["max_temp"],
        "cableOD": round(d_cable, 1),
        "groupOD": round(group_od, 1),
        "circuitsPerConduit": cond_per_duct,
        "cablesPerConduit": total_cables,
        "systemType": "dc",
        # Cable physical parameters
        "conductorDia": round(cable["conductor_diameter_mm"], 2),
        "overallDia": round(cable["overall_diameter_mm"], 2),
        "insulationThk": round((cable["overall_diameter_mm"] - cable["conductor_diameter_mm"]) / 2, 2),
        "Rdc20": round(cable["r_dc_20"] * 1e6, 2),
        "conductorMaterial": "Copper" if params.get("material") == "cu" else "Aluminum",
        "insulationType": INSULATION_TYPES[params.get("insulation", "xhhw")]["label"],
    }


# =============================================================================
# LVAC ENGINE
# =============================================================================

LV_FREQ = 60

def lv_skin_effect(r_dc):
    xs2 = (8 * math.pi * LV_FREQ / r_dc) * 1e-7
    xs4 = xs2 ** 2
    return xs4 / (192 + 0.8 * xs4)

def lv_proximity_effect(r_dc, dc_m, s_m):
    xp2 = (8 * math.pi * LV_FREQ / r_dc) * 1e-7
    xp4 = xp2 ** 2
    yp_base = xp4 / (192 + 0.8 * xp4)
    r = dc_m / s_m
    return yp_base * r ** 2 * (0.312 * r ** 2 + 1.18 / (yp_base + 0.27))

def compute_lvac(params):
    """LVAC ampacity calculation.
    Configurations:
      - single_phase: 2 conductors (hot + neutral)
      - split_phase: 3 conductors (2 hot + neutral)
      - three_phase_delta: 3 conductors
      - three_phase_wye: 4 conductors (3 phase + neutral)
    """
    size = params["cableSize"]
    material = params["material"]
    insulation = params["insulation"]
    cable = get_dc_cable(size, material, insulation)  # Same cable data as DC

    L = params["burialDepth"] * 0.0254
    max_temp = cable["max_temp"]
    llf = params["loadFactor"] ** 2
    use_dryout = params.get("useDryout", False)
    rho_n = params["soilRhoNative"]
    rho_d = params["soilRhoDry"] if use_dryout else rho_n

    config = params.get("phaseConfig", "three_phase_delta")
    neutral_factor = params.get("neutralFactor", 0.0)  # 0 = balanced, 1 = full load

    # Number of current-carrying conductors and total conductors
    if config == "single_phase":
        n_phase = 1       # 1 hot conductor
        n_neutral = 1     # neutral carries same as phase
        neutral_factor = 1.0  # always fully loaded for single phase
        total_cables = 2
    elif config == "split_phase":
        n_phase = 2
        n_neutral = 1
        neutral_factor = 1.0  # worst-case: full unbalance on neutral
        total_cables = 3
    elif config == "three_phase_delta":
        n_phase = 3
        n_neutral = 0
        total_cables = 3
    else:  # three_phase_wye
        n_phase = 3
        n_neutral = 1
        total_cables = 4

    is_conduit = params["installType"] == "conduit"
    conduit = CONDUIT_SIZES[params.get("conduitSize", 3)] if is_conduit else None
    c_type = CONDUIT_TYPES.get(params.get("conduitType", "pvc40")) if is_conduit else None

    d_cable = cable["overall_diameter_mm"]

    # IEC 60287-2-1 Table 3: equivalent thermal diameter of cable group
    # in duct.  Matches IEC definition (not circumscribing circle).
    #   2 cables flat:    Deq = 1.65 × d
    #   3 cables trefoil: Deq = 2.15 × d  (≈ 1+2/√3 = 2.155)
    #   4 cables diamond: Deq = 2.50 × d
    #   6 cables 3+3:     Deq = 2.45 × d
    _LV_DEQ_FACTOR = {2: 1.65, 3: 2.15, 4: 2.50, 6: 2.45}
    deq_factor = _LV_DEQ_FACTOR.get(total_cables, math.sqrt(total_cables))
    group_od = d_cable * deq_factor

    theta_c = max_temp
    I_calc = 0.0

    for iteration in range(80):
        R_dc = dc_resistance_at_temp(cable["r_dc_20"], cable["alpha_20"], theta_c)
        ys = lv_skin_effect(R_dc)
        # Proximity: use cable OD as spacing (touching)
        d_m = cable["conductor_diameter_mm"] / 1000
        s_m = cable["overall_diameter_mm"] / 1000
        yp = lv_proximity_effect(R_dc, d_m, s_m)
        R_ac = R_dc * (1 + ys + yp)

        t1 = dc_T1(cable)  # Same T1 calculation as DC

        # Heat from all conductors:
        # Phase conductors: n_phase * I^2*Rac
        # Neutral: n_neutral * (neutral_factor * I)^2 * Rac
        # Total heat factor per unit I^2*Rac:
        nf2 = neutral_factor ** 2
        heat_factor = n_phase + n_neutral * nf2

        if is_conduit and conduit and c_type:
            t4_wall = T4_duct_wall(conduit, c_type)
            t4_soil = T4_soil_single(conduit["od_mm"] / 1000, L, rho_n, rho_d, use_dryout)
            # IEC θm: estimate cable surface and duct inner temps
            W_est = I_calc ** 2 * R_ac if I_calc > 0 else 0.0
            if W_est > 0:
                theta_surf_est = theta_c - W_est * t1
                theta_duct_est = params["soilTemp"] + heat_factor * W_est * (t4_wall + t4_soil)
            else:
                theta_surf_est = theta_c
                theta_duct_est = params["soilTemp"]
            t4_air = T4_duct_air(group_od, conduit["id_mm"],
                                 theta_surf_est, theta_duct_est, c_type)
            t4_total = t4_air + t4_wall + t4_soil
            # IEC 60287: loss factor (LF²) applies only to the soil portion.
            # Duct air gap and wall see peak cable temperature regardless of LF.
            t4_eff = t4_air + t4_wall + t4_soil * llf
        else:
            # Direct burial — cables touching side by side in a line.
            d_e = d_cable / 1000
            t4_self = T4_soil_single(d_e, L, rho_n, rho_d, use_dryout)
            # Geometric path-fraction mutual: split the image-method ray
            # at the rectangular backfill boundary (same approach as MV/DC
            # engines).  Inner portion uses rho_dry, outer uses rho_native.

            ref = (n_phase - 1) // 2

            t4_phase_mut = 0.0
            t4_neutral_mut = 0.0
            for k in range(total_cables):
                if k == ref:
                    continue
                s_k = abs(k - ref) * d_e
                d_img = math.sqrt((2 * L) ** 2 + s_k ** 2)
                if use_dryout:
                    D_exit = _mutual_exit_distance(s_k, L)
                    t4_mut_inner = 0.0
                    t4_mut_outer = 0.0
                    if D_exit > s_k:
                        t4_mut_inner = (rho_d / (2 * math.pi)) * math.log(
                            min(D_exit, d_img) / s_k)
                    if d_img > D_exit:
                        t4_mut_outer = (rho_n / (2 * math.pi)) * math.log(
                            d_img / max(D_exit, s_k))
                    t4_k = t4_mut_inner + t4_mut_outer
                else:
                    t4_k = (rho_n / (2 * math.pi)) * math.log(d_img / s_k)
                if k < n_phase:
                    t4_phase_mut += t4_k
                else:
                    t4_neutral_mut += t4_k

            t4_total = t4_self + t4_phase_mut + t4_neutral_mut
            t4_weighted = t4_self + t4_phase_mut + nf2 * t4_neutral_mut
            t4_air = t4_wall = 0.0
            # Direct burial: entire T4 is soil, so LF² applies to all of it.
            t4_eff = t4_total * llf

        # Conduit: all cables share one duct — total heat through external
        #   path is heat_factor * I²Rac, so T4 multiplied by heat_factor.
        # Direct burial: t4_weighted accounts for self-heating plus mutual
        #   from phases (weight 1) and neutrals (weight nf²).
        if is_conduit:
            T_total = t1 + heat_factor * t4_eff
        else:
            T_total = t1 + t4_weighted * llf

        # Mutual heating from parallel conduits
        # dT_mut = heat_factor * I^2 * Rac * rho_d * F_m * llf / (2*pi)
        # Fold the I^2*Rac-proportional part into the thermal resistance
        dT_mut = 0.0
        T_mutual_factor = 0.0
        nC = params.get("numCircuits", 1)
        if nC > 1:
            F_m = mutual_heating_factor(nC, params.get("circuitSpacing", 48) * 0.0254, L)
            T_mutual_factor = heat_factor * rho_d * F_m * llf / (2 * math.pi)

        dT_avail = max_temp - params["soilTemp"]
        T_total_eff = R_ac * (T_total + T_mutual_factor)
        I_calc = math.sqrt(max(0, dT_avail) / T_total_eff) if dT_avail > 0 and T_total_eff > 0 else 0.0

        # Compute actual mutual heating for reporting
        if nC > 1:
            W_total = heat_factor * I_calc ** 2 * R_ac
            dT_mut = W_total * rho_d * F_m * llf / (2 * math.pi)

        theta_new = params["soilTemp"] + I_calc ** 2 * R_ac * T_total + dT_mut
        if abs(theta_new - theta_c) < 0.05:
            theta_c = theta_new
            break
        theta_c = theta_new
    else:
        raise ValueError("LVAC thermal iteration did not converge")

    # Final values
    R_dc_f = dc_resistance_at_temp(cable["r_dc_20"], cable["alpha_20"], theta_c)
    ys_f = lv_skin_effect(R_dc_f)
    yp_f = lv_proximity_effect(R_dc_f, cable["conductor_diameter_mm"] / 1000,
                                cable["overall_diameter_mm"] / 1000)
    R_ac_f = R_dc_f * (1 + ys_f + yp_f)

    config_labels = {
        "single_phase": "1Φ (2 cables)",
        "split_phase": "Split Φ (3 cables)",
        "three_phase_delta": "3Φ Delta (3 cables)",
        "three_phase_wye": "3Φ Wye (4 cables)",
    }

    return {
        "ampacity": math.floor(I_calc * 0.95 * 10) / 10,
        "ampacityRaw": round(I_calc, 1),
        "conductorTemp": round(theta_c, 1),
        "Rac": round(R_ac_f * 1e6, 2),
        "Rdc": round(R_dc_f * 1e6, 2),
        "ys": round(ys_f * 100, 3),
        "yp": round(yp_f * 100, 3),
        "T1": round(dc_T1(cable), 4),
        "T4a": round(t4_air, 4),
        "T4w": round(t4_wall if is_conduit else 0, 4),
        "T4s": round(t4_total, 4),
        "T4t": round(t4_total, 4),
        "dTm": round(dT_mut, 1),
        "cableLabel": cable["label"],
        "maxTemp": cable["max_temp"],
        "cableOD": round(d_cable, 1),
        "groupOD": round(group_od, 1),
        "phaseConfig": config_labels.get(config, config),
        "totalCables": total_cables,
        "neutralFactor": neutral_factor,
        "systemType": "lvac",
        # Cable physical parameters
        "conductorDia": round(cable["conductor_diameter_mm"], 2),
        "overallDia": round(cable["overall_diameter_mm"], 2),
        "insulationThk": round((cable["overall_diameter_mm"] - cable["conductor_diameter_mm"]) / 2, 2),
        "Rdc20": round(cable["r_dc_20"] * 1e6, 2),
        "conductorMaterial": "Copper" if params.get("material") == "cu" else "Aluminum",
        "insulationType": INSULATION_TYPES[params.get("insulation", "xhhw")]["label"],
    }


# =============================================================================
# PDF REPORT GENERATION — ReportLab-backed, print-friendly
# =============================================================================

class _Canvas:
    """Top-left-origin drawing surface backed by reportlab's canvas.

    Mirrors the subset of the old _PDFWriter API used by the report so the
    layout code is a faithful port. reportlab handles font metrics, text
    measurement (real, not approximated) and PDF serialization.
    """

    def __init__(self, width=None, height=None):
        # Imported lazily so the calculation engines and HTTP server remain
        # stdlib-only; only PDF export depends on ReportLab.
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
        self.w, self.h = letter  # (612, 792)
        self._buf = io.BytesIO()
        self.c = canvas.Canvas(self._buf, pagesize=letter)
        self.c.setTitle("Gridworks EMBR Ampacity Study Report")
        self._font = "Helvetica"
        self._size = 10

    def _ty(self, y):
        return self.h - y

    # ── text ──
    def set_font(self, name, size):
        self._font, self._size = name, size
        self.c.setFont(name, size)

    def set_color(self, r, g, b):
        self.c.setFillColorRGB(r, g, b)

    def set_stroke_color(self, r, g, b):
        self.c.setStrokeColorRGB(r, g, b)

    def set_line_width(self, w):
        self.c.setLineWidth(w)

    def string_width(self, s, name=None, size=None):
        return self.c.stringWidth(s, name or self._font, size or self._size)

    def text(self, x, y, s):
        self.c.drawString(x, self._ty(y), s)

    def text_right(self, x, y, s):
        self.c.drawRightString(x, self._ty(y), s)

    def text_center(self, x, y, s):
        self.c.drawCentredString(x, self._ty(y), s)

    def text_fit(self, x, y, s, max_w, min_size=6.0):
        """Draw text at the current font, shrinking size to fit max_w."""
        size = self._size
        while size > min_size and self.string_width(s, self._font, size) > max_w:
            size -= 0.5
        self.c.setFont(self._font, size)
        self.c.drawString(x, self._ty(y), s)
        self.c.setFont(self._font, self._size)

    # ── primitives ──
    def line(self, x1, y1, x2, y2, width=0.5):
        self.c.setLineWidth(width)
        self.c.line(x1, self._ty(y1), x2, self._ty(y2))

    def rect_fill(self, x, y, w, h):
        self.c.rect(x, self._ty(y) - h, w, h, stroke=0, fill=1)

    def rect_stroke(self, x, y, w, h, lw=0.5):
        self.c.setLineWidth(lw)
        self.c.rect(x, self._ty(y) - h, w, h, stroke=1, fill=0)

    def rect_fill_stroke(self, x, y, w, h, lw=0.5):
        self.c.setLineWidth(lw)
        self.c.rect(x, self._ty(y) - h, w, h, stroke=1, fill=1)

    def circle(self, cx, cy, r, fill=True, stroke=True):
        self.c.circle(cx, self._ty(cy), r, stroke=1 if stroke else 0,
                      fill=1 if fill else 0)

    def dashed_line(self, x1, y1, x2, y2, dash=3, gap=2, width=0.5):
        self.c.setLineWidth(width)
        self.c.setDash(dash, gap)
        self.c.line(x1, self._ty(y1), x2, self._ty(y2))
        self.c.setDash()

    def build(self):
        self.c.showPage()
        self.c.save()
        return self._buf.getvalue()


def _hex(h):
    h = h.lstrip('#')
    return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255

# Print-friendly palette
_C = {
    "brand":  _hex('#D4920A'),   # dark gold for headers
    "accent": _hex('#E8A400'),   # slightly brighter for lines
    "orange": _hex('#C06010'),   # section headers
    "dark":   (0.12, 0.12, 0.14),
    "text":   (0.15, 0.15, 0.17),
    "gray":   (0.45, 0.44, 0.42),
    "ltgray": (0.72, 0.71, 0.68),
    "rule":   (0.82, 0.80, 0.76),
    "bg":     (0.95, 0.94, 0.92),
    "white":  (1, 1, 1),
    "black":  (0, 0, 0),
    "red":    (0.75, 0.18, 0.18),
    "blue":   (0.13, 0.40, 0.75),
    "green":  (0.15, 0.50, 0.22),
    "cond_a": (0.80, 0.20, 0.20),
    "cond_b": (0.20, 0.20, 0.20),
    "cond_c": (0.13, 0.40, 0.80),
    "cond_n": (0.55, 0.55, 0.55),
}


# Exact cable/conductor colors used by the on-screen SVG (embr.html drawXS),
# kept identical in the PDF so the graphic matches 1:1.
_XS_RED, _XS_BLK, _XS_BLU, _XS_GRY = "#cc3333", "#000000", "#2266cc", "#888888"


def _dc_od_in(params):
    """DC/LVAC cable OD in inches, mirroring getDcOD() fallbacks in the UI."""
    from_sizes = {"4/0": 17.27, "250": 18.90, "300": 20.32, "350": 21.59,
                  "400": 22.78, "500": 24.94, "600": 26.92, "750": 29.46, "1000": 33.27}
    size = params.get("cableSize", "500")
    ins = params.get("insulation", "xhhw")
    od_mm = CABLE_OD.get(size, {}).get(ins, from_sizes.get(size, 25))
    return od_mm / 25.4


def _xs_world(params):
    """Reproduce the on-screen SVG world extents and viewBox (drawXS in
    embr.html). Depends only on circuit count, spacing, depth and dryout."""
    nC = max(1, min(params.get("numCircuits", 1), 12))
    sp = params.get("circuitSpacing", 48)
    dep = params.get("burialDepth", 36)
    dH = 12 if params.get("useDryout", False) else 0
    wL = -(dH or 6) - 20
    wR = (nC - 1) * sp + (dH or 6) + 8
    wB = dep + (dH or 6) + 24
    grA = max(8, wB * 0.06 + (20 if nC > 1 else 0))
    wT = -grA
    wW = wR - wL
    wH = wB - wT
    svgW = 700.0
    svgH = max(280.0, min(520.0, svgW * (wH / wW) + 20))
    return {"nC": nC, "sp": sp, "dep": dep, "dH": dH, "wL": wL, "wR": wR,
            "wB": wB, "wT": wT, "wW": wW, "wH": wH, "svgW": svgW, "svgH": svgH}


def _draw_installation_graphic(pdf, x0, y0, w_avail, h_avail, params, result):
    """Cross-section that mirrors the on-screen SVG (drawXS in embr.html) 1:1 in
    geometry, scale and element placement. The whole drawing is built in the
    SVG's own coordinate space (700 x svgH) then uniformly scaled to fit the PDF
    box, so proportions and positions match the screen. Only background/accent
    colors are remapped to the print palette; cable/conductor/conduit colors are
    kept identical to the UI."""
    import math as _m
    W = _xs_world(params)
    nC, sp, dep, dH = W["nC"], W["sp"], W["dep"], W["dH"]
    wL, wT = W["wL"], W["wT"]
    svgW, svgH = W["svgW"], W["svgH"]
    sys_type = result.get("systemType", "mv")
    is_cond = params.get("installType") == "conduit"

    # ── Cable OD (inches) + per-system layout, mirroring drawMvXS/Dc/Lv ──
    if sys_type == "mv":
        od = (result.get("overallDia")
              or MV_CABLES.get(params.get("cableSize", "500"), MV_CABLES["500"])["overall_diameter_mm"]) / 25.4
        cables = [("a", -_m.pi / 2, _XS_RED), ("a", _m.pi / 6, _XS_BLK), ("a", 5 * _m.pi / 6, _XS_BLU)]
        mode, tR = "trefoil", od / _m.sqrt(3)
        leg = [(_XS_RED, "A"), (_XS_BLK, "B"), (_XS_BLU, "C")]
    elif sys_type == "dc":
        od = (result.get("cableOD") / 25.4) if result.get("cableOD") else _dc_od_in(params)
        tot = (params.get("conductorsPerConduit", 1)) * 2
        if tot == 2:
            grid = [(-0.5, 0, _XS_RED), (0.5, 0, _XS_BLK)]
        elif tot == 4:
            grid = [(-0.5, -0.5, _XS_RED), (0.5, -0.5, _XS_BLK), (-0.5, 0.5, _XS_RED), (0.5, 0.5, _XS_BLK)]
        else:
            grid = [(-0.5, -1, _XS_RED), (0.5, -1, _XS_BLK), (-0.5, 0, _XS_RED),
                    (0.5, 0, _XS_BLK), (-0.5, 1, _XS_RED), (0.5, 1, _XS_BLK)]
        cables = [("d", dx, dy, c) for (dx, dy, c) in grid]
        mode, tR = "grid", 0
        leg = [(_XS_RED, "DC+"), (_XS_BLK, "DC-")]
    else:  # lvac
        od = (result.get("cableOD") / 25.4) if result.get("cableOD") else _dc_od_in(params)
        cfg = params.get("phaseConfig", "three_phase_delta")
        if cfg == "single_phase":
            cables = [("d", -0.5, 0, _XS_RED), ("d", 0.5, 0, _XS_GRY)]
            leg = [(_XS_RED, "Hot"), (_XS_GRY, "N")]
            mode, tR = "grid", 0
        elif cfg == "split_phase":
            cables = [("d", -0.5, -0.43, _XS_RED), ("d", 0.5, -0.43, _XS_BLK), ("d", 0, 0.43, _XS_GRY)]
            leg = [(_XS_RED, "L1"), (_XS_BLK, "L2"), (_XS_GRY, "N")]
            mode, tR = "grid", 0
        elif cfg == "three_phase_delta":
            cables = [("a", -_m.pi / 2, _XS_RED), ("a", _m.pi / 6, _XS_BLK), ("a", 5 * _m.pi / 6, _XS_BLU)]
            leg = [(_XS_RED, "A"), (_XS_BLK, "B"), (_XS_BLU, "C")]
            mode, tR = "trefoil", od / _m.sqrt(3)
        else:  # three_phase_wye
            cables = [("d", -0.5, -0.5, _XS_RED), ("d", 0.5, -0.5, _XS_BLK),
                      ("d", -0.5, 0.5, _XS_BLU), ("d", 0.5, 0.5, _XS_GRY)]
            leg = [(_XS_RED, "A"), (_XS_BLK, "B"), (_XS_BLU, "C"), (_XS_GRY, "N")]
            mode, tR = "grid", 0
    r = od / 2

    coOD = coID = 0
    if is_cond:
        ci = params.get("conduitSize", 3)
        if 0 <= ci < len(CONDUIT_SIZES):
            coOD = CONDUIT_SIZES[ci]["od_mm"] / 25.4
            coID = CONDUIT_SIZES[ci]["id_mm"] / 25.4

    # ── SVG-space layout (identical math to drawXS) ──
    scw = min((svgW - 10) / W["wW"], (svgH - 10) / W["wH"])
    ox, oy = 5 - wL * scw, 5 - wT * scw
    def tx(v): return ox + v * scw
    def ty(v): return oy + v * scw
    gY, cY = ty(0), ty(dep)
    dP, cR, tRP = dH * scw, r * scw, tR * scw
    cOR, cIR = coOD * scw / 2, coID * scw / 2
    cW = cOR - cIR
    cts = [(tx(i * sp), cY) for i in range(nC)]

    # ── Uniform scale of the whole SVG space into the PDF box (centered) ──
    k = min(w_avail / svgW, h_avail / svgH)
    offX = x0 + (w_avail - svgW * k) / 2.0
    offY = y0 + (h_avail - svgH * k) / 2.0
    def PX(sx): return offX + sx * k
    def PY(sy): return offY + sy * k
    def SK(v): return v * k
    def FS(pt): return max(5.0, pt * k)

    BRAND = _C["brand"]
    GRADE = (0.40, 0.35, 0.28)

    def arrowhead(px, py, ux, uy, size):
        bx, by = px - ux * size, py - uy * size
        perpx, perpy = -uy, ux
        pdf.line(px, py, bx + perpx * size * 0.55, by + perpy * size * 0.55, max(0.4, SK(0.8)))
        pdf.line(px, py, bx - perpx * size * 0.55, by - perpy * size * 0.55, max(0.4, SK(0.8)))

    # ── Sky + soil ──
    pdf.set_color(0.94, 0.93, 0.90)
    pdf.rect_fill(PX(0), PY(0), SK(svgW), PY(gY) - PY(0))
    pdf.set_color(0.87, 0.83, 0.75)
    pdf.rect_fill(PX(0), PY(gY), SK(svgW), PY(svgH) - PY(gY))

    # ── Grade line + label ──
    pdf.set_stroke_color(*GRADE)
    pdf.line(PX(0), PY(gY), PX(svgW), PY(gY), max(0.8, SK(2)))
    pdf.set_font("Helvetica-Bold", FS(11))
    pdf.set_color(*GRADE)
    pdf.text(PX(10), PY(gY - 8), "GRADE")

    # ── Spacing dimension ──
    if nC > 1:
        ccY = (cY - dP if dP > 0 else cY - 20) - 16
        c0, c1 = cts[0][0], cts[1][0]
        pdf.set_stroke_color(*BRAND)
        pdf.line(PX(c0), PY(ccY), PX(c1), PY(ccY), max(0.4, SK(0.8)))
        arrowhead(PX(c0), PY(ccY), -1, 0, SK(6))
        arrowhead(PX(c1), PY(ccY), 1, 0, SK(6))
        pdf.set_font("Helvetica-Bold", FS(9))
        pdf.set_color(*BRAND)
        pdf.text_center(PX((c0 + c1) / 2), PY(ccY - 6), f'{sp}" c/c')

    # ── Dry-out zones ──
    if dP > 0:
        for (cx, cy) in cts:
            pdf.set_color(0.82, 0.77, 0.62)
            pdf.rect_fill(PX(cx - dP), PY(cy - dP), SK(2 * dP), SK(2 * dP))
            pdf.set_stroke_color(*BRAND)
            x1, y1, x2, y2 = PX(cx - dP), PY(cy - dP), PX(cx + dP), PY(cy + dP)
            d, g = max(2, SK(5)), max(1.5, SK(3))
            pdf.dashed_line(x1, y1, x2, y1, d, g, max(0.4, SK(1)))
            pdf.dashed_line(x1, y2, x2, y2, d, g, max(0.4, SK(1)))
            pdf.dashed_line(x1, y1, x1, y2, d, g, max(0.4, SK(1)))
            pdf.dashed_line(x2, y1, x2, y2, d, g, max(0.4, SK(1)))

    # ── Depth dimension ──
    dxw = tx(wL + 4)
    pdf.set_stroke_color(*BRAND)
    pdf.line(PX(dxw), PY(gY + 6), PX(dxw), PY(cY), max(0.4, SK(0.8)))
    arrowhead(PX(dxw), PY(gY + 6), 0, -1, SK(6))
    arrowhead(PX(dxw), PY(cY), 0, 1, SK(6))
    pdf.line(PX(dxw - 5), PY(cY), PX(dxw + 5), PY(cY), max(0.4, SK(0.8)))
    pdf.set_font("Helvetica-Bold", FS(9))
    pdf.set_color(*BRAND)
    pdf.text(PX(dxw + 7), PY((gY + cY) / 2 + 4), f'{dep}"')

    # ── Circuits: conduit ring, cables, label ──
    for ci, (cx, cy) in enumerate(cts):
        if is_cond and cOR > 0:
            ring = _hex("#3a3a3a") if params.get("conduitType") == "hdpe" else _hex("#6a6a6a")
            pdf.set_stroke_color(*ring)
            pdf.set_line_width(max(1.0, SK(max(2, cW))))
            pdf.circle(PX(cx), PY(cy), SK(cOR), fill=False, stroke=True)
            pdf.set_color(0.96, 0.95, 0.92)
            pdf.circle(PX(cx), PY(cy), SK(cIR), fill=True, stroke=False)
        for cb in cables:
            if cb[0] == "a":
                bx, by, col = cx + _m.cos(cb[1]) * tRP, cy + _m.sin(cb[1]) * tRP, cb[2]
            else:
                bx, by, col = cx + cb[1] * cR * 2, cy + cb[2] * cR * 2, cb[3]
            pdf.set_color(*_hex(col))
            pdf.circle(PX(bx), PY(by), max(1.0, SK(cR)), fill=True, stroke=False)
        labY = cy + (dP if dP > 0 else cR + 6) + 16
        pdf.set_font("Helvetica-Bold", FS(10))
        pdf.set_color(*BRAND)
        pdf.text_center(PX(cx), PY(labY), f"CKT {ci + 1}")

    # ── Legend (no background box, matching the UI) ──
    ly = svgH - 40
    lx = max(8, tx(wL + 2))
    for li, (col, lab) in enumerate(leg):
        px = 14 + li * 50
        pdf.set_color(*_hex(col))
        pdf.circle(PX(lx + px), PY(ly + 16), max(2, SK(5)), fill=True, stroke=False)
        pdf.set_font("Helvetica-Bold", FS(8))
        pdf.set_color(*_C["dark"])
        pdf.text(PX(lx + px + 8), PY(ly + 20), lab)




def generate_pdf_report(params, result, project_info):
    """Generate a one-page print-friendly PDF ampacity study report."""
    try:
        from datetime import datetime
        try:
            import reportlab  # noqa: F401
        except ImportError:
            return None, ("PDF export requires the ReportLab package, which is not "
                          "installed. Install dependencies with: pip install -r requirements.txt")
        pdf = _Canvas()
        W = 612
        M = 50
        CW = W - 2 * M
        now = datetime.now().strftime("%B %d, %Y")

        # ── HEADER ──
        pdf.set_color(*_C["accent"])
        pdf.rect_fill(0, 0, W, 3)
        pdf.set_font("Helvetica-Bold", 20)
        pdf.set_color(*_C["brand"])
        pdf.text(M, 28, "Gridworks EMBR")
        pdf.set_font("Helvetica", 9)
        pdf.set_color(*_C["gray"])
        pdf.text(M, 42, "Engineered Model for Buried-cable Ratings  |  v1.2  |  IEC 60287 / Neher-McGrath")
        pdf.set_font("Helvetica-Bold", 11)
        pdf.set_color(*_C["dark"])
        pdf.text_right(W - M, 22, "AMPACITY STUDY REPORT")
        pdf.set_font("Helvetica", 9)
        pdf.set_color(*_C["gray"])
        pdf.text_right(W - M, 36, now)
        pdf.set_color(*_C["accent"])
        pdf.rect_fill(M, 52, CW, 1.5)

        y = 66

        # ── PROJECT INFO ──
        pdf.set_color(*_C["bg"])
        pdf.rect_fill(M, y, CW, 42)
        proj_num = project_info.get("projectNumber") or "-"
        proj_name = project_info.get("projectName") or "-"
        engineer = project_info.get("engineerName") or "-"
        pdf.set_font("Helvetica-Bold", 8)
        pdf.set_color(*_C["gray"])
        pdf.text(M + 10, y + 14, "PROJECT")
        pdf.set_font("Helvetica", 9)
        pdf.set_color(*_C["text"])
        pdf.text(M + 75, y + 14, f"{proj_num}  /  {proj_name}")
        pdf.set_font("Helvetica-Bold", 8)
        pdf.set_color(*_C["gray"])
        pdf.text(M + 10, y + 30, "ENGINEER")
        pdf.set_font("Helvetica", 9)
        pdf.set_color(*_C["text"])
        pdf.text(M + 75, y + 30, engineer)
        pdf.set_font("Helvetica-Bold", 8)
        pdf.set_color(*_C["gray"])
        pdf.text(M + 300, y + 30, "DATE")
        pdf.set_font("Helvetica", 9)
        pdf.set_color(*_C["text"])
        pdf.text(M + 340, y + 30, now)

        y += 52

        # ── MAIN RESULT ──
        sys_type = result.get("systemType", "mv")
        sys_labels = {"mv": "MV Trefoil (35kV)", "dc": "DC", "lvac": "LVAC"}
        install_label = "In Conduit" if params.get("installType") == "conduit" else "Direct Burial"
        pdf.set_color(*_C["bg"])
        pdf.rect_fill(M, y, CW, 56)
        pdf.set_color(*_C["accent"])
        pdf.rect_fill(M, y, 3, 56)
        pdf.set_font("Helvetica", 8)
        pdf.set_color(*_C["gray"])
        pdf.text(M + 14, y + 14, "MAXIMUM AMPACITY (5% BUFFER)")
        pdf.set_font("Helvetica-Bold", 32)
        pdf.set_color(*_C["brand"])
        amp_str = str(result.get("ampacity", "-"))
        pdf.text(M + 14, y + 46, amp_str)
        amp_w = pdf.string_width(amp_str, "Helvetica-Bold", 32)
        pdf.set_font("Helvetica", 14)
        pdf.set_color(*_C["gray"])
        pdf.text(M + 14 + amp_w + 6, y + 43, "A")
        pdf.set_font("Helvetica", 9)
        pdf.set_color(*_C["text"])
        pdf.text_right(W - M - 10, y + 14, f"Unbuffered: {result.get('ampacityRaw', '-')} A")
        pdf.text_right(W - M - 10, y + 28, f"Conductor Temp: {result.get('conductorTemp', '-')} C")
        pdf.text_right(W - M - 10, y + 42, f"{result.get('cableLabel', '-')}  |  {install_label}")

        y += 66

        # ── INSTALLATION CROSS-SECTION ──
        col_w = CW / 2 - 5

        def section_title(label, sx, sy, sw=None):
            pdf.set_font("Helvetica-Bold", 8)
            pdf.set_color(*_C["orange"])
            pdf.text(sx, sy, label)
            pdf.set_color(*_C["rule"])
            pdf.rect_fill(sx, sy + 3, sw or col_w, 0.5)
            return sy + 14

        y = section_title("INSTALLATION CROSS-SECTION", M, y, CW)
        _w = _xs_world(params)
        graphic_h = max(150.0, min(330.0, (CW - 4) * _w["svgH"] / _w["svgW"]))
        pdf.set_stroke_color(*_C["rule"])
        pdf.rect_stroke(M, y - 2, CW, graphic_h + 4, 0.4)
        _draw_installation_graphic(pdf, M + 2, y, CW - 4, graphic_h, params, result)
        y += graphic_h + 12

        # ── THREE-COLUMN PARAMETERS ──
        col3_w = (CW - 20) / 3

        def section_title3(label, sx, sy):
            pdf.set_font("Helvetica-Bold", 8)
            pdf.set_color(*_C["orange"])
            pdf.text(sx, sy, label)
            pdf.set_color(*_C["rule"])
            pdf.rect_fill(sx, sy + 3, col3_w, 0.5)
            return sy + 14

        def kv_row(label, value, rx, ry, label_w=90):
            pdf.set_font("Helvetica", 7)
            pdf.set_color(*_C["gray"])
            pdf.text(rx, ry, label)
            pdf.set_font("Helvetica-Bold", 7)
            pdf.set_color(*_C["text"])
            # Shrink value to fit the remaining column width (prevents overflow
            # into the next column, e.g. long LVAC cable labels).
            pdf.text_fit(rx + label_w, ry, str(value), col3_w - label_w)
            # Lightweight separator rule under the row for readability.
            pdf.set_stroke_color(0.88, 0.86, 0.83)
            pdf.line(rx, ry + 3.5, rx + col3_w, ry + 3.5, 0.3)
            return ry + 11

        c1x = M
        c2x = M + col3_w + 10
        c3x = M + 2 * (col3_w + 10)
        y1 = section_title3("CABLE PARAMETERS", c1x, y)
        y2 = section_title3("INPUT PARAMETERS", c2x, y)
        y3 = section_title3("OUTPUT PARAMETERS", c3x, y)

        def mm_mils(val_mm):
            mils = val_mm / 25.4 * 1000
            return f'{val_mm} mm ({mils:.0f} mils)'

        # Column 1
        yr = y1
        yr = kv_row("Cable", result.get("cableLabel", "-"), c1x, yr)
        yr = kv_row("System", sys_labels.get(sys_type, sys_type), c1x, yr)
        if result.get("conductorMaterial"):
            yr = kv_row("Conductor", result["conductorMaterial"], c1x, yr)
        if result.get("insulationType"):
            yr = kv_row("Insulation", result["insulationType"], c1x, yr)
        if result.get("voltageClass"):
            yr = kv_row("Voltage Class", result["voltageClass"], c1x, yr)
        if result.get("voltageKv"):
            yr = kv_row("Operating Voltage", f'{result["voltageKv"]} kV', c1x, yr)
        if result.get("conductorDia"):
            yr = kv_row("Conductor Dia.", mm_mils(result["conductorDia"]), c1x, yr)
        if result.get("insulationDia"):
            yr = kv_row("Insulation Dia.", mm_mils(result["insulationDia"]), c1x, yr)
        if result.get("insulationThk"):
            yr = kv_row("Insulation Thk.", mm_mils(result["insulationThk"]), c1x, yr)
        if result.get("overallDia"):
            yr = kv_row("Overall Dia.", mm_mils(result["overallDia"]), c1x, yr)
        elif result.get("cableOD"):
            yr = kv_row("Cable OD", mm_mils(result["cableOD"]), c1x, yr)
        if result.get("jacketThk"):
            yr = kv_row("Jacket Thk.", mm_mils(result["jacketThk"]), c1x, yr)
        if result.get("cnWires"):
            yr = kv_row("CN Wires", f'{result["cnWires"]} x {mm_mils(result.get("cnWireDia", 0))}', c1x, yr)
        if result.get("maxTemp"):
            yr = kv_row("Max Temp", f'{result["maxTemp"]} C', c1x, yr)
        if result.get("Rdc20"):
            yr = kv_row("Rdc @ 20C", f'{result["Rdc20"]} uOhm/m', c1x, yr)
        y_end1 = yr

        # Column 2
        yl = y2
        rho_n = params.get("soilRhoNative", 0)
        rho_display = rho_n * 100 if rho_n < 10 else rho_n
        rho_d = params.get("soilRhoDry", 0)
        rho_d_display = rho_d * 100 if rho_d < 10 else rho_d
        yl = kv_row("Installation", install_label, c2x, yl)
        yl = kv_row("Burial Depth", f'{params.get("burialDepth", "-")}"', c2x, yl)
        yl = kv_row("Soil Temp", f'{params.get("soilTemp", "-")} C', c2x, yl)
        yl = kv_row("Soil Rho", f"{rho_display:.0f} C-cm/W", c2x, yl)
        if params.get("useDryout"):
            yl = kv_row("Dry-Out Rho", f"{rho_d_display:.0f} C-cm/W", c2x, yl)
        yl = kv_row("Dry-Out Zone", "Yes" if params.get("useDryout") else "No", c2x, yl)
        yl = kv_row("Load Factor", f'{params.get("loadFactor", "-")}', c2x, yl)
        yl = kv_row("Circuits", f'{params.get("numCircuits", 1)}', c2x, yl)
        if params.get("numCircuits", 1) > 1:
            yl = kv_row("Spacing", f'{params.get("circuitSpacing", "-")}" c/c', c2x, yl)
        if params.get("installType") == "conduit":
            ct = {"pvc40": "PVC Sch 40", "hdpe": "HDPE"}.get(params.get("conduitType", ""), "-")
            yl = kv_row("Conduit", ct, c2x, yl)
            ci = params.get("conduitSize", 3)
            if 0 <= ci < len(CONDUIT_SIZES):
                yl = kv_row("Conduit Size", CONDUIT_SIZES[ci]["label"], c2x, yl)
        if sys_type == "lvac":
            cfg_l = {"single_phase": "1-Phase", "split_phase": "Split Ph",
                     "three_phase_delta": "3Ph Delta", "three_phase_wye": "3Ph Wye"}
            yl = kv_row("Phase Config", cfg_l.get(params.get("phaseConfig", ""), "-"), c2x, yl)
            if params.get("phaseConfig") in ("split_phase", "three_phase_wye"):
                yl = kv_row("Neutral Factor", f'{params.get("neutralFactor", 0)}', c2x, yl)
        if sys_type == "dc":
            yl = kv_row("Ckts/Conduit", f'{params.get("conductorsPerConduit", 1)}', c2x, yl)
        y_end2 = yl

        # Column 3
        yr = y3
        yr = kv_row("Ampacity (buf)", f'{result.get("ampacity", "-")} A', c3x, yr)
        yr = kv_row("Ampacity (raw)", f'{result.get("ampacityRaw", "-")} A', c3x, yr)
        yr = kv_row("Cond. Temp", f'{result.get("conductorTemp", "-")} C', c3x, yr)
        if result.get("Rac") is not None and sys_type != "dc":
            yr = kv_row("R_ac", f'{result["Rac"]} uOhm/m', c3x, yr)
        if result.get("Rdc") is not None:
            yr = kv_row("R_dc", f'{result["Rdc"]} uOhm/m', c3x, yr)
        if result.get("ys") is not None and sys_type != "dc":
            yr = kv_row("Skin Eff (ys)", f'{result["ys"]}%', c3x, yr)
            yr = kv_row("Proximity (yp)", f'{result.get("yp", "-")}%', c3x, yr)
        if result.get("l1") is not None:
            yr = kv_row("Shield Loss", f'{result["l1"]}%', c3x, yr)
        if result.get("Wd") is not None:
            yr = kv_row("Diel. Loss", f'{result["Wd"]} W/m', c3x, yr)
        yr = kv_row("T1 (Insul.)", f'{result.get("T1", "-")} K-m/W', c3x, yr)
        if result.get("T3") is not None:
            yr = kv_row("T3 (Jacket)", f'{result["T3"]} K-m/W', c3x, yr)
        yr = kv_row("T4 (Ext. Total)", f'{result.get("T4t", "-")} K-m/W', c3x, yr)
        if result.get("T4a", 0) > 0:
            yr = kv_row("T4 (Duct Air)", f'{result["T4a"]} K-m/W', c3x, yr)
            yr = kv_row("T4 (Duct Wall)", f'{result.get("T4w", "-")} K-m/W', c3x, yr)
        yr = kv_row("Mutual dT", f'{result.get("dTm", "-")} C', c3x, yr)
        y_end3 = yr

        y = max(y_end1, y_end2, y_end3) + 12

        # ── FOOTER ──
        pdf.set_color(*_C["rule"])
        pdf.rect_fill(M, 762, CW, 0.5)
        pdf.set_font("Helvetica", 6.5)
        pdf.set_color(*_C["ltgray"])
        pdf.text(M, 773, "Gridworks EMBR  |  www.gridworks.energy  |  IEC 60287 / Neher-McGrath  |  Soil data: Open-Meteo.com")
        pdf.text_right(W - M, 773, f"Generated {now}")

        return pdf.build()

    except Exception:
        import traceback
        traceback.print_exc()  # keep detail server-side; do not leak to client
        return None, "PDF generation failed"


# =============================================================================
# INPUT VALIDATION
# =============================================================================
MAX_BODY_BYTES = 256 * 1024  # 256 KB cap on POST bodies
# Same-origin only unless an explicit origin is configured (review BE-2).
ALLOWED_ORIGIN = os.environ.get("EMBR_ALLOWED_ORIGIN", "").strip()


class ValidationError(ValueError):
    """Malformed or out-of-range API input; surfaced to the client as HTTP 400."""


_VALID = {
    "systemType": {"mv", "dc", "lvac"},
    "installType": {"direct", "conduit"},
    "conduitType": {"pvc40", "hdpe"},
    "material": {"cu", "al"},
    "insulation": {"thwn", "xhhw", "use2", "pv2kv"},
    "phaseConfig": {"single_phase", "split_phase",
                    "three_phase_delta", "three_phase_wye"},
}


def _v_num(params, key, lo, hi, default=None, integer=False):
    """Validate that params[key] is finite and within [lo, hi]."""
    if key not in params or params[key] is None:
        if default is None:
            raise ValidationError("Missing required numeric field: %s" % key)
        return default
    try:
        f = float(params[key])
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number" % key)
    if math.isnan(f) or math.isinf(f):
        raise ValidationError("%s must be a finite number" % key)
    if integer and f != int(f):
        raise ValidationError("%s must be a whole number" % key)
    if f < lo or f > hi:
        raise ValidationError("%s must be between %s and %s" % (key, lo, hi))
    return int(f) if integer else f


def _v_enum(params, key, default=None):
    v = params.get(key, default)
    if v not in _VALID[key]:
        raise ValidationError("%s must be one of %s" % (key, sorted(_VALID[key])))
    return v


def validate_params(params):
    """Validate /api/calculate (and export-pdf) input. Raises ValidationError
    (-> HTTP 400) on anything malformed or out of range. Bounds mirror the
    frontend's input limits so normal use never trips them; this exists to
    reject malformed or adversarial payloads before they reach the engines."""
    if not isinstance(params, dict):
        raise ValidationError("Request body must be a JSON object")

    system = _v_enum(params, "systemType")
    install = _v_enum(params, "installType", default="direct")

    _v_num(params, "burialDepth", 1, 600)
    _v_num(params, "soilTemp", -50, 100, default=25)
    _v_num(params, "soilRhoNative", 0.01, 100)
    if params.get("useDryout"):
        _v_num(params, "soilRhoDry", 0.01, 100)
    _v_num(params, "loadFactor", 0.01, 1.0, default=1.0)

    n_circ = _v_num(params, "numCircuits", 1, 12, default=1, integer=True)
    if n_circ > 1:
        _v_num(params, "circuitSpacing", 0.1, 1000)

    if install == "conduit":
        _v_enum(params, "conduitType", default="pvc40")
        _v_num(params, "conduitSize", 0, len(CONDUIT_SIZES) - 1, default=3, integer=True)

    if system == "mv":
        if params.get("cableSize") not in MV_CABLES:
            raise ValidationError("cableSize must be one of %s for MV" % sorted(MV_CABLES))
        if params.get("voltage_kv") is not None:
            v = _v_num(params, "voltage_kv", 0.1, 46)
            cls = MV_CABLES[params["cableSize"]].get("voltage_class_kv", 35)
            if v > cls:
                raise ValidationError(
                    "operating voltage %.4g kV exceeds the selected cable's %d kV "
                    "insulation class — choose a higher-class cable or lower the voltage" % (v, cls))
    else:  # dc / lvac
        if params.get("cableSize") not in CABLE_OD:
            raise ValidationError("cableSize must be one of %s" % sorted(CABLE_OD))
        _v_enum(params, "material")
        _v_enum(params, "insulation")
        if system == "dc":
            _v_num(params, "conductorsPerConduit", 1, 3, default=1, integer=True)
        else:  # lvac
            cfg = _v_enum(params, "phaseConfig")
            if cfg in ("split_phase", "three_phase_wye"):
                _v_num(params, "neutralFactor", 0, 1, default=0)
    return params


# =============================================================================
# HTTP SERVER
# =============================================================================

class AmpacityHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        # Pin the served root to the app directory (not the launch CWD) so only
        # the intended asset set is reachable regardless of where the process is
        # started; directory listings are disabled below. (review BE-3)
        super().__init__(*args, directory=_APP_DIR, **kwargs)

    def list_directory(self, path):
        self.send_error(404, "Not found")
        return None

    def _send_cors(self):
        # Same-origin only by default (no Access-Control-Allow-Origin header).
        # Set EMBR_ALLOWED_ORIGIN to permit a specific cross-origin caller later
        # without shipping open CORS. (review BE-2)
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)

    def _read_body(self):
        """Read the POST body with a bounded, validated Content-Length.
        Sends the error response and returns None on any problem."""
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw not in (None, "") else 0
        except (TypeError, ValueError):
            self._json_response(400, {"error": "Invalid Content-Length header"})
            return None
        if length < 0:
            self._json_response(400, {"error": "Invalid Content-Length header"})
            return None
        if length > MAX_BODY_BYTES:
            self._json_response(413, {"error": "Request body too large (max %d bytes)"
                                      % MAX_BODY_BYTES})
            return None
        try:
            return self.rfile.read(length)
        except Exception:
            self._json_response(400, {"error": "Could not read request body"})
            return None

    @staticmethod
    def _parse_json(body):
        """Parse a JSON object, rejecting NaN/Infinity and malformed bodies."""
        def _reject_const(tok):
            raise ValidationError("Non-finite JSON value not allowed: %s" % tok)
        try:
            return json.loads((body or b"").decode("utf-8") or "{}",
                              parse_constant=_reject_const)
        except ValidationError:
            raise
        except Exception:
            raise ValidationError("Request body is not valid JSON")

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if body is None:
            return  # error response already sent

        if path == "/api/calculate":
            try:
                params = self._parse_json(body)
                validate_params(params)
                engine = {"mv": compute_mv, "dc": compute_dc, "lvac": compute_lvac}
                result = engine[params["systemType"]](params)
                self._json_response(200, result)
            except ValidationError as e:
                self._json_response(400, {"error": str(e)})
            except Exception:
                import traceback
                traceback.print_exc()  # detail stays server-side
                self._json_response(500, {"error": "Internal calculation error"})

        elif path == "/api/export-pdf":
            try:
                data = self._parse_json(body)
                if not isinstance(data, dict):
                    raise ValidationError("Request body must be a JSON object")
                validate_params(data.get("params", {}))
                pdf_bytes = generate_pdf_report(
                    data.get("params", {}),
                    data.get("result", {}),
                    data.get("project", {}))
                if isinstance(pdf_bytes, tuple) or pdf_bytes is None:
                    self._json_response(500, {"error": "PDF generation failed"})
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", len(pdf_bytes))
                    self._send_cors()
                    self.end_headers()
                    self.wfile.write(pdf_bytes)
            except ValidationError as e:
                self._json_response(400, {"error": str(e)})
            except Exception:
                import traceback
                traceback.print_exc()  # detail stays server-side
                self._json_response(500, {"error": "Internal error generating PDF"})
        else:
            self._json_response(404, {"error": "Not found"})

    # Only these paths are served; everything else (source, configs, listings)
    # returns 404 so the app dir's non-asset files stay private. (review BE-3)
    _STATIC_ALLOW = {"/embr.html", "/gridworks_logo.png", "/favicon.ico"}

    def _resolve_static(self):
        """Return the servable path for a GET, or None if it is not allowed."""
        path = urlparse(self.path).path
        if path in ("", "/"):
            return "/embr.html"
        if path in self._STATIC_ALLOW:
            return path
        if ".." in path:
            return None
        if path.startswith("/docs/") and path.endswith(".pdf"):
            return path
        # Home-page carousel screenshots.
        if path.startswith("/docs/screenshots/") and path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            return path
        return None

    def do_GET(self):
        target = self._resolve_static()
        if target is None:
            self.send_error(404, "Not found")
            return
        self.path = target
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_HEAD(self):
        target = self._resolve_static()
        if target is None:
            self.send_error(404, "Not found")
            return
        self.path = target
        return SimpleHTTPRequestHandler.do_HEAD(self)

    def _json_response(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(data))
        self._send_cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        # Preflight is only sent cross-origin; with the same-origin default there
        # is no ACAO header, so cross-origin preflight fails (intended). (BE-2)
        self.send_response(204 if ALLOWED_ORIGIN else 200)
        self._send_cors()
        if ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8080))
    # Per-request socket timeout frees a worker thread stuck on a slow/partial
    # client instead of stalling the server (review BE-1).
    AmpacityHandler.timeout = 30
    httpd = ThreadingHTTPServer(("", PORT), AmpacityHandler)
    httpd.daemon_threads = True
    print(f"EMBR server running on port {PORT}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nEMBR server stopped")
    finally:
        httpd.server_close()

