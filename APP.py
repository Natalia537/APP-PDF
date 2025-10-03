import re
import io
import zipfile
import unicodedata
from pathlib import Path
from typing import List, Tuple, Optional, Dict

import streamlit as st
import pandas as pd
from pypdf import PdfReader, PdfWriter
import pdfplumber


# ================== Utilidades de texto ==================
def normalize_text(s: str) -> str:
    """
    Normaliza a minúsculas y sin tildes/acentos para hacer match más tolerante.
    También colapsa espacios múltiples.
    """
    s = s or ""
    s = s.replace("\u00A0", " ")  # NBSP -> espacio normal
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")  # quita diacríticos
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def sanitize_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r"[^\w\s\-_.()]", "", name, flags=re.UNICODE).strip()
    name = re.sub(r"\s+", " ", name)
    return (name or "Plan")[:max_len]


def split_label_value(line: str) -> Optional[str]:
    """
    Dado una línea, intenta extraer lo que va después de un separador tipo :, -, —, –, =
    Devuelve el "valor" (a la derecha) limpio, o None.
    """
    # acepta :, -, — (emdash), – (endash), =
    parts = re.split(r"\s*[:=\-\u2014\u2013]\s*", line, maxsplit=1)
    if len(parts) == 2:
        value = parts[1].strip()
        value = re.sub(r"\s+", " ", value)
        return value if value else None
    return None


# ================== Extracción de texto ==================
def get_page_texts(pdf_bytes: bytes, max_lines: int) -> List[str]:
    """
    Extrae texto por página. Devuelve solo las primeras max_lines líneas de cada página
    (para hacer detección de INICIO más rápida/robusta).
    """
    texts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            lines = txt.splitlines()
            take = lines if max_lines <= 0 else lines[:max_lines]
            texts.append("\n".join(take))
    return texts


def get_section_text_block(pdf_bytes: bytes, start_page: int, end_page: int, scan_pages: int, max_lines: int) -> str:
    """
    Toma desde start_page hasta start_page+scan_pages (sin pasar end_page),
    une hasta max_lines por página para buscar el nombre.
    """
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        stop = min(end_page, start_page + scan_pages)
        buf = []
        for p in range(start_page, stop):
            txt = (pdf.pages[p].extract_text() or "")
            lines = txt.splitlines()
            take = lines if max_lines <= 0 else lines[:max_lines]
            buf.extend(take)
    return "\n".join(buf)


# ================== Detección de INICIOS ==================
def compile_start_patterns(raw_patterns: List[str]) -> List[re.Pattern]:
    """
    Compila patrones de INICIO, pero aplicamos normalización al texto cuando comparamos,
    así que aquí convertimos cada raw_pattern a una versión sin tildes/minúsculas.
    Además, permitimos que el usuario escriba patrones 'simples' (no regex) si quiere.
    """
    # Estrategia: convertimos cada patrón del usuario a una regex que busque su versión normalizada.
    # Soportamos que el patrón tenga (.+) para capturar nombre.
    compiled = []
    for pat in raw_patterns:
        pat = pat.strip()
        if not pat:
            continue
        # Mantén los metacaracteres del usuario. La comparación será sobre texto normalizado.
        try:
            rx = re.compile(pat)
            compiled.append(rx)
        except re.error:
            # Si el patrón no es regex válido, escápalo:
            rx = re.compile(re.escape(pat))
            compiled.append(rx)
    return compiled


def detect_starts(page_texts: List[str], start_rxs: List[re.Pattern]) -> List[Tuple[int, Optional[str]]]:
    """
    Devuelve lista de (page_index, label_capturada_o_None).
    Hace matching sobre versión normalizada del texto de página.
    Si el patrón tiene grupo de captura, toma ese grupo como label provisional.
    """
    starts = []
    for i, raw in enumerate(page_texts):
        norm = normalize_text(raw)
        # Intentamos buscar por-línea para que ^ y $ funcionen mejor
        for rx in start_rxs:
            found = None
            # Busca línea por línea
            for line in norm.split("\n"):
                m = rx.search(line)
                if m:
                    found = m
                    break
            if found:
                label = None
                if found.lastindex:  # si hay grupo capturado
                    label = sanitize_filename(found.group(1))
                starts.append((i, label))
                break
    return starts


def build_ranges_from_starts(total_pages: int, starts: List[Tuple[int, Optional[str]]]):
    ranges = []
    for k, (p_ini, label_maybe) in enumerate(starts):
        p_fin = starts[k + 1][0] if k + 1 < len(starts) else total_pages
        ranges.append((p_ini, p_fin, label_maybe))
    return ranges


def build_ranges_every_n(total_pages: int, n: int):
    ranges = []
    i = 0
    while i < total_pages:
        end = min(i + n, total_pages)
        ranges.append((i, end, None))
        i = end
    return ranges


# ================== Búsqueda de NOMBRE ==================
def compile_name_labels(raw_labels: List[str]) -> List[str]:
    """
    A partir de etiquetas como:
      "nombre del profesor(a)"
      "docente"
      "nombre del docente"
    devolvemos versiones normalizadas (sin tildes y lower) para comparación.
    """
    labels = []
    for lab in raw_labels:
        lab = normalize_text(lab)
        if lab:
            labels.append(lab)
    return labels


def extract_name_from_text_block(text_block: str, name_labels_norm: List[str]) -> Optional[str]:
    """
    Busca líneas que contengan una etiqueta (normalizada) seguida de separador y valor.
    - Compara la parte izquierda normalizada contra cualquiera de name_labels_norm.
    - Devuelve el valor a la derecha del separador como nombre.
    """
    if not text_block:
        return None

    for raw_line in text_block.splitlines():
        if not raw_line.strip():
            continue

        # Normalizamos la línea para comparar la etiqueta, pero queremos
        # extraer el valor del raw_line (sin perder mayúsculas/tildes originales).
        norm_line = normalize_text(raw_line)

        # Intentamos separar en lado-izquierdo (etiqueta) y lado-derecho (valor)
        # usando split_label_value sobre el raw, pero para chequear etiqueta,
        # tomamos la parte izquierda normalizada.
        # Para lograr esto, primero identifiquemos separador en la línea normalizada:
        sep_match = re.search(r"\s*[:=\-\u2014\u2013]\s*", norm_line)
        if not sep_match:
            continue

        # Dividimos la línea RAW por el primer separador real (para no perder acentos)
        value_raw = split_label_value(raw_line)
        if not value_raw:
            continue

        # La etiqueta la tomamos de la parte izquierda, pero desde RAW:
        left_raw = re.split(r"\s*[:=\-\u2014\u2013]\s*", raw_line, maxsplit=1)[0]
        left_norm = normalize_text(left_raw)

        # ¿La etiqueta normalizada contiene alguna de las labels?
        # Permitimos que la línea tenga más texto (ej: "Nombre del profesor(a) - titular:")
        for lab in name_labels_norm:
            # Match si la etiqueta contiene la label (no necesariamente igual exacta)
            if lab in left_norm:
                cand = value_raw.strip()
                cand = re.sub(r"\s+", " ", cand)
                # Evitar valores triviales
                if cand and len(cand) >= 2:
                    return sanitize_filename(cand)
    return None


# ================== Export y Reporte ==================
def export_ranges_to_zip_and_report(
    pdf_bytes: bytes,
    ranges: List[Tuple[int, int, Optional[str]]],
    name_labels_norm: List[str],
    prefix: str = "",
    scan_pages: int = 3,
    max_lines_for_name: int = 30,
    include_excel_inside_zip: bool = True,
) -> Tuple[bytes, bytes, pd.DataFrame, pd.DataFrame]:
    """
    Genera:
      - ZIP de PDFs (bytes)
      - Excel (bytes) con 'detalles' y 'resumen'
      - DataFrames (detalles, resumen)
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    mem_zip = io.BytesIO()

    detalles_rows = []

    with zipfile.ZipFile(mem_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (start, end, label_maybe) in enumerate(ranges, 1):
            # Nombre buscado:
            if label_maybe and label_maybe.strip():
                detected_name = label_maybe
            else:
                text_block = get_section_text_block(pdf_bytes, start, end, scan_pages, max_lines_for_name)
                detected_name = extract_name_from_text_block(text_block, name_labels_norm) or f"Plan_{idx:03d}"

            # Construir PDF del rango
            writer = PdfWriter()
            for p in range(start, end):
                writer.add_page(reader.pages[p])
            out_bytes = io.BytesIO()
            writer.write(out_bytes)
            out_bytes.seek(0)

            fname = f"{prefix}{idx:03d}_{sanitize_filename(detected_name)}.pdf"
            zf.writestr(fname, out_bytes.read())

            # Agregar fila a detalles
            detalles_rows.append({
                "orden": idx,
                "archivo": fname,
                "nombre_detectado": detected_name,
                "pagina_inicio_1based": start + 1,
                "pagina_fin_1based": end,
                "paginas_en_pdf": (end - start),
            })

        # Crear Excel
        detalles_df = pd.DataFrame(detalles_rows)
        resumen_df = (detalles_df.groupby("nombre_detectado", dropna=False)
                      .size().reset_index(name="cantidad_pdfs")
                      .sort_values(["cantidad_pdfs", "nombre_detectado"], ascending=[False, True]))

        excel_bytes = io.BytesIO()
        with pd.ExcelWriter(excel_bytes, engine="openpyxl") as writer:
            detalles_df.to_excel(writer, index=False, sheet_name="detalles")
            resumen_df.to_excel(writer, index=False, sheet_name="resumen")
        excel_bytes.seek(0)

        if include_excel_inside_zip:
            zf.writestr("reporte_division.xlsx", excel_bytes.getvalue())

    mem_zip.seek(0)
    return mem_zip.getvalue(), excel_bytes.getvalue(), detalles_df, resumen_df


# ================== UI ==================
st.set_page_config(page_title="PDF Splitter — Cortes + Nombres + Excel", page_icon="📄")
st.title("📄 Dividir PDF por cortes, nombrar por etiqueta y generar Excel de registro")
st.caption("Robusto a tildes, mayúsculas y separadores (:, -, —, –, =). Incluye reporte en Excel.")

with st.sidebar:
    st.header("⚙️ Configuración de detección")
    mode = st.radio("Modo de división", ["Por patrones de inicio", "Cada N páginas"])

    start_patterns_text = st.text_area(
        "Patrones de INICIO (regex, uno por línea). Se aplican sobre texto normalizado.",
        value="\n".join([
            r"^\s*plan\s+de\s+clase",        # (ejemplo)
            r"^\s*profesor(?:a)?\s*:\s*(.+)$",  # si aquí mismo aparece el nombre
            r"^\s*docente\s*:\s*(.+)$",
        ]),
        height=110,
        help="Si usas (.+) capturará un nombre provisional. Se hace match sobre texto sin tildes y en minúsculas."
    )

    header_lines_start = st.number_input(
        "Líneas a leer por página (INICIO)",
        min_value=0, max_value=80, value=10, step=1
    )

    n_pages = st.number_input(
        "N páginas por bloque (si eliges 'Cada N páginas')",
        min_value=1, max_value=20, value=2, step=1
    )

    st.markdown("---")
    st.subheader("📛 Etiquetas para NOMBRE")
    name_labels_text = st.text_area(
        "Etiquetas de nombre (una por línea, NO regex). Se comparan normalizadas.",
        value="\n".join([
            "nombre del profesor(a)",
            "nombre del profesor",
            "docente",
            "profesor(a)",
            "profesor",
            "nombre del docente",
            "nombre profesor",
            "nombre del cocente",  # por si viene con error tipográfico
        ]),
        height=120,
        help="Escribe cómo aparece la etiqueta a la izquierda (sin importar tildes/mayúsculas). Ej: 'Docente', 'Nombre del profesor(a)'."
    )

    header_lines_name = st.number_input(
        "Líneas a leer por página (NOMBRE)",
        min_value=1, max_value=120, value=30, step=1
    )
    scan_pages = st.number_input(
        "Páginas a escanear por sección (NOMBRE)",
        min_value=1, max_value=10, value=3, step=1
    )

    st.markdown("---")
    prefix = st.text_input("Prefijo para archivos", value="")
    include_excel_in_zip = st.checkbox("Incluir Excel dentro del ZIP", value=True)
    debug_mode = st.checkbox("🔎 Modo depuración (ver líneas analizadas para nombre)", value=False)

file = st.file_uploader("Sube tu PDF", type=["pdf"])

if file is not None:
    pdf_bytes = file.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    st.info(f"PDF cargado: **{file.name}** — {total_pages} páginas")

    if st.button("🔍 Previsualizar cortes / nombres"):
        if mode == "Por patrones de inicio":
            patterns = [p for p in start_patterns_text.splitlines() if p.strip()]
            start_rxs = compile_start_patterns(patterns)
            page_texts = get_page_texts(pdf_bytes, max_lines=header_lines_start)
            starts = detect_starts(page_texts, start_rxs)
            if not starts:
                st.warning("No se detectaron INICIOS. Ajusta patrones o usa 'Cada N páginas'.")
            else:
                ranges = build_ranges_from_starts(total_pages, starts)
                st.success(f"Detectados {len(ranges)} cortes/secciones.")
                df_prev = {
                    "Inicio (pág. 1-based)": [a+1 for a, _ in starts],
                    "Fin (pág. 1-based)": [r[1] for r in ranges],
                    "Nombre capturado en INICIO": [lbl or "" for _, lbl in starts],
                }
                st.dataframe(df_prev)

                # Intento de nombre (preview) con etiquetas:
                name_labels_norm = compile_name_labels([x for x in name_labels_text.splitlines() if x.strip()])
                preview_names = []
                for (start, end, lbl) in ranges:
                    if lbl:
                        preview_names.append(lbl)
                    else:
                        block = get_section_text_block(pdf_bytes, start, end, scan_pages, header_lines_name)
                        nm = extract_name_from_text_block(block, name_labels_norm) or ""
                        preview_names.append(nm)
                st.dataframe({"Nombre (búsqueda etiquetas)": preview_names})

                if debug_mode:
                    st.markdown("#### 🧪 Debug: primeras líneas analizadas por sección (para NOMBRE)")
                    for i, (start, end, _) in enumerate(ranges, 1):
                        block = get_section_text_block(pdf_bytes, start, end, scan_pages, header_lines_name)
                        st.markdown(f"**Sección {i}** — páginas {start+1} a {min(end, start+scan_pages)} (primeras {header_lines_name} líneas por pág.)")
                        st.code(block)
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)
            st.success(f"Se crearían {len(ranges)} archivos de {n_pages} páginas (último puede variar).")
            st.dataframe({
                "Inicio (pág. 1-based)": [r[0] + 1 for r in ranges],
                "Fin (pág. 1-based)": [r[1] for r in ranges],
            })

    st.divider()

    if st.button("✂️ Dividir, nombrar y descargar"):
        name_labels_norm = compile_name_labels([x for x in name_labels_text.splitlines() if x.strip()])

        if mode == "Por patrones de inicio":
            patterns = [p for p in start_patterns_text.splitlines() if p.strip()]
            start_rxs = compile_start_patterns(patterns)
            page_texts = get_page_texts(pdf_bytes, max_lines=header_lines_start)
            starts = detect_starts(page_texts, start_rxs)
            if not starts:
                st.error("No se detectaron INICIOS. Ajusta patrones o usa 'Cada N páginas'.")
            else:
                ranges = build_ranges_from_starts(total_pages, starts)
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)

        zip_bytes, excel_bytes, detalles_df, resumen_df = export_ranges_to_zip_and_report(
            pdf_bytes,
            ranges,
            name_labels_norm=name_labels_norm,
            prefix=prefix,
            scan_pages=scan_pages,
            max_lines_for_name=header_lines_name,
            include_excel_inside_zip=include_excel_in_zip,
        )

        st.success(f"¡Listo! Generados {len(detalles_df)} PDFs.")
        st.download_button("⬇️ Descargar ZIP de PDFs", data=zip_bytes,
                           file_name=f"{Path(file.name).stem}_split.zip", mime="application/zip")
        st.download_button("⬇️ Descargar Excel (detalles y resumen)", data=excel_bytes,
                           file_name=f"{Path(file.name).stem}_reporte.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

        st.markdown("#### Vista rápida del reporte")
        st.write("**Resumen (conteo por nombre):**")
        st.dataframe(resumen_df)
        st.write("**Detalles (un renglón por PDF):**")
        st.dataframe(detalles_df)

st.markdown("---")
with st.expander("❓ Consejos si NO encuentra los nombres"):
    st.markdown(
        """
- Activa **🔎 Modo depuración** y revisa las líneas que realmente está leyendo en cada sección.
- Aumenta **'Páginas a escanear por sección'** (por ejemplo, 5) y/o **'Líneas a leer por página (NOMBRE)'** (por ejemplo, 50).
- En **'Etiquetas de nombre'**, escribe cómo aparece a la izquierda (sin preocuparte por tildes/mayúsculas), p. ej.:
  - `nombre del profesor(a)`
  - `docente`
  - `profesor`
  - `nombre del docente`
- Si el PDF es una **imagen escaneada**, primero aplica OCR (ocrmypdf/Tesseract) para que haya texto seleccionable.
- Si el nombre viene **en otra página** distinta a la primera del plan, sube **'Páginas a escanear'**.
- Si hay guiones/largas (–, —) u otros separadores, ya están soportados.
"""
    )
