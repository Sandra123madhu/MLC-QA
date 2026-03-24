/**
 * dicom-detect.js — MLC QA file-type detection & wrong-test modal
 *
 * Exported helpers:
 *   detectDicomType(file)  → Promise<"picket_fence"|"starshot"|"winston_lutz"|"unknown">
 *   showWrongTestModal(opts)
 *   dismissWrongTestModal()
 *   proceedAnyway()
 *
 * Each page calls detectDicomType() on file selection and compares the
 * result against the page's own expected type.
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
};

/**
 * Read first 8 KB of a File and return its text (latin-1).
 */
async function readDicomHeader(file) {
  try {
    const buf  = await file.slice(0, 8192).arrayBuffer();
    const text = new TextDecoder("latin1").decode(new Uint8Array(buf));
    return text;
  } catch (_) { return ""; }
}

/**
 * Returns true if the 4-byte DICM magic is present at offset 128.
 */
function hasDicomMagic(header) {
  return header.slice(128, 132) === "DICM";
}

/**
 * Score a candidate type against filename + header text.
 * Returns an integer score (higher = stronger match).
 */
function scoreType(type, filename, header) {
  const sig = SIGNATURES[type];
  let score = 0;
  for (const pat of sig.filename) { if (pat.test(filename)) score += 3; }
  for (const pat of sig.header)   { if (pat.test(header))   score += 2; }
  return score;
}

/**
 * Detect the likely test type of a .dcm file.
 * Returns: "picket_fence" | "starshot" | "winston_lutz" | "unknown"
 */
async function detectDicomType(file) {
  const filename = file.name.toLowerCase();
  const header   = await readDicomHeader(file);

  // Must be a .dcm to bother inspecting
  if (!filename.endsWith(".dcm")) return "unknown";

  // Not a valid DICOM at all → skip
  if (!hasDicomMagic(header)) {
    // Still try filename-only scoring for badly-formed files
  }

  const scores = {
    picket_fence:  scoreType("picket_fence",  filename, header),
    starshot:      scoreType("starshot",       filename, header),
    winston_lutz:  scoreType("winston_lutz",  filename, header),
  };

  const best = Object.entries(scores).sort((a, b) => b[1] - a[1])[0];
  return best[1] > 0 ? best[0] : "unknown";
}

// ─── Modal helpers ────────────────────────────────────────────────────────────

let _wrongFileConfirmed = false;

function isWrongFileConfirmed() { return _wrongFileConfirmed; }

/**
 * opts = {
 *   detectedType: "picket_fence"|"starshot"|"winston_lutz",
 *   pageType:     "picket_fence"|"starshot"|"winston_lutz",
 *   analyzeBtn:   HTMLElement,
 * }
 */
function showWrongTestModal(opts) {
  _wrongFileConfirmed = false;
  if (opts.analyzeBtn) opts.analyzeBtn.disabled = true;

  const LABELS = {
    picket_fence:  { name: "Picket Fence",  icon: "⚡", href: "mlc-qa.html" },
    starshot:      { name: "Starshot",      icon: "✦", href: "starshot.html" },
    winston_lutz:  { name: "Winston-Lutz", icon: "◎", href: "winston-lutz.html" },
  };

  const detected = LABELS[opts.detectedType] || { name: "a different test", icon: "📂", href: "#" };
  const page     = LABELS[opts.pageType]     || { name: "this test",         icon: "📂", href: "#" };

  const modal = document.getElementById("wrongTestModal");
  document.getElementById("wtm-detected-name").textContent  = detected.name;
  document.getElementById("wtm-correct-icon").textContent   = detected.icon;
  document.getElementById("wtm-correct-label").textContent  = `Go to ${detected.name} Test`;
  document.getElementById("wtm-correct-sub").textContent    = `This file belongs on the ${detected.name} page`;
  document.getElementById("wtm-correct-link").href          = detected.href;
  document.getElementById("wtm-page-name").textContent      = page.name;
  modal.style.display = "flex";
}

function dismissWrongTestModal() {
  document.getElementById("wrongTestModal").style.display = "none";
  _wrongFileConfirmed = false;

  // Clear every file input on the page
  document.querySelectorAll("input[type=file]").forEach(inp => {
    inp.value = "";
  });
  // Reset label + button (pages use these ids)
  const label = document.getElementById("fileLabel");
  const btn   = document.getElementById("analyzeBtn");
  if (label) label.textContent = "";
  if (btn)   btn.disabled = true;
}

function proceedAnyway() {
  document.getElementById("wrongTestModal").style.display = "none";
  _wrongFileConfirmed = true;
}
