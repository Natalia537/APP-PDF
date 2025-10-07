import io
import re
import zipfile
import unicodedata
from pathlib import Path
from typing import List, Tuple, Optional

import streamlit as st
from pypdf import PdfReader, PdfWriter
import pdfplumber

# ============ Utilidades de texto / limpieza ============
def sanitize_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r"[^\w\s\-_.()]", "", name, flags=re.UNICODE).strip()
    name = re.sub(r"\s+", " ", name)
    name = name[:max_len] or "Plan"
    return name

def clean_title_prefixes(name: str) -> str:
    """Quita títulos comunes al inicio (DR., DRA., LIC., ING., MSC., MAG., MTR., PHD, PROF., etc.)."""
    if not name:
        return name
    pattern = r"^\s*(?:dr\.?|dra\.?|lic\.?|ing\.?|msc\.?|m\.?sc\.?|mag\.?|maestr[eo]|master|mtr\.?|ph\.?d\.?|prof\.?)\s+"
    name = re.sub(pattern, "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()
    return name

def normalize_text(s: str) -> str:
    """Normaliza: sin tildes, minúsculas y espacios colapsados (para comparar)."""
    s = s or ""
    s = s.replace("\u00A0", " ")
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

# corta un texto ANTES de la palabra GÉNERO/GENERO (insensible a mayúsculas/acentos)
def cut_before_genero(s: str) -> str:
    if not s:
        return s
    parts = re.split(r"\bG[EÉ]NERO\b", s, flags=re.IGNORECASE, maxsplit=1)
    return parts[0].strip() if parts else s.strip()

# ============ Extracción de textos ============
def get_page_texts_for_start(pdf_bytes: bytes, max_lines: int) -> List[str]:
    """Texto por página (solo primeras max_lines) para detectar INICIO."""
    texts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            lines = txt.splitlines()
            take = lines if max_lines <= 0 else lines[:max_lines]
            texts.append("\n".join(take))
    return texts

def get_text_block_for_name(pdf_bytes: bytes, start_page: int, end_page: int,
                            scan_pages: int, max_lines_per_page: int) -> str:
    """Concatena las primeras max_lines de las primeras scan_pages páginas del rango."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        stop = min(end_page, start_page + scan_pages)
        buf = []
        for p in range(start_page, stop):
            txt = pdf.pages[p].extract_text() or ""
            lines = txt.splitlines()
            take = lines if max_lines_per_page <= 0 else lines[:max_lines_per_page]
            buf.extend(take)
    return "\n".join(buf)

# ============ Detección de inicios ============
def detect_starts_by_patterns(page_texts: List[str], patterns: List[str]) -> List[Tuple[int, Optional[str]]]:
    """
    Devuelve lista de (page_index, etiqueta_capturada_o_None).
    Los patrones pueden tener (.+) para capturar un valor.
    Se evalúa línea por línea.
    """
    regexes = [re.compile(pat, re.IGNORECASE) for pat in patterns]
    starts = []
    for i, txt in enumerate(page_texts):
        found_label = None
        for rx in regexes:
            # buscar por líneas para que ^ y $ sean útiles
            matched = False
            for line in txt.split("\n"):
                m = rx.search(line)
                if m:
                    matched = True
                    if m.lastindex:
                        cand = sanitize_filename(m.group(1))
                        found_label = cand or None
                    else:
                        found_label = None
                    break
            if matched:
                starts.append((i, found_label))
                break
    return starts

def build_ranges_from_starts(total_pages: int, starts: List[Tuple[int, Optional[str]]]) -> List[Tuple[int, int, Optional[str]]]:
    ranges = []
    for k, (p_ini, label_opt) in enumerate(starts):
        p_fin = starts[k + 1][0] if k + 1 < len(starts) else total_pages
        ranges.append((p_ini, p_fin, label_opt))
    return ranges

def build_ranges_every_n(total_pages: int, n: int) -> List[Tuple[int, int, Optional[str]]]:
    ranges = []
    i = 0
    while i < total_pages:
        end = min(i + n, total_pages)
        ranges.append((i, end, None))
        i = end
    return ranges

# ============ Búsqueda específica del nombre ============
def find_prof_name_in_section(pdf_bytes: bytes, start_page: int, end_page: int,
                              scan_pages: int = 2, lines_per_page: int = 60) -> Optional[str]:
    """
    Busca EXACTAMENTE la línea: NOMBRE DEL PROFESOR(A) : <valor>
    Acepta separadores :  -  –  —  =
    Corta el valor antes de la palabra GÉNERO/GENERO si aparece en la misma línea.
    Devuelve <valor> limpio (sin títulos), o None si no lo encuentra.
    """
    label_rx = re.compile(
        r"^\s*NOMBRE\s+DEL\s+PROFESOR\(A\)\s*[:=\-\u2014\u2013]\s*(.+)$",
        re.IGNORECASE
    )

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        stop = min(end_page, start_page + scan_pages)
        for p in range(start_page, stop):
            txt = pdf.pages[p].extract_text() or ""
            for raw in (txt.splitlines()[:lines_per_page]):
                m = label_rx.search(raw)
                if m and m.group(1).strip():
                    name = m.group(1).strip()
                    # cortar antes de "GÉNERO" si viene en la misma línea
                    name = cut_before_genero(name)
                    name = clean_title_prefixes(name)
                    name = sanitize_filename(name)
                    return name if name else None
    return None

# ============ Excel (openpyxl) ============
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

def build_excel_bytes(detalles_rows: List[dict]) -> bytes:
    """Crea un Excel con hojas 'detalles' y 'resumen' usando openpyxl (sin pandas/numpy)."""
    wb = Workbook()
    ws_det = wb.active
    ws_det.title = "detalles"

    headers = ["orden", "archivo", "nombre_detectado", "pagina_inicio_1based", "pagina_fin_1based", "paginas_en_pdf"]
    ws_det.append(headers)
    for row in detalles_rows:
        ws_det.append([row.get(h) for h in headers])

    for col_idx, h in enumerate(headers, 1):
        ws_det.column_dimensions[get_column_letter(col_idx)].width = max(16, len(h) + 2)

    ws_res = wb.create_sheet("resumen")
    ws_res.append(["nombre_detectado", "cantidad_pdfs"])
    counts = {}
    for r in detalles_rows:
        key = r.get("nombre_detectado") or ""
        counts[key] = counts.get(key, 0) + 1
    for name, qty in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        ws_res.append([name, qty])
    ws_res.column_dimensions["A"].width = 40
    ws_res.column_dimensions["B"].width = 16

    bio = io.BytesIO()
    wb.save(bio)
    bio.seek(0)
    return bio.getvalue()

# ============ Export (ZIP + Excel) ============
def export_zip_and_excel(
    pdf_bytes: bytes,
    ranges: List[Tuple[int, int, Optional[str]]],
    prefix: str,
    scan_pages: int,
    lines_per_page: int,
) -> Tuple[bytes, bytes, List[dict]]:
    """
    Devuelve (zip_bytes, excel_bytes, detalles_rows).
    El ZIP trae todos los PDFs nombrados; el Excel trae detalles y resumen.
    - SIN numeración al inicio del nombre del archivo.
    - Si hay colisión de nombres, agrega sufijo _(2), _(3)...
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    detalles_rows = []
    mem_zip = io.BytesIO()
    used_names: dict[str, int] = {}

    with zipfile.ZipFile(mem_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (start, end, label_opt) in enumerate(ranges, 1):
            # 1) Intentar con NOMBRE DEL PROFESOR(A):
            detected = find_prof_name_in_section(
                pdf_bytes, start, end, scan_pages=scan_pages, lines_per_page=lines_per_page
            )
            # 2) Si no, usar etiqueta del inicio si existe
            if not detected and label_opt:
                detected = sanitize_filename(label_opt)
            # 3) Fallback final
            if not detected:
                detected = f"Plan_{idx:03d}"

            base = sanitize_filename(f"{prefix}{detected}") if prefix else sanitize_filename(detected)

            # Unicidad: si ya existe, agrega sufijo _(2), _(3)...
            final_name = base
            if final_name in used_names:
                used_names[final_name] += 1
                final_name = f"{base}_({used_names[base]})"
            else:
                used_names[final_name] = 1

            # Escribir el sub-PDF
            writer = PdfWriter()
            for p in range(start, end):
                writer.add_page(reader.pages[p])
            out_bytes = io.BytesIO()
            writer.write(out_bytes)
            out_bytes.seek(0)

            fname = f"{final_name}.pdf"  # <-- sin numeración
            zf.writestr(fname, out_bytes.read())

            # Registro para Excel
            detalles_rows.append({
                "orden": idx,
                "archivo": fname,
                "nombre_detectado": detected,
                "pagina_inicio_1based": start + 1,
                "pagina_fin_1based": end,
                "paginas_en_pdf": end - start,
            })

    mem_zip.seek(0)
    excel_bytes = build_excel_bytes(detalles_rows)
    return mem_zip.getvalue(), excel_bytes, detalles_rows

# ============ UI ============
st.set_page_config(page_title="PDF Splitter — Profes y Excel", page_icon="📄")
st.title("📄 Dividir PDF, nombrar por 'NOMBRE DEL PROFESOR(A):' y generar Excel")
st.caption("Corta nombres antes de 'GÉNERO'. Sin numeración en el nombre del archivo.")

with st.sidebar:
    st.header("⚙️ Configuración")
    mode = st.radio("Modo de división", ["Por patrones de inicio", "Cada N páginas"])

    # Patrones para detectar inicio de cada “plan”
    default_patterns = "\n".join([
        r"^\s*plan\s+de\s+clase",              # ejemplo
        r"^\s*profesor(?:a)?\s*:\s*(.+)$",     # si justo aquí aparece un nombre
        r"^\s*docente\s*:\s*(.+)$"
    ])
    patterns_text = st.text_area(
        "Patrones de INICIO (uno por línea; opcionalmente con (.+) para capturar).",
        value=default_patterns if mode == "Por patrones de inicio" else "",
        height=110
    )
    header_lines = st.number_input(
        "Líneas a leer por página (INICIO)",
        min_value=0, max_value=120, value=10, step=1
    )

    n_pages = st.number_input(
        "N páginas por bloque (si eliges 'Cada N páginas')",
        min_value=1, max_value=20, value=2, step=1
    )

    st.markdown("---")
    st.subheader("📛 Búsqueda del nombre")
    st.write("Se busca **exactamente** la línea `NOMBRE DEL PROFESOR(A): ...` en las primeras páginas de cada sección y se corta antes de 'GÉNERO'.")
    scan_pages = st.number_input("Páginas a escanear por sección", 1, 10, 2, 1)
    lines_for_name = st.number_input("Líneas a leer por página (para nombre)", 5, 120, 60, 5)

    prefix = st.text_input("Prefijo para archivos (opcional)", value="")

file = st.file_uploader("Sube tu PDF", type=["pdf"])

if file is not None:
    pdf_bytes = file.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    st.info(f"PDF cargado: **{file.name}** — {total_pages} páginas")

    if st.button("🔍 Previsualizar cortes"):
        if mode == "Por patrones de inicio":
            if not patterns_text.strip():
                st.error("Agrega al menos un patrón o usa 'Cada N páginas'.")
            else:
                patterns = [p for p in (patterns_text.splitlines()) if p.strip()]
                page_texts = get_page_texts_for_start(pdf_bytes, header_lines)
                starts = detect_starts_by_patterns(page_texts, patterns)
                if not starts:
                    st.warning("No se detectaron INICIOS. Ajusta patrones o usa 'Cada N páginas'.")
                else:
                    ranges = build_ranges_from_starts(total_pages, starts)
                    st.success(f"Detectados {len(ranges)} cortes/secciones.")
                    st.dataframe({
                        "Inicio (pág. 1-based)": [s[0] + 1 for s in starts],
                        "Fin (pág. 1-based)": [r[1] for r in ranges],
                        "Etiqueta capturada": [s[1] or "" for s in starts],
                    })
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)
            st.success(f"Se crearían {len(ranges)} archivos de {n_pages} páginas (último puede variar).")
            st.dataframe({
                "Inicio (pág. 1-based)": [r[0] + 1 for r in ranges],
                "Fin (pág. 1-based)": [r[1] for r in ranges],
                "Etiqueta capturada": [r[2] or "" for r in ranges],
            })

    st.divider()

    if st.button("✂️ Dividir, nombrar y descargar"):
        # Construir los rangos según el modo
        if mode == "Por patrones de inicio":
            patterns = [p for p in (patterns_text.splitlines()) if p.strip()]
            if not patterns:
                st.error("Agrega patrones o usa 'Cada N páginas'.")
                st.stop()
            page_texts = get_page_texts_for_start(pdf_bytes, header_lines)
            starts = detect_starts_by_patterns(page_texts, patterns)
            if not starts:
                st.error("No se detectaron INICIOS. Revisa los patrones.")
                st.stop()
            ranges = build_ranges_from_starts(total_pages, starts)
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)

        # Exportar ZIP y Excel (sin numeración en archivo, y cortando antes de GÉNERO)
        zip_bytes, excel_bytes, detalles_rows = export_zip_and_excel(
            pdf_bytes, ranges, prefix=prefix,
            scan_pages=scan_pages, lines_per_page=lines_for_name
        )

        st.success(f"¡Listo! Generados {len(detalles_rows)} PDFs.")
        st.download_button(
            "⬇️ Descargar ZIP de PDFs",
            data=zip_bytes,
            file_name=f"{Path(file.name).stem}_split.zip",
            mime="application/zip"
        )
        st.download_button(
            "⬇️ Descargar Excel (detalles y resumen)",
            data=excel_bytes,
            file_name=f"{Path(file.name).stem}_reporte.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

st.markdown("---")
with st.expander("❓ Tips"):
    st.markdown(
        """
- Si el nombre aparece más abajo, sube **“Páginas a escanear”** (3–4) o **“Líneas para nombre”** (80–100).
- Acepta `GÉNERO` o `GENERO` con o sin acento, mayús/minús.
- Si hay nombres repetidos, el ZIP agrega `_(2)`, `_(3)` para no sobrescribir.
"""
    )
