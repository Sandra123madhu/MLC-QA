/**
 * dicom-detect.js — MLC QA file-type detection & wrong-test modal
 *
 * Exported helpers:
 *   detectDicomType(file)  → Promise<"picket_fence"|"starshot"|"winston_lutz"|"congruence"|"unknown">
 *   showWrongTestModal(opts)
 *   dismissWrongTestModal()
 *
 * NOTE: "Proceed Anyway" has been intentionally removed. Uploading the wrong
 * DICOM image type produces clinically meaningless or misleading results.
 * Wrong-type files are hard-blocked and must be redirected to the correct page.
 */

// ─── Keyword maps per test type ──────────────────────────────────────────────
const SIGNATURES = {
  picket_fence: {
    filename: [/picket/i, /\bpf[_\-\s]/i, /mlc[_\-\s]fence/i, /leaf[_\-\s]pos/i, /mlcqa/i, /pf_test/i, /pf-test/i],
    header:    [/picket[\s_\-]?fence/i, /PicketFence/, /mlc.*picket/i, /picket.*mlc/i,
                /Leaf\s*\d+\s*(Bank|Pair)/i, /DMLC|SMLC/, /MLC QA/i,
                /leaf[\s_\-]?position/i, /mlc_qa/i, /mlcfence/i],
  },
  starshot: {
    filename: [/starshot/i, /star[_\-\s]shot/i, /gantry[_\-\s]rot/i, /collimator[_\-\s]rot/i, /spoke/i],
    header:   [/starshot/i, /star[\s_\-]?shot/i, /spoke[\s_\-]?angle/i, /wobble/i,
               /collimator.*rotat/i, /gantry.*spoke/i, /radiation[\s_\-]?spoke/i],
  },
  winston_lutz: {
    filename: [/winston/i, /\bwl\b/i, /wl[_\-\s]/i, /[_\-\s]wl/i, /winston[_\-]lutz/i,
               /ball[_\-\s]?bear/i, /isocent/i],
    header:   [/winston[\s_\-]?lutz/i, /WinstonLutz/, /ball[\s_\-]?bearing/i,
               /\bBB\b.*marker/i, /isocenter.*bb/i, /bb.*isocenter/i,
               /gantry.*angle.*bb/i, /radiation[\s_\-]?isocent/i],
  },
  congruence: {
    filename: [/congruence/i, /field[_\-\s]?size/i, /light[_\-\s]?field/i, /rad[_\-\s]?light/i,
               /open[_\-\s]?field/i, /flatness/i, /symmetry/i],
    header:   [/congruence/i, /field[\s_\-]?analysis/i, /light[\s_\-]?field/i,
               /radiation[\s_\-]?field/i, /field[\s_\-]?size/i, /flatness/i, /symmetry/i,
               /field[\s_\-]?edge/i],
  },
};

async function readDicomHeader(file) {
  try {
    const buf  = await file.slice(0, 8192).arrayBuffer();
    const text = new TextDecoder("latin1").decode(new Uint8Array(buf));
    return text;
  } catch (_) { return ""; }
}

function hasDicomMagic(header) {
  return header.slice(128, 132) === "DICM";
}

function scoreType(type, filename, header) {
  const sig = SIGNATURES[type];
  let score = 0;
  for (const pat of sig.filename) { if (pat.test(filename)) score += 3; }
  for (const pat of sig.header)   { if (pat.test(header))   score += 2; }
  return score;
}

/**
 * Detect the likely test type of a .dcm file.
 * Returns: "picket_fence" | "starshot" | "winston_lutz" | "congruence" | "unknown"
 */
async function detectDicomType(file) {
  const filename = file.name.toLowerCase();
  const header   = await readDicomHeader(file);

  if (!filename.endsWith(".dcm")) return "unknown";

  const scores = {
    picket_fence:  scoreType("picket_fence",  filename, header),
    starshot:      scoreType("starshot",       filename, header),
    winston_lutz:  scoreType("winston_lutz",  filename, header),
    congruence:    scoreType("congruence",     filename, header),
  };

  const best = Object.entries(scores).sort((a, b) => b[1] - a[1])[0];
  return best[1] > 0 ? best[0] : "unknown";
}

// ─── Shared wrong-test modal HTML ─────────────────────────────────────────────

/**
 * Inject the shared wrong-test modal into the page if not already present.
 * Call once at page load (or lazily on first mismatch).
 */
function ensureWrongTestModal() {
  if (document.getElementById("wrongTestModal")) return;
  const div = document.createElement("div");
  div.innerHTML = `
  <div id="wrongTestModal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(6,12,24,0.88);backdrop-filter:blur(6px);align-items:center;justify-content:center;">
    <div style="background:var(--surface);border:1px solid rgba(201,64,80,0.5);border-left:4px solid var(--fail);border-radius:var(--r-lg);padding:32px 28px;max-width:480px;width:92%;box-shadow:0 8px 48px rgba(0,0,0,0.6);">
      <div style="display:flex;align-items:center;gap:14px;margin-bottom:18px;">
        <div style="width:46px;height:46px;border-radius:50%;background:rgba(201,64,80,0.18);border:1.5px solid rgba(201,64,80,0.45);display:flex;align-items:center;justify-content:center;font-size:1.4rem;flex-shrink:0;">&#9888;</div>
        <div>
          <div style="font-family:var(--mono);font-size:0.95rem;font-weight:600;color:var(--fail);letter-spacing:0.05em;">UNACCEPTABLE DATA — WRONG TEST FILE</div>
          <div style="font-size:0.75rem;color:var(--text-muted);margin-top:3px;font-family:var(--mono);">File type mismatch — analysis blocked</div>
        </div>
      </div>
      <p style="font-size:0.85rem;color:var(--text-2);line-height:1.7;margin-bottom:10px;">
        The selected file appears to be a <strong id="wtm-detected-name" style="color:var(--accent);">…</strong> image,
        not a <strong id="wtm-page-name" style="color:var(--text-1);">…</strong> file.
      </p>
      <p style="font-size:0.85rem;color:var(--text-2);line-height:1.7;margin-bottom:22px;">
        Analysing the wrong image type will produce <strong style="color:var(--fail);">clinically meaningless or misleading results</strong>.
        This action is <strong style="color:var(--fail);">blocked</strong> to protect data integrity.
        Please upload this file on the correct page:
      </p>
      <a id="wtm-correct-link" href="#" style="display:flex;align-items:center;gap:12px;padding:13px 16px;background:rgba(43,159,212,0.08);border:1px solid var(--accent-border);border-radius:var(--r);text-decoration:none;margin-bottom:20px;">
        <span id="wtm-correct-icon" style="font-size:1.2rem;">&#128194;</span>
        <div>
          <div id="wtm-correct-label" style="font-size:0.85rem;font-weight:600;color:var(--accent);">Go to correct test</div>
          <div id="wtm-correct-sub"   style="font-size:0.74rem;color:var(--text-muted);margin-top:2px;">…</div>
        </div>
        <span style="margin-left:auto;color:var(--text-muted);font-size:1rem;">&#8594;</span>
      </a>
      <div style="display:flex;gap:10px;">
        <button onclick="dismissWrongTestModal()" style="width:100%;padding:11px 0;background:transparent;border:1px solid var(--border);border-radius:var(--r);color:var(--text-muted);font-size:0.83rem;cursor:pointer;font-family:var(--mono);">&#8592; Choose a Different File</button>
      </div>
      <p style="font-size:0.72rem;color:var(--text-muted);margin-top:14px;text-align:center;line-height:1.5;">Detection is based on DICOM metadata and filename heuristics. If you believe this is a false positive, please verify the file type before uploading.</p>
    </div>
  </div>`;
  document.body.appendChild(div.firstElementChild);
}

// ─── Modal show / dismiss ─────────────────────────────────────────────────────

function showWrongTestModal(opts) {
  ensureWrongTestModal();

  if (opts.analyzeBtn) opts.analyzeBtn.disabled = true;

  const LABELS = {
    picket_fence:  { name: "Picket Fence",  icon: "⚡", href: "mlc-qa.html" },
    starshot:      { name: "Starshot",      icon: "✦", href: "starshot.html" },
    winston_lutz:  { name: "Winston-Lutz", icon: "◎", href: "winston-lutz.html" },
    congruence:    { name: "Congruence",   icon: "⊞", href: "congruence.html" },
  };

  const detected = LABELS[opts.detectedType] || { name: "a different test", icon: "📂", href: "#" };
  const page     = LABELS[opts.pageType]     || { name: "this test",         icon: "📂", href: "#" };

  document.getElementById("wtm-detected-name").textContent  = detected.name;
  document.getElementById("wtm-correct-icon").textContent   = detected.icon;
  document.getElementById("wtm-correct-label").textContent  = `Go to ${detected.name} Test`;
  document.getElementById("wtm-correct-sub").textContent    = `This file belongs on the ${detected.name} page`;
  document.getElementById("wtm-correct-link").href          = detected.href;
  document.getElementById("wtm-page-name").textContent      = page.name;
  document.getElementById("wrongTestModal").style.display   = "flex";
}

function dismissWrongTestModal() {
  const modal = document.getElementById("wrongTestModal");
  if (modal) modal.style.display = "none";

  // Clear every file input on the page
  document.querySelectorAll("input[type=file]").forEach(inp => { inp.value = ""; });

  // Reset label + button
  const label = document.getElementById("fileLabel");
  const btn   = document.getElementById("analyzeBtn");
  if (label) label.textContent = "";
  if (btn)   btn.disabled = true;
}

/**
 * proceedAnyway() is intentionally disabled.
 * Wrong-type DICOM files are hard-blocked for clinical data integrity.
 */
function proceedAnyway() {
  console.warn("[MLC QA] proceedAnyway() is disabled. Wrong file types are blocked.");
}

// Backwards-compat shim — pages that checked isWrongFileConfirmed() before submitting
function isWrongFileConfirmed() { return false; }

/**
 * checkFileAcceptable(file, pageType, analyzeBtn)
 * Called by each page on file selection and before submission.
 * Returns true if the file is acceptable, false if blocked (modal shown).
 */
async function checkFileAcceptable(file, pageType, analyzeBtn) {
  const ext = file.name.split(".").pop().toLowerCase();
  if (ext !== "dcm") return true; // ZIP or other — skip type detection

  const detected = await detectDicomType(file);
  if (detected === "unknown" || detected === pageType) return true;

  showWrongTestModal({ detectedType: detected, pageType, analyzeBtn });
  return false;
}
