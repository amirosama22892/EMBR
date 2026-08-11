/*
 * Frontend integration tests for The Three Phase Ampacity Calculator.
 *
 * Runs the real inline script inside jsdom and drives it the way a user would,
 * asserting on user-visible behavior (Testing Trophy — integration over unit).
 * Covers the bugs fixed in the 2026-07 bug review + code-review pass so they
 * cannot silently regress:
 *   - csz(): conduit-size index 0 is a valid choice (was falsy-coerced to 3)
 *   - mvOD(): a bad/blank cable size must not throw
 *   - calculate(): non-2xx responses surface an error instead of failing silently
 *   - FE-2: a slow earlier response can never overwrite a newer one (latest-wins)
 *   - FE-3: server-echoed values render as text, not HTML (no markup injection)
 *   - FE-4: soil preset dropdown is keyboard-operable (roles / aria / focus)
 *   - showModule() / syncSoil() golden paths
 *
 * Run:  node test_frontend.js        (exit 0 = all pass)
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const HTML = fs.readFileSync(path.join(__dirname, "embr.html"), "utf8");

const results = [];
function check(name, cond, detail) {
  results.push([name, !!cond, detail || ""]);
}

// A valid-looking MV result so renderResults() and load-time calculate() work.
function mvResult(ampacity) {
  return {
    ampacity: ampacity, ampacityRaw: ampacity, conductorTemp: 90,
    cableLabel: "500 kcmil Cu", systemType: "mv", phaseConfig: "",
    Rac: 22.1, ys: 1.2, yp: 0.9, l1: 3.4, Wd: 0.46, tOD: 110,
    T1: 0.4, T4t: 1.2, T4a: 0, T4w: 0, dTm: 0.5, maxTemp: 90,
    Rdc: 20, cableOD: 51, groupOD: 110, voltageKv: 34.5, voltageClass: "35kV",
  };
}

// Controllable fetch mock. Tests set MOCK to shape/delay the next response(s).
let MOCK = () => ({ ok: true, status: 200, body: mvResult(100), delay: 0 });
function makeResponse(spec) {
  return {
    ok: spec.ok, status: spec.status,
    json: () => new Promise((res) => setTimeout(() => res(spec.body), spec.delay || 0)),
  };
}

const jsErrors = [];
const dom = new JSDOM(HTML, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = (url, opts) => {
      if (opts && opts.body) window.__lastPost = opts.body;
      const spec = MOCK(url, opts);
      return new Promise((resolve) => setTimeout(() => resolve(makeResponse(spec)), 0));
    };
    window.scrollTo = () => {};
    window.alert = () => {};
    window.addEventListener("error", (e) => jsErrors.push(String(e.error || e.message)));
  },
});
const { window } = dom;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

window.addEventListener("load", async () => {
  const $ = (id) => window.document.getElementById(id);
  try {
    // ---- T1: page loaded and initialized without throwing ----
    await wait(250); // let the debounced load-time calculate() settle
    check("page loads and initializes without a JS error", jsErrors.length === 0, jsErrors.join(" | "));
    check("core elements exist (rAmp, detailGrid, mod-home)", !!($("rAmp") && $("detailGrid") && $("mod-home")));
    check("The Three Phase logo and Ampacity Calculator branding render",
      window.document.querySelector('.logo-img')?.getAttribute('src') === '/three_phase_logo.png' &&
      window.document.querySelector('.header-title')?.textContent === 'Ampacity Calculator' &&
      !window.document.body.textContent.includes('Gridworks'));
    check("new configuration exports use The Three Phase format",
      window.gatherCurrentConfig()._format === 'the-three-phase-ampacity-config');
    check("cross-section uses the brown-and-gold reference palette",
      $("xsection").innerHTML.includes('#5A4A3A') &&
      $("xsection").innerHTML.includes('#FBB52B'));

    // ---- T2: module navigation ----
    window.showModule("about");
    check("showModule('about') shows About, hides Home",
      $("mod-about").style.display !== "none" && $("mod-home").style.display === "none");
    window.showModule("calculator");
    check("showModule('calculator') shows Calculator", $("mod-calculator").style.display !== "none");

    // ---- T3: csz() — conduit index 0 is valid, blank falls back to 3 ----
    const cz = $("mv_conduitSize");
    cz.value = "0";
    check("csz() returns 0 for a valid index-0 selection (regression)", window.csz("mv_conduitSize") === 0,
      "got " + window.csz("mv_conduitSize"));
    cz.value = "";
    check("csz() falls back to 3 for a blank value", window.csz("mv_conduitSize") === 3,
      "got " + window.csz("mv_conduitSize"));

    // ---- T4: mvOD() guards a bad cable size ----
    const cs = $("mv_cableSize");
    const prev = cs.value;
    cs.value = "NOT_A_SIZE";
    let threw = false, od;
    try { od = window.mvOD(); } catch (e) { threw = true; }
    check("mvOD() does not throw on a bad cable size and returns a default", !threw && od > 0,
      "threw=" + threw + " od=" + od);
    cs.value = prev;

    // ---- T5: calculate() surfaces a server error (non-2xx) ----
    window.currentSystem = "mv";
    MOCK = () => ({ ok: false, status: 500, body: {}, delay: 0 });
    window.calculate();
    await wait(60);
    check("calculate() surfaces a non-2xx response in the error box",
      !$("errorBox").classList.contains("hidden") && /500/.test($("errorBox").textContent),
      "hidden=" + $("errorBox").classList.contains("hidden") + " text=" + $("errorBox").textContent);

    // ---- T6: calculate() happy path renders the ampacity and clears the error ----
    MOCK = () => ({ ok: true, status: 200, body: mvResult(321), delay: 0 });
    window.calculate();
    await wait(60);
    check("calculate() happy path renders ampacity and hides the error box",
      $("rAmp").textContent === "321" && $("errorBox").classList.contains("hidden"),
      "rAmp=" + $("rAmp").textContent);

    // ---- T7: FE-2 latest-wins — a slow earlier response must not clobber a newer one ----
    let call = 0;
    MOCK = () => {
      call += 1;
      // 1st call resolves LATE with 111; 2nd resolves FAST with 222.
      return call === 1
        ? { ok: true, status: 200, body: mvResult(111), delay: 80 }
        : { ok: true, status: 200, body: mvResult(222), delay: 10 };
    };
    window.calculate(); // request A (slow, 111)
    window.calculate(); // request B (fast, 222) — the latest
    await wait(200);
    check("FE-2: latest response wins; stale earlier response is discarded",
      $("rAmp").textContent === "222", "rAmp=" + $("rAmp").textContent);

    // ---- T8: FE-3 — server-echoed label renders as text, not HTML ----
    MOCK = () => ({ ok: true, status: 200, body: Object.assign(mvResult(200),
      { cableLabel: "<img src=x onerror=window.__pwned=1>EVIL" }), delay: 0 });
    window.calculate();
    await wait(60);
    const injected = $("rInfo").querySelector("img");
    check("FE-3: malicious cableLabel is not parsed as HTML (no injected <img>)",
      injected === null && !window.__pwned && /EVIL/.test($("rInfo").textContent),
      "img=" + injected + " pwned=" + window.__pwned);

    // ---- T9: FE-4 — soil dropdown accessibility wiring ----
    const menu = $("mv_soilRhoNative_menu");
    const firstOpt = menu.querySelector("button");
    const arrow = $("mv_soilRhoNative").parentNode.querySelector(".soil-arrow");
    check("FE-4: soil menu has role=menu and options role=menuitem/tabindex=-1",
      menu.getAttribute("role") === "menu" && firstOpt.getAttribute("role") === "menuitem" &&
      firstOpt.getAttribute("tabindex") === "-1");
    check("FE-4: toggle carries aria-haspopup and aria-expanded",
      arrow.getAttribute("aria-haspopup") === "true" && arrow.getAttribute("aria-expanded") === "false");
    window.toggleSoilMenu("mv_soilRhoNative");
    check("FE-4: opening the menu sets aria-expanded and moves focus into it",
      arrow.getAttribute("aria-expanded") === "true" && window.document.activeElement === firstOpt,
      "expanded=" + arrow.getAttribute("aria-expanded"));
    window.closeSoilMenus("mv_soilRhoNative");
    check("FE-4: closing resets aria-expanded", arrow.getAttribute("aria-expanded") === "false");

    // ---- T10: syncSoil() links the paired field for a recognized preset ----
    const presets = window.SOIL_PRESETS;
    const key = Object.keys(presets)[0];
    $("mv_soilRhoNative").value = String(presets[key].nat);
    window.syncSoil("mv", "native");
    check("syncSoil(): choosing a native preset fills the paired dry-out field",
      Number($("mv_soilRhoDry").value) === presets[key].dry,
      "dry=" + $("mv_soilRhoDry").value + " expected=" + presets[key].dry);

    // ---- T11: FE-5 — soil lookup surfaces a non-ok API response ----
    $("soilLookupInput").value = "Nowheresville";
    MOCK = () => ({ ok: false, status: 503, body: {}, delay: 0 });
    window.soilLookupSearch();
    await wait(80);
    check("FE-5: soil lookup surfaces a non-ok API response (not a silent no-data)",
      /error/i.test($("soilLookupStatus").textContent) || /HTTP/.test($("soilLookupStatus").textContent),
      "status=" + $("soilLookupStatus").textContent);

    // ---- T12: home carousel renders slides + dots and navigates (wrap-around) ----
    const slides = [
      { src: "/docs/screenshots/01-mv-trefoil-direct.png", cap: "MV trefoil" },
      { src: "/docs/screenshots/02-mv-multi-circuit.png", cap: "Multi circuit" },
      { src: "/docs/screenshots/03-mv-conduit.png", cap: "Conduit" },
    ];
    window.renderCarousel(slides);
    const vp = $("carouselViewport"), dots = $("carouselDots");
    check("carousel builds one slide + one dot per image and reveals the section",
      vp.children.length === 3 && dots.children.length === 3 && $("homeShots").style.display !== "none",
      "slides=" + vp.children.length + " dots=" + dots.children.length);
    check("carousel: first slide starts active",
      vp.children[0].classList.contains("active") && dots.children[0].classList.contains("active"));
    window.carMove(1);
    check("carMove(1) advances to the second slide",
      vp.children[1].classList.contains("active") && !vp.children[0].classList.contains("active"));
    window.carMove(-1);
    window.carMove(-1);
    check("carMove(-1) wraps around to the last slide",
      vp.children[2].classList.contains("active"), "active idx unexpected");
    check("carousel: alt text is set on each slide image (a11y)",
      vp.children[0].querySelector("img").getAttribute("alt") === "MV trefoil");

    // ---- T13: a single-image carousel hides the arrows and dots ----
    window.renderCarousel([{ src: "/docs/screenshots/01-mv-trefoil-direct.png", cap: "solo" }]);
    check("single-slide carousel hides prev/next arrows and dots",
      $("carPrev").style.display === "none" && $("carNext").style.display === "none" &&
      $("carouselDots").style.display === "none");
    if (typeof window.carStop === "function") window.carStop();

    // ---- T14: MV voltage-class dropdown is grouped and includes 15/25 kV ----
    const sel = $("mv_cableSize");
    const groups = sel.querySelectorAll("optgroup");
    const values = Array.from(sel.querySelectorAll("option")).map((o) => o.value);
    check("cable dropdown is grouped by voltage class (3 optgroups)", groups.length === 3,
      "groups=" + groups.length);
    check("cable dropdown includes 15 kV and 25 kV options and keeps legacy 35 kV keys",
      values.includes("15kv_500") && values.includes("25kv_1000") && values.includes("500"),
      values.join(","));

    // ---- T15: operating-voltage field + class-nominal auto-fill ----
    const vf = $("mv_voltage");
    check("operating-voltage field exists and defaults to 34.5", vf && vf.value === "34.5");
    sel.value = "15kv_500"; window.mvCableChanged();
    check("choosing a 15 kV cable sets operating voltage to 13.8", vf.value === "13.8", "v=" + vf.value);
    sel.value = "25kv_750"; window.mvCableChanged();
    check("choosing a 25 kV cable sets operating voltage to 24", vf.value === "24", "v=" + vf.value);
    sel.value = "500"; window.mvCableChanged();
    check("choosing a 35 kV cable sets operating voltage to 34.5", vf.value === "34.5", "v=" + vf.value);

    // ---- T16: calculate() sends voltage_kv in the request body ----
    window.currentSystem = "mv";
    sel.value = "15kv_1000"; window.mvCableChanged(); // -> 13.8
    MOCK = () => ({ ok: true, status: 200, body: mvResult(400), delay: 0 });
    window.__lastPost = null;
    window.calculate();
    await wait(40);
    const sent = JSON.parse(window.__lastPost || "{}");
    check("calculate() includes cableSize and voltage_kv in the POST body",
      sent.cableSize === "15kv_1000" && sent.voltage_kv === 13.8,
      "cableSize=" + sent.cableSize + " voltage_kv=" + sent.voltage_kv);

    // ---- T17: over-class voltage is blocked client-side (no request sent) ----
    window.currentSystem = "mv";
    sel.value = "15kv_500"; window.mvCableChanged();      // -> 13.8, valid
    $("mv_voltage").value = "24";                          // manually over the 15 kV class
    window.__lastPost = null;
    window.calculate();
    await wait(40);
    check("over-class operating voltage is blocked before any request is sent",
      window.__lastPost === null && !$("errorBox").classList.contains("hidden") &&
      /15 kV cable class/.test($("errorBox").textContent),
      "post=" + window.__lastPost + " err=" + $("errorBox").textContent);
    // a valid class/voltage pair calculates normally again
    $("mv_voltage").value = "13.8";
    MOCK = () => ({ ok: true, status: 200, body: mvResult(300), delay: 0 });
    window.calculate();
    await wait(40);
    check("a valid class/voltage pair calculates normally", $("rAmp").textContent === "300");
  } catch (e) {
    check("test harness ran to completion", false, "harness threw: " + (e && e.stack || e));
  }

  // ---- report ----
  console.log("=".repeat(74));
  console.log("The Three Phase Ampacity Calculator frontend integration tests");
  console.log("=".repeat(74));
  let n = 0;
  for (const [name, ok, detail] of results) {
    console.log((ok ? "PASS " : "FAIL ") + name + (ok ? "" : "   [" + detail + "]"));
    n += ok ? 1 : 0;
  }
  console.log("-".repeat(74));
  console.log(n + "/" + results.length + " passed");
  dom.window.close();
  process.exit(n === results.length ? 0 : 1);
});
