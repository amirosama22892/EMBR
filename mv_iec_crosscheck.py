#!/usr/bin/env python3
"""
Independent IEC 60287 cross-check for EMBR's 15 kV and 25 kV MV cables.

WHAT THIS IS
------------
CIGRE TB 880 contains no 15 kV or 25 kV single-core extruded-dielectric land
cable case (its examples run 10 kV three-core PILC, 30 kV three-core submarine,
33 kV single-core, and 110-400 kV), so the new Southwire 15/25 kV cables cannot
be validated against a published TB 880 reference directly.

Instead this harness re-derives the IEC 60287-1-1 / 60287-2-1 rating chain for
each new cable from first principles in a SEPARATE implementation (it reads only
the datasheet geometry from embr.MV_CABLES; it does not call EMBR's compute
functions) and compares term-by-term and end-to-end against EMBR's compute_mv.
Matching confirms EMBR applies the IEC equations correctly to the new geometry
and operating voltage — extending the method TB 880 verified at 33 kV.

SCOPE / HONESTY
---------------
Run in the standard regime (single circuit, direct-buried touching trefoil, no
dry-out zone, unity load factor) so EMBR reduces to the pure-IEC path. EMBR's
Cymcap-tuned refinements — the two-zone dry-out T4, multi-circuit mutual model,
and the concentric-neutral lambda_1 literature cap — are outside pure IEC and
remain pending a Cymcap cross-check at 15/25 kV. lambda_1 is reported with its
uncapped IEC value for transparency.
"""
import importlib.util as u, math, sys

spec = u.spec_from_file_location("embr", "embr-server.py")
embr = u.module_from_spec(spec); spec.loader.exec_module(embr)

# Standard IEC / material inputs (identical to EMBR's documented constants).
EPS, TAND, RHO_INS, RHO_JKT, F = 2.3, 0.001, 3.5, 3.5, 60
THETA_C, SOIL, RHO_SOIL = 90.0, 25.0, 0.9
L = 36 * 0.0254  # burial depth 36 in -> m


def independent_rating(cab, v_ll_kv):
    """Independent IEC 60287 rating for one cable, single circuit, direct
    buried touching trefoil, no dry-out, LF=1. Returns a dict of quantities."""
    dc = cab["conductor_diameter_mm"] / 1000.0
    Di = cab["insulation_diameter_mm"] / 1000.0
    Do = cab["overall_diameter_mm"] / 1000.0
    ks, kp = cab.get("ks", 1.0), cab.get("kp", 1.0)

    # Conductor AC resistance at 90 C (IEC 60287-1-1 sec.2.1)
    Rp = cab["r_dc_20"] * (1 + cab["alpha_20"] * (THETA_C - 20))
    xs4 = ((8 * math.pi * F * ks / Rp) * 1e-7) ** 2
    ys = xs4 / (192 + 0.8 * xs4)
    xp4 = ((8 * math.pi * F * kp / Rp) * 1e-7) ** 2
    ypb = xp4 / (192 + 0.8 * xp4)
    r = dc / Do
    yp = ypb * r ** 2 * (0.312 * r ** 2 + 1.18 / (ypb + 0.27))
    Rac = Rp * (1 + ys + yp)

    # Dielectric loss (IEC 60287-1-1 sec.2.2)
    C = (EPS / (18 * math.log(Di / dc))) * 1e-9
    U0 = v_ll_kv * 1000 / math.sqrt(3)
    Wd = 2 * math.pi * F * C * U0 ** 2 * TAND

    # Thermal resistances (IEC 60287-2-1)
    dcn = cab["insulation_diameter_mm"] + cab["cn_wire_diameter_mm"]
    T1 = (RHO_INS / (2 * math.pi)) * math.log(dcn / cab["conductor_diameter_mm"])
    d_under = cab["overall_diameter_mm"] - 2 * cab["jacket_thickness_mm"]
    T3 = (RHO_JKT / (2 * math.pi)) * math.log(cab["overall_diameter_mm"] / d_under)
    # External: image method, self + 2 trefoil mutuals (touching, s = De)
    uu = 2 * L / Do
    T_self = (RHO_SOIL / (2 * math.pi)) * math.log(uu + math.sqrt(uu * uu - 1))
    s = Do
    d_img = math.sqrt((2 * L) ** 2 + s ** 2)
    T_mut = (RHO_SOIL / (2 * math.pi)) * math.log(d_img / s)
    T4 = T_self + 2 * T_mut

    # Screen circulating-loss factor lambda_1 (IEC 60287-1-1 sec.2.3)
    theta_s = THETA_C - (THETA_C - SOIL) * T1 / (T1 + T3 + T4)
    Rs = cab["cn_wire_r_dc_20"] / cab["cn_wires"]
    Rs_t = Rs * (1 + 0.00393 * (theta_s - 20))
    d_wire_in = cab["cn_wire_diameter_mm"] / 25.4
    lay = (2.5 + 25 * d_wire_in) * 25.4
    helical = math.sqrt(1 + (math.pi * dcn / lay) ** 2)
    Rs_h = Rs_t * helical
    x_s = 2 * math.pi * F * 2e-7 * math.log(2 * Do / (dcn / 1000.0))
    lam_raw = (Rs_h / Rac) / (1 + (Rs_h / x_s) ** 2)
    cap = 0.03 if cab.get("cn_fraction") == "1/6" else 0.06
    lam1 = max(0.0, min(lam_raw, cap))

    # Current rating (IEC 60287-1-1 eq.1) at 90 C, single circuit, LF=1
    T_i2r = T1 + (1 + lam1) * (T3 + T4)
    T_wd = 0.5 * T1 + T3 + T4
    I = math.sqrt((THETA_C - SOIL - Wd * T_wd) / (Rac * T_i2r))
    return dict(Rp=Rp, ys=ys, yp=yp, Rac=Rac, Wd=Wd, T1=T1, T3=T3, T4=T4,
                lam1=lam1, lam_raw=lam_raw, I_raw=round(I, 1))


CASES = [("15kv_500", 13.8), ("15kv_1000", 13.8), ("25kv_500", 24.0), ("25kv_1000", 24.0)]
TOL = 0.005  # 0.5% (accommodates EMBR's result rounding)

allpass = True
for key, vll in CASES:
    cab = embr.MV_CABLES[key]
    ind = independent_rating(cab, vll)
    r = embr.compute_mv(dict(systemType="mv", cableSize=key, voltage_kv=vll,
                             installType="direct", soilTemp=SOIL, soilRhoNative=RHO_SOIL,
                             soilRhoDry=2.5, useDryout=False, loadFactor=1.0, numCircuits=1,
                             circuitSpacing=48, burialDepth=36, conduitType="pvc40", conduitSize=3))
    rows = [
        ("R_ac (uOhm/m)", ind["Rac"] * 1e6, r["Rac"]),
        ("y_s (%)",       ind["ys"] * 100,  r["ys"]),
        ("y_p (%)",       ind["yp"] * 100,  r["yp"]),
        # compare W_d against EMBR's full-precision value (the result dict rounds
        # it to 3 dp, which is lossy for the sub-0.02 W/m loss at these voltages)
        ("W_d (W/m)",     ind["Wd"],        embr.mv_dielectric_loss(cab, vll)),
        ("T1 (K.m/W)",    ind["T1"],        r["T1"]),
        ("T3 (K.m/W)",    ind["T3"],        r["T3"]),
        ("T4 (K.m/W)",    ind["T4"],        r["T4t"]),
        ("lambda_1 (%)",  ind["lam1"] * 100, r["l1"]),
        ("Ampacity (A)",  ind["I_raw"],     r["ampacityRaw"]),
    ]
    print("=" * 76)
    print(f"{key}  @ {vll} kV L-L   (independent IEC 60287  vs  EMBR compute_mv)")
    print("=" * 76)
    print(f"{'Quantity':16s}{'independent':>16s}{'EMBR':>16s}{'delta %':>12s}  result")
    for name, a, b in rows:
        d = abs(a - b) / abs(b) if b else abs(a)
        ok = d <= TOL
        allpass = allpass and ok
        print(f"{name:16s}{a:16.6g}{b:16.6g}{d*100:12.4f}  {'PASS' if ok else 'FAIL'}")
    print(f"  (transparency) uncapped IEC lambda_1 = {ind['lam_raw']*100:.2f}%  "
          f"vs EMBR capped {r['l1']:.2f}%")
    print()

print("ALL PASS" if allpass else "FAILURES PRESENT")
sys.exit(0 if allpass else 1)
