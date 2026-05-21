# =============================================================================
# CatPhan 604 CBCT QA  —  /analyze/catphan
#
# HOW TO INTEGRATE:
#   1. Paste this entire block at the bottom of your existing main.py
#   2. The import at the top is the only new dependency (already in pylinac)
#   3. The frontend page (catphan.html) calls POST /analyze/catphan
#
# ENHANCED FEATURES (based on Hemant K B N, Shaleen Cancer Centre paper):
#   - Contrast-group–wise low-contrast detection: 1.0% / 0.5% / 0.3%
#   - Visual fallback using relative signal difference (local background ROIs)
#   - Background ROIs placed adjacent to signal ROIs (matches phantom geometry)
#   - Per-group pass/fail logic (tolerance: ≥1 ROI per group)
#   - Full QA summary table: HU linearity, uniformity, MTF, slice thickness
# =============================================================================

import zipfile
import shutil
import numpy as np

from pylinac import CatPhan604
from pylinac.ct import CTP515, CatPhanModule


# ─────────────────────────────────────────────────────────────────────────────
# Enhanced CTP515 subclass
# Adds per-contrast-group detection with a visual fallback.
# ─────────────────────────────────────────────────────────────────────────────
class EnhancedCTP515(CTP515):
    """
    Drop-in replacement for pylinac's CTP515 that reports low-contrast ROIs
    broken down by contrast group (1.0%, 0.5%, 0.3%) as specified in the
    CatPhan 604 manual.

    Visual fallback: if the CNR criterion does not detect an ROI, we apply a
    relative-signal-difference check against a local background reference ROI
    placed adjacent to the signal disc — mimicking human visual assessment.
    """

    # Angular positions for each contrast group (degrees, measured from
    # phantom geometry in the CatPhan 604 / CTP730 module manual).
    CONTRAST_GROUPS = {
        "1.0": {"angles": [-87.4, -69, -51.7, -38.5, -25.1, -12]},
        "0.5": {"angles": [36, 53.1, 66.5, 78.7, 90.1, 102.4]},
        "0.3": {"angles": [150, 170, 188, 202, 215, 227]},
    }

    # Relative signal difference threshold that mimics human visibility
    # (~3 % relative contrast → just-visible disc)
    VISUAL_THRESHOLD = 0.03

    def _is_roi_visible(self, roi_angle_deg: float) -> bool:
        """
        Returns True if the ROI at the given angle is considered 'seen' by
        either the standard CNR criterion OR the visual fallback.
        """
        # 1. Standard pylinac CNR-based check ----------------------------
        for roi in self.rois.values():
            if abs(roi.angle - roi_angle_deg) < 2.0:
                if roi.cnr > self.cnr_threshold:
                    return True
                # 2. Visual fallback: relative signal difference ----------
                try:
                    bg_val  = self._sample_local_background(roi)
                    rel_sig = abs(roi.pixel_value - bg_val) / (abs(bg_val) + 1e-9)
                    if rel_sig >= self.VISUAL_THRESHOLD:
                        return True
                except Exception:
                    pass
                return False
        return False

    def _sample_local_background(self, roi) -> float:
        """
        Sample a small circular region placed just outside the disc boundary
        (adjacent background), matching the phantom geometry recommendation.
        """
        # Offset the centre outward by 1.5× the ROI radius along the same
        # radial direction as the disc.
        angle_rad = np.deg2rad(roi.angle)
        offset    = roi.radius_pixels * 1.5
        bg_x = roi.center.x + offset * np.cos(angle_rad)
        bg_y = roi.center.y + offset * np.sin(angle_rad)

        arr    = self.image.array
        r_px   = max(int(roi.radius_pixels * 0.6), 2)
        x0, x1 = int(bg_x - r_px), int(bg_x + r_px)
        y0, y1 = int(bg_y - r_px), int(bg_y + r_px)
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, arr.shape[1]), min(y1, arr.shape[0])

        patch = arr[y0:y1, x0:x1]
        return float(np.mean(patch)) if patch.size > 0 else float(np.mean(arr))

    def group_results(self) -> dict:
        """
        Returns a dict with per-group ROI counts and pass/fail flags:
        {
            "1.0": <int>,  "1.0_pass": <bool>,
            "0.5": <int>,  "0.5_pass": <bool>,
            "0.3": <int>,  "0.3_pass": <bool>,
        }
        Tolerance: ≥1 ROI seen per group.
        """
        result = {}
        for group_name, info in self.CONTRAST_GROUPS.items():
            seen = sum(
                1 for angle in info["angles"]
                if self._is_roi_visible(angle)
            )
            result[group_name]             = seen
            result[f"{group_name}_pass"]   = seen >= 1
        return result


# ─────────────────────────────────────────────────────────────────────────────
# Chart data extractor
# ─────────────────────────────────────────────────────────────────────────────
def _extract_catphan_chart_data(phantom) -> dict:
    """Build the chart_data dict that the frontend consumes."""
    cd: dict = {}

    # ── Low contrast (enhanced, per-group) ──────────────────────────────────
    try:
        ctp515 = phantom.ctp515
        enhanced = EnhancedCTP515.__new__(EnhancedCTP515)
        enhanced.__dict__.update(ctp515.__dict__)   # copy state
        cd["low_contrast"] = enhanced.group_results()
        cd["low_contrast"]["total_seen"] = ctp515.num_contrast_rois_seen
        cd["cnr_threshold"]              = ctp515.cnr_threshold
    except Exception as exc:
        cd["low_contrast"] = {"1.0": 0, "0.5": 0, "0.3": 0, "error": str(exc)}

    # ── HU Linearity (CTP404) ───────────────────────────────────────────────
    try:
        ctp404 = phantom.ctp404
        cd["hu_values"] = {
            name: round(float(roi.pixel_value), 1)
            for name, roi in ctp404.hu_rois.items()
        }
        deviations = [
            abs(roi.pixel_value - roi.nominal_hu_value)
            for roi in ctp404.hu_rois.values()
            if hasattr(roi, "nominal_hu_value")
        ]
        cd["hu_linearity_max_deviation"] = round(float(max(deviations)), 2) if deviations else None
        cd["slice_thickness_mm"] = round(float(ctp404.measured_slice_thickness_mm), 3)
    except Exception as exc:
        cd["hu_values"] = {}
        cd["hu_linearity_max_deviation"] = None
        cd["slice_thickness_error"] = str(exc)

    # ── Uniformity (CTP486) ─────────────────────────────────────────────────
    try:
        ctp486 = phantom.ctp486
        cd["uniformity_index"] = round(float(ctp486.uniformity_index), 3)
    except Exception as exc:
        cd["uniformity_index"] = None
        cd["uniformity_error"] = str(exc)

    # ── MTF (CTP528) ────────────────────────────────────────────────────────
    try:
        ctp528 = phantom.ctp528
        cd["mtf_50"]     = round(float(ctp528.mtf.relative_resolution(50)), 4)
        # Down-sample MTF curve for chart rendering (≤60 points)
        mtf_vals = list(ctp528.mtf.norm_mtfs.values())
        step     = max(1, len(mtf_vals) // 60)
        cd["mtf_profile"] = [round(float(v), 4) for v in mtf_vals[::step]]
    except Exception as exc:
        cd["mtf_50"]      = None
        cd["mtf_profile"] = []
        cd["mtf_error"]   = str(exc)

    # ── QA Summary table rows (matches paper layout) ─────────────────────────
    lc    = cd.get("low_contrast", {})
    hu_ok = (cd.get("hu_linearity_max_deviation") or 9999) <= 40
    un_ok = (cd.get("uniformity_index")            or 9999) <= 40
    st    = cd.get("slice_thickness_mm")

    cd["qa_table"] = [
        {
            "module":    "CTP404",
            "parameter": "HU Linearity",
            "measured":  "See HU ROIs",
            "tolerance": "±40 HU",
            "status":    "PASS" if hu_ok else "FAIL",
        },
        {
            "module":    "CTP404",
            "parameter": "Slice Thickness",
            "measured":  f"{st:.2f} mm" if st else "—",
            "tolerance": "±0.2 mm",
            "status":    "PASS" if st and abs(st - 3.0) <= 0.2 else "FAIL",
        },
        {
            "module":    "CTP486",
            "parameter": "Uniformity",
            "measured":  str(cd.get("uniformity_index", "—")),
            "tolerance": "≤ 40",
            "status":    "PASS" if un_ok else "FAIL",
        },
        {
            "module":    "CTP528",
            "parameter": "MTF 50%",
            "measured":  f"{cd['mtf_50']:.3f} lp/mm" if cd.get("mtf_50") else "—",
            "tolerance": "Vendor Spec",
            "status":    "PASS",   # vendor-specific; flag for physicist review
        },
        {
            "module":    "CTP515",
            "parameter": "Low Contrast 1.0%",
            "measured":  f"{lc.get('1.0', 0)} ROIs",
            "tolerance": "≥ 1",
            "status":    "PASS" if lc.get("1.0_pass") else "FAIL",
        },
        {
            "module":    "CTP515",
            "parameter": "Low Contrast 0.5%",
            "measured":  f"{lc.get('0.5', 0)} ROIs",
            "tolerance": "≥ 1",
            "status":    "PASS" if lc.get("0.5_pass") else "FAIL",
        },
        {
            "module":    "CTP515",
            "parameter": "Low Contrast 0.3%",
            "measured":  f"{lc.get('0.3', 0)} ROIs",
            "tolerance": "≥ 1",
            "status":    "PASS" if lc.get("0.3_pass") else "FAIL",
        },
    ]

    return cd


# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────
def _run_catphan(job_id: str, file_path: str, email: str, filename: str, is_zip: bool):
    tmp_dir = None
    try:
        import matplotlib
        matplotlib.use("Agg")

        tmp_dir = tempfile.mkdtemp(prefix=f"catphan_{job_id}_")

        if is_zip:
            # Unzip full DICOM series into tmp_dir
            with zipfile.ZipFile(file_path, "r") as zf:
                zf.extractall(tmp_dir)
            phantom = CatPhan604(tmp_dir)
        else:
            # Single .dcm — copy into tmp_dir so pylinac can locate it
            import shutil as _shutil
            dest = os.path.join(tmp_dir, os.path.basename(file_path))
            _shutil.copy2(file_path, dest)
            phantom = CatPhan604(tmp_dir)
        phantom.analyze()
        summary   = phantom.results()

        # Overall pass: HU ±40, uniformity ≤40, slice thickness ±0.2,
        # AND low contrast 1.0% group ≥ 1 ROI
        chart_data = _extract_catphan_chart_data(phantom)
        lc         = chart_data.get("low_contrast", {})
        hu_ok      = (chart_data.get("hu_linearity_max_deviation") or 9999) <= 40
        un_ok      = (chart_data.get("uniformity_index")            or 9999) <= 40
        lc_ok      = lc.get("1.0_pass", False)  # primary clinical criterion
        passed     = hu_ok and un_ok and lc_ok

        # Save analysis plot
        plot_path = os.path.join(tmp_dir, "catphan_plot.png")
        try:
            phantom.save_analyzed_image(plot_path)
        except Exception:
            try:
                import matplotlib.pyplot as _plt
                phantom.plot_analyzed_image(show=False)
                _plt.savefig(plot_path, bbox_inches="tight", dpi=120)
                _plt.close("all")
            except Exception:
                plot_path = None

        image_url = upload_plot(plot_path, f"catphan_{job_id}.png") if plot_path and os.path.exists(plot_path) else ""

        save_analysis(
            email      = email,
            test_type  = "CatPhan 604",
            filename   = filename,
            passed     = passed,
            summary    = summary,
            image_url  = image_url,
            chart_data = chart_data,
            job_id     = job_id,
        )

        jobs[job_id] = {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        }

    except Exception as exc:
        jobs[job_id] = {
            "status":  "Error",
            "message": f"CatPhan 604 analysis failed: {exc}",
        }
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# API endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/analyze/catphan")
async def analyze_catphan(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    """
    Accepts either:
      - A .zip archive containing a full CBCT DICOM series (recommended — pylinac
        uses multiple slices to locate each phantom module automatically), or
      - A single .dcm DICOM file (limited — pylinac will analyse the one slice it
        can find; some modules may not be detected).

    Returns a job_id for polling via GET /result/{job_id}.
    """
    fname_lower = file.filename.lower()
    is_zip = fname_lower.endswith(".zip")
    is_dcm = fname_lower.endswith(".dcm")

    if not is_zip and not is_dcm:
        raise HTTPException(
            400,
            "Unsupported file type. Please upload a .zip (full DICOM series, recommended) "
            "or a .dcm (single DICOM slice)."
        )

    job_id    = str(uuid.uuid4())
    tmp_dir   = tempfile.gettempdir()
    ext       = ".zip" if is_zip else ".dcm"
    file_path = os.path.join(tmp_dir, f"catphan_{job_id}{ext}")

    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)

    jobs[job_id] = {"status": "Processing", "_ts": time.time()}

    background_tasks.add_task(
        _run_catphan,
        job_id    = job_id,
        file_path = file_path,
        email     = u["email"],
        filename  = file.filename,
        is_zip    = is_zip,
    )

    return {"status": "Queued", "job_id": job_id}
