#!/usr/bin/env python3
"""
CIGRE TB 880 verification harness for EMBR
==========================================
Verifies EMBR's IEC 60287 implementation against the published worked examples
in CIGRE Technical Brochure 880 (2022), "Power cable rating examples for
calculation tool verification".

Two provenance tiers are reported for each quantity:
  [EMBR] - computed by EMBR's PRODUCTION functions (imported from embr-server.py)
  [IEC ] - computed here from the IEC 60287 baseline formula that EMBR's
           production thermal/shield models extend (see report for details)

Only TB 880's numeric reference values are embedded (with citation); the
brochure text is not reproduced. TB 880 is CIGRE copyright.

Scope: the two cases nearest EMBR's MV single-core / trefoil / direct-buried
domain — Case 4 (33 kV land cable, full rating) and Case 1 (132 kV, loss core).
Both cases are 50 Hz; EMBR's MV_FREQ is set accordingly for verification.
"""
import importlib.util, math, os

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("embr_server", os.path.join(_HERE, "embr-server.py"))
embr = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(embr)
embr.MV_FREQ = 50  # TB 880 cases are 50 Hz


def _Tlayer(rho, D_over, D_under):
    return rho / (2 * math.pi) * math.log(D_over / D_under)


def _rating(R, Wd, lam1, T1, T2, T3, T4, dtheta, n=1, lam2=0.0):
    """IEC 60287-1-1 §1.4.1.1 permissible current rating."""
    num = dtheta - Wd * (0.5 * T1 + n * (T2 + T3 + T4))
    den = R * T1 + n * R * (1 + lam1) * T2 + n * R * (1 + lam1 + lam2) * (T3 + T4)
    return math.sqrt(num / den)


# ---------------------------------------------------------------------------
# CASE 4 : 33 kV land cable, single-core Cu XLPE, close trefoil, direct buried
# TB 880 Section 8. Full end-to-end rating reproduction.
# ---------------------------------------------------------------------------
def compute_case4():
    theta, alpha = 90.0, 3.93e-3
    R0 = 0.0754e-3
    Rp = embr.dc_resistance_at_temp(R0, alpha, theta)          # R' at operating temp
    ys = embr.mv_skin_effect(Rp, ks=1.0)                       # [EMBR] production
    yp = embr.mv_proximity_effect(Rp, 0.0184, 0.044, kp=1.0)   # [EMBR] production
    R = Rp * (1 + ys + yp)

    # Dielectric loss (IEC 60287-1-1 §2.2)
    Di, dsc, eps, tand = 34.8e-3, 19.4e-3, 2.5, 0.004
    Uo = 33000 / math.sqrt(3); w = 2 * math.pi * embr.MV_FREQ
    C = eps / (18 * math.log(Di / dsc)) * 1e-9
    Wd = w * C * Uo ** 2 * tand

    # Thermal resistances (IEC 60287-2-1). T1/T3 carry the <50%-cover
    # correction factors (1.07 / 1.6) called out in TB 880 §8.
    T1 = 1.07 * (_Tlayer(2.5, 19.4, 18.4) + _Tlayer(3.5, 34.8, 19.4)
                 + _Tlayer(2.5, 35.8, 34.8) + _Tlayer(6.0, 36.8, 35.8))
    T2 = 0.0
    T3 = 1.6 * (_Tlayer(6.0, 39.2, 38.6) + _Tlayer(3.5, 43.6, 39.2)
                + _Tlayer(2.5, 44.0, 43.6))
    De, L, rho_soil = 44e-3, 1.0, 1.0
    u = 2 * L / De
    T4 = 1.5 / math.pi * rho_soil * (math.log(2 * u) - 0.630)   # touching-trefoil (IEC)

    # Screen circulating-loss factor lambda1 (IEC 60287-1-1 §2.3), iterated
    ns, ds, Ls, Ds = 56, 0.9e-3, 240e-3, 36.8e-3
    rho_s, alpha_s, s = 1.7241e-8, 3.93e-3, 44e-3
    As = ns * math.pi / 4 * ds ** 2
    d_mean = Ds + ds
    LFs = math.sqrt(1 + (math.pi * d_mean / Ls) ** 2)
    Rs0 = LFs * rho_s / As
    X = 2 * w * 1e-7 * math.log(2 * s / d_mean)
    I, lam1, theta_s = 500.0, 0.0, theta
    for _ in range(500):
        theta_s = theta - (I ** 2 * R + 0.5 * Wd) * T1
        Rs = Rs0 * (1 + alpha_s * (theta_s - 20))
        lam1 = (Rs / R) / (1 + (Rs / X) ** 2)
        I_new = _rating(R, Wd, lam1, T1, T2, T3, T4, dtheta=theta - 20)
        if abs(I_new - I) < 1e-9:
            I = I_new; break
        I = I_new

    return {
        "R' (Ω/m)":   (Rp, 9.614254000e-5, "IEC"),
        "y_s":        (ys, 8.835005445e-3, "EMBR"),
        "y_p":        (yp, 6.622704052e-3, "EMBR"),
        "R_ac (Ω/m)": (R, 9.762868345e-5, "EMBR"),
        "W_d (W/m)":  (Wd, 0.10842143853, "IEC"),
        "λ₁":         (lam1, 0.0435122656, "IEC"),
        "T1 (K·m/W)": (T1, 0.4110322351, "IEC"),
        "T3 (K·m/W)": (T3, 0.12419418991, "IEC"),
        "T4 (K·m/W)": (T4, 1.8524966955, "IEC"),
        "I (A)":      (I, 537.4631099368, "IEC"),
    }


# ---------------------------------------------------------------------------
# CASE 1 : 132 kV single-core Cu XLPE, close trefoil, direct buried.
# TB 880 Section 5. Loss-core verification (Milliken + laminated-foil thermal
# chain is outside EMBR's MV cable family; conductor-loss engine is verified).
# ---------------------------------------------------------------------------
def compute_case1_losscore():
    theta, alpha = 90.0, 3.93e-3
    R0 = 0.0151e-3
    Rp = embr.dc_resistance_at_temp(R0, alpha, theta)
    ys = embr.mv_skin_effect(Rp, ks=0.8)                        # [EMBR] Milliken ks
    yp = embr.mv_proximity_effect(Rp, 0.0431, 0.098, kp=0.37)   # [EMBR] Milliken kp
    R = Rp * (1 + ys + yp)
    Di, dsc, eps, tand = 80.5e-3, 46.7e-3, 2.5, 1e-3
    Uo = 132000 / math.sqrt(3); w = 2 * math.pi * embr.MV_FREQ
    C = eps / (18 * math.log(Di / dsc)) * 1e-9
    Wd = w * C * Uo ** 2 * tand
    return {
        "R' (Ω/m)":   (Rp, 1.925401e-5, "IEC"),
        "y_s":        (ys, 0.1275058631, "EMBR"),
        "y_p":        (yp, 0.0229311323, "EMBR"),
        "R_ac (Ω/m)": (R, 2.2150525415e-5, "EMBR"),
        "W_d (W/m)":  (Wd, 0.4654100053, "IEC"),
    }


def _report(title, results, tol=1e-3):
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(f"{'Quantity':14s} {'src':5s} {'EMBR / IEC':>18s} {'TB 880 published':>18s} {'Δ%':>9s}  result")
    allpass = True
    for name, (val, ref, src) in results.items():
        d = abs(val - ref) / abs(ref) if ref else abs(val)
        ok = d <= tol
        allpass = allpass and ok
        print(f"{name:14s} [{src:4s}] {val:18.9g} {ref:18.9g} {d*100:9.4f}  {'PASS' if ok else 'FAIL'}")
    print(f"{'':>68s}{'ALL PASS' if allpass else 'FAILURES'}")
    print()
    return allpass


if __name__ == "__main__":
    p1 = _report("CIGRE TB 880 Case 4 — 33 kV land cable (full rating)  [tol 0.1%]",
                 compute_case4(), tol=1e-3)
    p2 = _report("CIGRE TB 880 Case 1 — 132 kV trefoil (IEC 60287 loss core)  [tol 0.05%]",
                 compute_case1_losscore(), tol=5e-4)
    import sys
    sys.exit(0 if (p1 and p2) else 1)
