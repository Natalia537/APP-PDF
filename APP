import re
import io
import zipfile
from pathlib import Path
from typing import List, Tuple, Optional

import streamlit as st
from pypdf import PdfReader, PdfWriter
import pdfplumber


# ========= Utilidades =========
def sanitize_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r"[^\w\s\-_.()]", "", name, flags=re.UNICODE).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:max_len] or "Plan"


def detect_starts_by_patterns(
    page_texts: List[str],
    patterns: List[str],
) -> List[Tuple[int, str]]:
    """Devuelve lista de (page_index, label) donde se detecta inicio."""
    regexes = [re.compile(pat, re.IGNORECASE) for pat in patterns]
    starts = []
    for i, txt in enumerate(page_texts):
        label = None
        for rx in regexes:
            m = rx.search(txt)
            if m:
                if m.lastindex:  # si hay grupo capturado, úsalo para nombrar
                    cand = sanitize_filename(m.group(1))
                    label = cand or "Plan"
                else:
                    label = "Plan"
                break
        if label:
            starts.append((i, label))
    return starts


def get_page_texts(
    pdf_bytes: bytes,
    header_lines: int = 8,
) -> List[str]:
    """Extrae texto por página (sólo primeras N líneas para robustez)."""
    texts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            lines = txt.splitlines()
            texts.append("\n".join(lines[: header_lines if header_lines > 0 else len(lines)]))
    return texts


def export_ranges_to_zip(
    pdf_bytes: bytes,
    ranges: List[Tuple[int, int, str]],
    prefix: str = "",
) -> bytes:
    """Crea un ZIP con cada rango (start, end, label)."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (start, end, label) in enumerate(ranges, 1):
            writer = PdfWriter()
            for p in range(start, end):
                writer.add_page(reader.pages[p])
            out_bytes = io.BytesIO()
            writer.write(out_bytes)
            out_bytes.seek(0)
            fname = f"{prefix}{idx:03d}_{sanitize_filename(label)}.pdf"
            zf.writestr(fname, out_bytes.read())
    mem_zip.seek(0)
    return mem_zip.getvalue()


def build_ranges_from_starts(
    total_pages: int,
    starts: List[Tuple[int, str]],
) -> List[Tuple[int, int, str]]:
    """Convierte inicios (página, etiqueta) a rangos (ini, fin, etiqueta)."""
    ranges = []
    for k, (p_ini, label) in enumerate(starts):
        p_fin = starts[k + 1][0] if k + 1 < len(starts) else total_pages
        ranges.append((p_ini, p_fin, label or f"Plan_{k+1}"))
    return ranges


def build_ranges_every_n(
    total_pages: int, n: int
) -> List[Tuple[int, int, str]]:
    ranges = []
    i = 0
    idx = 0
    while i < total_pages:
        idx += 1
        end = min(i + n, total_pages)
        ranges.append((i, end, f"Plan_{idx:03d}"))
        i = end
    return ranges


# ========= UI =========
st.set_page_config(page_title="PDF Splitter por Criterios (Profes/Planes)", page_icon="📄")

st.title("📄 Dividir PDF por criterios (Profes/Planes)")
st.caption(
    "Sube un PDF grande y sepáralo por patrones de texto (ej. 'Profesor: Nombre') "
    "o cada N páginas. Descarga un ZIP con los PDFs resultantes."
)

with st.sidebar:
    st.header("⚙️ Configuración")
    mode = st.radio(
        "Modo de división",
        options=["Por patrones de texto", "Cada N páginas"],
    )

    default_patterns = (
        r"^\s*Profesor(?:a)?\s*:\s*(.+)$\n"
        r"^\s*Docente\s*:\s*(.+)$\n"
        r"^\s*Plan\s+de\s+clase"
    )
    patterns_text = st.text_area(
        "Patrones (uno por línea, usa paréntesis de captura para el nombre)",
        value=default_patterns if mode == "Por patrones de texto" else "",
        height=110,
        help="Ejemplos:\n"
             r"^\s*Profesor\s*:\s*(.+)$"
             "\n"
             r"^\s*Docente\s*:\s*(.+)$"
             "\n"
             r"^\s*Plan\s+de\s+clase"
    )

    header_lines = st.number_input(
        "Líneas a leer por página (para detectar encabezado)",
        min_value=0, max_value=50, value=8, step=1,
        help="Leer solo las primeras líneas suele ser suficiente y más robusto."
    )

    n_pages = st.number_input(
        "N páginas por bloque (si eliges 'Cada N páginas')",
        min_value=1, max_value=20, value=2, step=1
    )

    prefix = st.text_input(
        "Prefijo para nombres de archivo (opcional)",
        value="",
        help="Ej: '2025_Profesores_'. Deja vacío si no lo necesitas."
    )

file = st.file_uploader("Sube tu PDF", type=["pdf"])

if file is not None:
    pdf_bytes = file.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    st.info(f"PDF cargado: **{file.name}** — {total_pages} páginas")

    if st.button("🔍 Previsualizar detecciones" if mode == "Por patrones de texto" else "🔍 Previsualizar bloques"):
        if mode == "Por patrones de texto":
            if not patterns_text.strip():
                st.error("Agrega al menos un patrón.")
            else:
                patterns = [p for p in patterns_text.splitlines() if p.strip()]
                texts = get_page_texts(pdf_bytes, header_lines=header_lines)
                starts = detect_starts_by_patterns(texts, patterns)
                if not starts:
                    st.warning("No se detectaron inicios. Ajusta patrones y prueba otra vez.")
                else:
                    ranges = build_ranges_from_starts(total_pages, starts)
                    st.success(f"Detectados {len(ranges)} planes/secciones.")
                    st.dataframe(
                        {
                            "Inicio (página 1-based)": [a+1 for a, _ in starts],
                            "Etiqueta": [b for _, b in starts],
                            "Fin (página 1-based)": [r[1] for r in ranges],
                        }
                    )
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)
            st.success(f"Se crearían {len(ranges)} archivos (bloques de {n_pages} pág.).")
            st.dataframe(
                {
                    "Inicio (página 1-based)": [r[0] + 1 for r in ranges],
                    "Fin (página 1-based)": [r[1] for r in ranges],
                    "Etiqueta": [r[2] for r in ranges],
                }
            )

    st.divider()

    if st.button("✂️ Dividir y descargar ZIP"):
        if mode == "Por patrones de texto":
            patterns = [p for p in patterns_text.splitlines() if p.strip()]
            texts = get_page_texts(pdf_bytes, header_lines=header_lines)
            starts = detect_starts_by_patterns(texts, patterns)
            if not starts:
                st.error("No se detectaron inicios. Ajusta patrones o usa el modo 'Cada N páginas'.")
            else:
                ranges = build_ranges_from_starts(total_pages, starts)
                zip_bytes = export_ranges_to_zip(pdf_bytes, ranges, prefix=prefix)
                st.success(f"Listo: {len(ranges)} archivos generados.")
                st.download_button(
                    "⬇️ Descargar ZIP",
                    data=zip_bytes,
                    file_name=f"{Path(file.name).stem}_split.zip",
                    mime="application/zip",
                )
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)
            zip_bytes = export_ranges_to_zip(pdf_bytes, ranges, prefix=prefix)
            st.success(f"Listo: {len(ranges)} archivos generados (bloques de {n_pages} páginas).")
            st.download_button(
                "⬇️ Descargar ZIP",
                data=zip_bytes,
                file_name=f"{Path(file.name).stem}_cada_{n_pages}pag.zip",
                mime="application/zip",
            )

st.markdown("---")
with st.expander("❓ Consejos / Problemas comunes"):
    st.markdown(
        """
- Si **no detecta** inicios por patrones, abre el PDF y copia 2–3 líneas del encabezado real. Ajusta tus regex; por ejemplo:
  - `^\\s*Profesor\\s*:\\s*(.+)$`
  - `^\\s*Docente\\s*:\\s*(.+)$`
  - `^\\s*Plan\\s+de\\s+.*`
- Si el PDF es **escaneado** (imágenes), este método no verá texto. Para usar patrones, primero conviértelo con OCR en tu PC (ver README).
- Puedes bajar el número de **líneas a leer** si hay ruido, o subirlo si el encabezado no entra.
"""
    )
