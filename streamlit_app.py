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

import io
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import streamlit as st
from PIL import Image

from bv_extractor.claude_extractor import (
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
    st.caption("Biyolojik varyasyon tablosu çıkarıcı")
    if not expected:
        st.error(
            "APP_PASSWORD tanımlı değil. Bulutta 'Secrets' paneline, yerelde "
            "ortam değişkeni veya .streamlit/secrets.toml içine ekleyin."
        )
        st.stop()
    pw = st.text_input("Şifre", type="password")
    if st.button("Giriş"):
        if pw and pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Yanlış şifre.")
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


def _page_image(pdf_path: str, page_index: int, rotation: int) -> Image.Image:
    png = render_region_png(pdf_path, page_index, rotation=rotation)
    return Image.open(io.BytesIO(png)).convert("RGB")


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

    st.title("BV Extractor")
    st.caption("PDF makaleden biyolojik varyasyon tablosunu çıkar")

    uploaded = st.file_uploader("PDF yükle", type=["pdf"])
    if uploaded is None:
        st.info("Başlamak için bir PDF yükleyin.")
        return

    pdf_path = _save_upload(uploaded)
    profile = st.session_state["profile"]

    with st.expander("Ön analiz", expanded=False):
        st.text(format_profile(profile))

    left, right = st.columns([3, 2])

    # ---- left: page view + drawing ------------------------------------
    with left:
        c1, c2 = st.columns([3, 1])
        page_num = c1.number_input(
            "Sayfa", min_value=1, max_value=profile.page_count,
            value=st.session_state["page_index"] + 1,
        )
        st.session_state["page_index"] = int(page_num) - 1
        if c2.button("Döndür ⟳"):
            st.session_state["rotation"] = (st.session_state["rotation"] + 90) % 360

        page_index = st.session_state["page_index"]
        rotation = st.session_state["rotation"]
        img = _page_image(pdf_path, page_index, rotation)
        disp_w = DISPLAY_W
        disp_h = int(img.height * (DISPLAY_W / img.width))
        img_disp = img.resize((disp_w, disp_h))

        st.caption(
            "Tabloyu çevreleyen kutu(lar) çizin (birden çok tablo için birden "
            "çok kutu). Kutu çizmezseniz tüm sayfa gönderilir."
        )
        canvas_result = None
        if st_canvas is not None:
            canvas_result = st_canvas(
                fill_color="rgba(255, 0, 0, 0.10)",
                stroke_color="red",
                stroke_width=2,
                background_image=img_disp,
                drawing_mode="rect",
                width=disp_w,
                height=disp_h,
                key=f"canvas_{page_index}_{rotation}",
            )
        else:
            st.image(img_disp)
            st.warning(
                "streamlit-drawable-canvas kurulu değil — kutu çizimi devre "
                "dışı; tüm sayfa gönderilecek. (requirements.txt'e ekleyin.)"
            )

    # ---- right: controls + run ----------------------------------------
    with right:
        use_claude = st.checkbox("Claude (LLM) kullan", value=False)
        st.caption(
            "Kapalı: önce hızlı/yerel parser; değer bulamazsa Claude'a düşer."
        )
        run = st.button("Çıkar ▶", type="primary")

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
                    st.success(f"Parser {len(result.analytes)} analit buldu.")
                else:
                    st.info("Parser değer bulamadı — Claude'a gönderiliyor…")
                    with st.spinner("Claude çıkarım yapıyor… (15–90 sn)"):
                        result = extract_with_claude_regions(
                            pdf_path, whole_or_regions()
                        )
            else:
                with st.spinner("Claude çıkarım yapıyor… (15–90 sn)"):
                    result = extract_with_claude_regions(
                        pdf_path, whole_or_regions()
                    )
            st.session_state["result"] = result
        except Exception as exc:  # noqa: BLE001
            st.session_state["result"] = None
            st.error(f"Çıkarım hatası: {exc}")

    # ---- results -------------------------------------------------------
    result = st.session_state.get("result")
    if result:
        st.subheader("Sonuçlar")
        rows = _result_rows(result)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.warning("Hiç analit çıkarılamadı.")

        if result.report.used_llm_fallback and (
            result.report.input_tokens or result.report.output_tokens
        ):
            cost = estimate_cost_usd(
                result.report.input_tokens, result.report.output_tokens
            )
            st.caption(
                f"Token: {result.report.input_tokens} girdi / "
                f"{result.report.output_tokens} çıktı  (~${cost:.3f})"
            )
        for note in result.report.manual_review:
            st.caption(f"📝 {note}")

        if rows:
            xb, jb, tb = _result_files(result, st.session_state["stem"])
            d1, d2, d3 = st.columns(3)
            stem = st.session_state["stem"]
            d1.download_button("Excel indir", xb, f"{stem}.xlsx")
            d2.download_button("JSON indir", jb, f"{stem}.json")
            d3.download_button("Rapor indir", tb, f"{stem}.txt")


if __name__ == "__main__":
    main()
