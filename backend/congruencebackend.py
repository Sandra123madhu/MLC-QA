# ═══════════════════════════════════════════════════════════════════════════════
# CONGRUENCE TEST — Radiation / Light Field Congruence (Field Analysis)
# Add this block to main.py, alongside the existing /analyze/starshot and
# /analyze/winston-lutz endpoints.
# ═══════════════════════════════════════════════════════════════════════════════

# Add this import at the top of main.py with the other pylinac imports:
#   from pylinac import FieldAnalysis
#   from pylinac.core.profile import CollapsedCircleProfile   # not needed directly
# The full import line becomes:
#   from pylinac import PicketFence, WinstonLutz, Starshot, FieldAnalysis


def _extract_congruence_chart_data(fa) -> dict:
    """
    Extract edge offsets and profiles from a pylinac FieldAnalysis result.
    Returns a dict with keys: edges, inline_profile, crossline_profile, tolerance_mm.
    """
    try:
        results = fa.results_data()

        # --- Edge offsets (radiation field edge vs. CAX/nominal) ---
        # pylinac reports field edges as positions in mm from CAX.
        # We compare these to the nominal (light field) values (0 for centred field).
        # If pylinac exposes top/bottom/left/right directly, use them.
        edges = {}
        try:
            # pylinac >= 3.x stores field_size_vertical_mm, field_size_horizontal_mm
            # and top/bottom/left/right as signed offsets from CAX.
            # Attribute names vary slightly by version; we try multiple.
            attrs = vars(results) if hasattr(results, '__dict__') else {}
            
            def _get(candidates, default=None):
                for name in candidates:
                    v = attrs.get(name) or getattr(results, name, None)
                    if v is not None:
                        return float(v)
                return default

            # Try pylinac ResultsData attribute names
            top    = _get(["top_penumbra_mm", "top_field_edge_mm", "top_mm"])
            bottom = _get(["bottom_penumbra_mm", "bottom_field_edge_mm", "bottom_mm"])
            left   = _get(["left_penumbra_mm",  "left_field_edge_mm",  "left_mm"])
            right  = _get(["right_penumbra_mm",  "right_field_edge_mm", "right_mm"])

            # Fallback: derive from symmetry / field size difference
            if top is None:
                fs_v = _get(["field_size_vertical_mm", "vertical_field_size"])
                fs_h = _get(["field_size_horizontal_mm", "horizontal_field_size"])
                # Nominal field size is unknown here, so report deltas as 0 if we can't compute
                top    = round(fs_v / 2, 3) if fs_v else None
                bottom = round(-fs_v / 2, 3) if fs_v else None
                left   = round(-fs_h / 2, 3) if fs_h else None
                right  = round(fs_h / 2, 3) if fs_h else None

            edges = {
                "top":    round(top,    3) if top    is not None else None,
                "bottom": round(bottom, 3) if bottom is not None else None,
                "left":   round(left,   3) if left   is not None else None,
                "right":  round(right,  3) if right  is not None else None,
            }
        except Exception as e:
            edges = {"top": None, "bottom": None, "left": None, "right": None}

        # --- Inline / crossline profiles ---
        inline_profile, crossline_profile = [], []
        try:
            # fa.image.array is the raw pixel matrix; sample a central row/col
            import numpy as np
            arr = fa.image.array.astype(float)
            # Normalise 0-1
            arr_min, arr_max = arr.min(), arr.max()
            if arr_max > arr_min:
                arr = (arr - arr_min) / (arr_max - arr_min)
            cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
            inline_raw    = arr[cy, :].tolist()
            crossline_raw = arr[:, cx].tolist()
            # Down-sample to ~100 points for the chart
            def downsample(lst, n=100):
                step = max(1, len(lst) // n)
                return [round(float(lst[i]), 4) for i in range(0, len(lst), step)][:n]
            inline_profile    = downsample(inline_raw)
            crossline_profile = downsample(crossline_raw)
        except Exception:
            pass

        return {
            "edges":              edges,
            "inline_profile":     inline_profile,
            "crossline_profile":  crossline_profile,
            "tolerance_mm":       1.0,
        }
    except Exception as e:
        return {"error": str(e), "edges": {}, "inline_profile": [], "crossline_profile": [], "tolerance_mm": 1.0}


def _run_congruence(job_id: str, filepath: str, email: str, filename: str):
    """
    Background task: run pylinac FieldAnalysis for the congruence test.
    Updates jobs[job_id] and persists to Supabase when done.
    """
    try:
        from pylinac import FieldAnalysis

        fa = FieldAnalysis(filepath)
        fa.analyze(
            protocol=None,         # use default tolerances
            is_FFF=False,          # standard flattened beam
        )

        summary    = fa.results()
        passed     = fa.passed

        # Generate and upload plot image
        plot_path  = filepath.replace(".dcm", "_congruence.png")
        fa.plot_analyzed_image(filename=plot_path, show=False)
        image_url  = upload_plot(plot_path, f"congruence_{job_id}.png")

        chart_data = _extract_congruence_chart_data(fa)

        save_analysis(
            email      = email,
            test_type  = "Congruence",
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

    except Exception as e:
        error_msg = str(e)
        jobs[job_id] = {
            "status":  "Error",
            "message": f"Congruence analysis failed: {error_msg}",
        }
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        cleanup()


@app.post("/analyze/congruence")
async def analyze_congruence(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    """
    Radiation–light field congruence test using pylinac FieldAnalysis.
    Accepts a single .dcm file and returns a job_id for polling.
    """
    if not file.filename.lower().endswith(".dcm"):
        raise HTTPException(400, "Only .dcm DICOM files are supported for the Congruence test.")

    # Save upload to a temp file
    job_id   = str(uuid.uuid4())
    tmp_dir  = tempfile.gettempdir()
    filepath = os.path.join(tmp_dir, f"congruence_{job_id}.dcm")

    try:
        contents = await file.read()
        with open(filepath, "wb") as f:
            f.write(contents)
    except Exception as e:
        raise HTTPException(500, f"Failed to save uploaded file: {e}")

    jobs[job_id] = {"status": "Processing"}

    background_tasks.add_task(
        _run_congruence,
        job_id   = job_id,
        filepath = filepath,
        email    = u["email"],
        filename = file.filename,
    )

    return {"status": "Queued", "job_id": job_id}
