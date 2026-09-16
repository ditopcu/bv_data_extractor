"""
Streamlit web UI for bv_extractor.

A browser front-end over the existing extraction core (preanalyzer,
claude_extractor, pipeline, output writers). It mirrors the desktop wizard:

    login (shared password)
        -> upload a PDF
        -> pre-analysis locates the BV table page
        -> draw box(es) around the table(s) on a canvas (optional; rotate/zoom)
        -> Extract: deterministic parser, or Claude vision on the selected
           region(s) / whole page
        -> review the analytes
        -> download Excel / JSON / text report

Secrets (NEVER committed) come from st.secrets:
    APP_PASSWORD       — the shared login password
    ANTHROPIC_API_KEY  — server-side Claude key (cost borne by the host)

The Tk desktop modules (app.py, interactive_picker.py) are NOT imported here,
so tkinter is not required on the server.
"""

from __future__ import annotations

import base64
import io
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import streamlit as st
from PIL import Image

from bv_extractor import __version__
from bv_extractor.claude_extractor import (
    DEFAULT_MODEL,
    PRICE_INPUT_PER_MTOK,
    PRICE_OUTPUT_PER_MTOK,
    estimate_cost_usd,
    extract_with_claude_regions,
    render_region_png,
)
from bv_extractor.outputs.excel_writer import write_excel
from bv_extractor.outputs.json_writer import write_json
from bv_extractor.outputs.report_writer import write_report
from bv_extractor.pipeline import extract
from bv_extractor.preanalyzer import analyze, format_profile

try:
    from streamlit_drawable_canvas import st_canvas
except Exception:  # noqa: BLE001
    st_canvas = None

# Width (px) the page image is displayed at on the drawing canvas.
DISPLAY_W = 720


# ---------------------------------------------------------------------------
# Auth + secrets
# ---------------------------------------------------------------------------

def _secret(name: str):
    """Read a secret from st.secrets (cloud) falling back to env (local dev)."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:  # noqa: BLE001 - no secrets.toml locally
        pass
    return os.environ.get(name)


def _inject_api_key() -> None:
    """Expose the Claude key to claude_extractor via the environment."""
    key = _secret("ANTHROPIC_API_KEY")
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key


def require_password() -> None:
    """Block the app behind a single shared password (st.stop if not authed)."""
    if st.session_state.get("auth_ok"):
        return
    expected = _secret("APP_PASSWORD")
    st.title("BV Extractor")
    st.caption("Biological variation table extractor")
    if not expected:
        st.error(
            "APP_PASSWORD is not set. Add it to the cloud 'Secrets' panel, or "
            "to an environment variable / .streamlit/secrets.toml locally."
        )
        st.stop()
    pw = st.text_input("Password", type="password")
    if st.button("Log in"):
        if pw and pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_upload(uploaded) -> str:
    """Persist the uploaded PDF to a temp file and return its path."""
    sig = (uploaded.name, uploaded.size)
    if st.session_state.get("pdf_sig") != sig:
        tmp = Path(tempfile.gettempdir()) / f"bvx_{abs(hash(sig))}.pdf"
        tmp.write_bytes(uploaded.getvalue())
        profile = analyze(tmp)
        st.session_state.update(
            pdf_sig=sig,
            pdf_path=str(tmp),
            profile=profile,
            stem=Path(uploaded.name).stem,
            page_index=(profile.primary_table_page - 1)
            if profile.primary_table_page else 0,
            rotation=0,
            result=None,
        )
    return st.session_state["pdf_path"]


@st.cache_data(show_spinner=False)
def _page_image(pdf_path: str, page_index: int, rotation: int) -> Image.Image:
    """Render one page; cached so widget reruns don't re-rasterise the PDF."""
    png = render_region_png(pdf_path, page_index, rotation=rotation)
    return Image.open(io.BytesIO(png)).convert("RGB")


@st.cache_data(show_spinner=False)
def _display_image(pdf_path: str, page_index: int, rotation: int) -> Image.Image:
    """The page scaled to the canvas width (cached, same reason as above)."""
    img = _page_image(pdf_path, page_index, rotation)
    disp_h = int(img.height * (DISPLAY_W / img.width))
    return img.resize((DISPLAY_W, disp_h))


def _canvas_drawing(img_disp: Image.Image) -> dict:
    """Fabric.js JSON that paints the page image as a locked canvas object.

    streamlit-drawable-canvas serves `background_image` through the server's
    /media endpoint, and that request does not resolve inside the component
    iframe on Streamlit Community Cloud, leaving a blank (transparent) canvas.
    Embedding the page as a data-URL image object needs no extra request, so
    it renders the same locally and in the cloud. The object is unselectable
    and non-evented, so the rectangle tool draws over it normally.
    """
    buf = io.BytesIO()
    img_disp.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {
        "version": "4.4.0",
        "objects": [{
            "type": "image",
            "version": "4.4.0",
            "originX": "left",
            "originY": "top",
            "left": 0,
            "top": 0,
            "width": img_disp.width,
            "height": img_disp.height,
            "src": f"data:image/jpeg;base64,{b64}",
            "selectable": False,
            "evented": False,
            "hasControls": False,
        }],
    }


def _regions_from_canvas(canvas_result, page_index, rotation, w, h) -> list:
    """Convert canvas rectangles to region specs (fractions of the view)."""
    regions = []
    data = getattr(canvas_result, "json_data", None)
    if not data:
        return regions
    for obj in data.get("objects", []):
        if obj.get("type") != "rect":
            continue
        left, top = obj["left"], obj["top"]
        rw = obj["width"] * obj.get("scaleX", 1)
        rh = obj["height"] * obj.get("scaleY", 1)
        fx0, fy0 = max(0.0, left / w), max(0.0, top / h)
        fx1, fy1 = min(1.0, (left + rw) / w), min(1.0, (top + rh) / h)
        if fx1 > fx0 and fy1 > fy0:
            regions.append(SimpleNamespace(
                page_index=page_index, rotation=rotation,
                bbox_frac=(fx0, fy0, fx1, fy1),
            ))
    return regions


def _result_rows(result) -> list:
    rows = []
    for a in result.analytes:
        def ci(lo, hi):
            if lo.value is None and hi.value is None:
                return ""
            return f"{_fmt(lo.value)}–{_fmt(hi.value)}"
        rows.append({
            "Analyte": a.name or a.abbreviation or "—",
            "Abbr": a.abbreviation or "",
            "Unit": a.unit or "",
            "CVI": _fmt(a.cvi.value),
            "CVI 95% CI": ci(a.cvi_ci_lower, a.cvi_ci_upper),
            "CVG": _fmt(a.cvg.value),
            "CVG 95% CI": ci(a.cvg_ci_lower, a.cvg_ci_upper),
            "CVA": _fmt(a.analytical_cv.value),
            "Mean": _fmt(a.measurand_mean.value),
            "SD": _fmt(a.measurand_sd.value),
            "Method": a.method or "",
        })
    return rows


def _fmt(v) -> str:
    return "" if v is None else f"{v:g}"


# ---------------------------------------------------------------------------
# Sidebar: Claude status + cost
# ---------------------------------------------------------------------------

def _ping_claude() -> tuple[bool, str]:
    """Check the API key and model access without spending tokens.

    `models.retrieve` is a metadata call: it fails on a missing/invalid key or
    an unavailable model and costs nothing, so it is safe to press repeatedly.
    """
    try:
        import anthropic

        client = anthropic.Anthropic()
        m = client.models.retrieve(DEFAULT_MODEL)
        return True, f"Connected: {getattr(m, 'display_name', None) or m.id}"
    except Exception as exc:  # noqa: BLE001 - shown to the user verbatim
        return False, f"{type(exc).__name__}: {str(exc)[:200]}"


def _record_usage(result) -> None:
    """Accumulate token usage of Claude runs for the sidebar cost panel."""
    rep = result.report
    if not rep.used_llm_fallback or not (rep.input_tokens or rep.output_tokens):
        return
    cost = estimate_cost_usd(rep.input_tokens, rep.output_tokens)
    st.session_state["last_usage"] = {
        "in": rep.input_tokens, "out": rep.output_tokens, "cost": cost,
    }
    tot = st.session_state.setdefault(
        "usage_total", {"runs": 0, "in": 0, "out": 0, "cost": 0.0}
    )
    tot["runs"] += 1
    tot["in"] += rep.input_tokens
    tot["out"] += rep.output_tokens
    tot["cost"] += cost


def render_sidebar() -> None:
    """Always-visible status panel: Claude availability, model, prices, cost."""
    with st.sidebar:
        st.header("Status")

        key_set = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if key_set:
            st.success("Claude API key: set")
        else:
            st.error("Claude API key: missing")
            st.caption(
                "Only the local parser will run. Add ANTHROPIC_API_KEY to "
                "the app secrets to enable Claude."
            )

        if st.button("Test Claude connection", disabled=not key_set,
                     use_container_width=True):
            with st.spinner("Checking…"):
                st.session_state["claude_ping"] = _ping_claude()
        ping = st.session_state.get("claude_ping")
        if ping is not None:
            ok, msg = ping
            (st.success if ok else st.error)(msg)

        st.caption(f"Model: `{DEFAULT_MODEL}`")
        # No "$" here: Streamlit's Markdown treats `$…$` as LaTeX.
        st.caption(
            f"Price: {PRICE_INPUT_PER_MTOK:g} USD in / {PRICE_OUTPUT_PER_MTOK:g} USD "
            "out, per million tokens"
        )

        st.divider()
        st.header("Cost (USD)")
        last = st.session_state.get("last_usage")
        tot = st.session_state.get("usage_total")
        c1, c2 = st.columns(2)
        c1.metric("Last run", f"${last['cost']:.3f}" if last else "—")
        c2.metric("This session", f"${tot['cost']:.3f}" if tot else "—")
        if last:
            st.caption(f"Last run: {last['in']} in / {last['out']} out tokens")
        if tot:
            st.caption(
                f"Session: {tot['runs']} Claude run(s), "
                f"{tot['in']} in / {tot['out']} out tokens"
            )
        if not last:
            st.caption("No Claude run yet. The local parser is free.")

        st.divider()
        st.caption(f"bv_extractor v{__version__} · research prototype")


def _result_files(result, stem: str):
    d = Path(tempfile.mkdtemp())
    xb = Path(write_excel(result, d / f"{stem}.xlsx")).read_bytes()
    jb = Path(write_json(result, d / f"{stem}.json")).read_bytes()
    tb = Path(write_report(result, d / f"{stem}.txt")).read_bytes()
    return xb, jb, tb


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="BV Extractor", layout="wide")
    _inject_api_key()
    require_password()
    render_sidebar()

    st.title("BV Extractor")
    st.caption("Extract biological variation tables from a PDF article")

    uploaded = st.file_uploader("Upload a PDF", type=["pdf"])
    if uploaded is None:
        st.info("Upload a PDF to begin.")
        return

    pdf_path = _save_upload(uploaded)
    profile = st.session_state["profile"]

    with st.expander("Pre-analysis", expanded=False):
        st.text(format_profile(profile))

    left, right = st.columns([3, 2])

    # ---- left: page view + drawing ------------------------------------
    with left:
        c1, c2 = st.columns([3, 1])
        page_num = c1.number_input(
            "Page", min_value=1, max_value=profile.page_count,
            value=st.session_state["page_index"] + 1,
        )
        st.session_state["page_index"] = int(page_num) - 1
        if c2.button("Rotate ⟳"):
            st.session_state["rotation"] = (st.session_state["rotation"] + 90) % 360

        page_index = st.session_state["page_index"]
        rotation = st.session_state["rotation"]
        img_disp = _display_image(pdf_path, page_index, rotation)
        disp_w, disp_h = img_disp.width, img_disp.height

        st.caption(
            "Draw a box around the table(s) (one box per table). If you draw "
            "none, the whole page is sent."
        )
        canvas_result = None
        if st_canvas is not None:
            canvas_result = st_canvas(
                fill_color="rgba(255, 0, 0, 0.10)",
                stroke_color="red",
                stroke_width=2,
                background_color="#ffffff",
                initial_drawing=_canvas_drawing(img_disp),
                drawing_mode="rect",
                width=disp_w,
                height=disp_h,
                key=f"canvas_{page_index}_{rotation}",
            )
        else:
            st.image(img_disp)
            st.warning(
                "streamlit-drawable-canvas is not installed — box drawing is "
                "disabled; the whole page will be sent. (Add it to "
                "requirements.txt.)"
            )

    # ---- right: controls + run ----------------------------------------
    with right:
        use_claude = st.checkbox("Use Claude (LLM)", value=False)
        st.caption(
            "Off: try the fast local parser first; fall back to Claude if it "
            "finds nothing."
        )
        run = st.button("Extract ▶", type="primary")

    if run:
        regions = _regions_from_canvas(
            canvas_result, page_index, rotation, disp_w, disp_h
        ) if canvas_result is not None else []

        def whole_or_regions():
            return regions or [SimpleNamespace(
                page_index=page_index, rotation=rotation, bbox_frac=None,
            )]

        try:
            if not use_claude:
                result = extract(pdf_path)
                if result.report.fields_extracted:
                    st.success(f"Parser found {len(result.analytes)} analyte(s).")
                else:
                    st.info("Parser found no values — sending to Claude…")
                    with st.spinner("Claude is extracting… (15–90 s)"):
                        result = extract_with_claude_regions(
                            pdf_path, whole_or_regions()
                        )
            else:
                with st.spinner("Claude is extracting… (15–90 s)"):
                    result = extract_with_claude_regions(
                        pdf_path, whole_or_regions()
                    )
            st.session_state["result"] = result
            _record_usage(result)
            # Sidebar was drawn before this run; redraw so the cost updates now.
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.session_state["result"] = None
            st.error(f"Extraction error: {exc}")

    # ---- results -------------------------------------------------------
    result = st.session_state.get("result")
    if result:
        st.subheader("Results")
        rows = _result_rows(result)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.warning("No analytes were extracted.")

        if result.report.used_llm_fallback and (
            result.report.input_tokens or result.report.output_tokens
        ):
            cost = estimate_cost_usd(
                result.report.input_tokens, result.report.output_tokens
            )
            st.caption(
                f"Tokens: {result.report.input_tokens} in / "
                f"{result.report.output_tokens} out  (~${cost:.3f})"
            )
        for note in result.report.manual_review:
            st.caption(f"📝 {note}")

        if rows:
            xb, jb, tb = _result_files(result, st.session_state["stem"])
            d1, d2, d3 = st.columns(3)
            stem = st.session_state["stem"]
            d1.download_button("Download Excel", xb, f"{stem}.xlsx")
            d2.download_button("Download JSON", jb, f"{stem}.json")
            d3.download_button("Download report", tb, f"{stem}.txt")


if __name__ == "__main__":
    main()
