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


def get_page_texts(pdf_bytes: bytes, header_lines: int = 8) -> List[str]:
    """Extrae texto por página (puedes limitar a primeras N líneas para robustez)."""
    texts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            lines = txt.splitlines()
            if header_lines > 0:
                txt = "\n".join(lines[:header_lines])
            texts.append(txt)
    return texts


def detect_starts_by_patterns(
    page_texts: List[str],
    patterns: List[str],
) -> List[Tuple[int, Optional[str]]]:
    """
    Devuelve una lista de tuplas (page_index, label_capturada_o_None)
    - Si el patrón tiene grupo de captura, usamos ese texto como label inicial.
    - Si no, dejamos None para intentar nombrar luego con patrones de nombre.
    """
    regexes = [re.compile(pat, re.IGNORECASE) for pat in patterns]
    starts = []
    for i, txt in enumerate(page_texts):
        label = None
        for rx in regexes:
            m = rx.search(txt)
            if m:
                if m.lastindex:  # grupo capturado
                    label = sanitize_filename(m.group(1))
                else:
                    label = None
                starts.append((i, label))
                break
    return starts


def build_ranges_from_starts(total_pages: int, starts: List[Tuple[int, Optional[str]]]):
    """Convierte inicios (página, etiqueta_maybe) a rangos (ini, fin, etiqueta_maybe)."""
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


def extract_name_from_text_block(
    text_block: str,
    naming_regexes: List[re.Pattern],
) -> Optional[str]:
    """
    Busca la primera coincidencia del tipo:
    ^ ... : <NOMBRE>
    y devuelve el nombre (lo que sigue a ":") limpio.
    """
    for rx in naming_regexes:
        for line in text_block.splitlines():
            m = rx.search(line)
            if m:
                # Si el patrón tiene grupo de captura úsalo, si no toma lo que sigue a ':'
                if m.lastindex:
                    raw = m.group(1)
                else:
                    parts = line.split(":", 1)
                    raw = parts[1] if len(parts) == 2 else ""
                name = sanitize_filename(raw.strip())
                if name:
                    return name
    return None


def extract_section_name(
    pdf_bytes: bytes,
    start_page: int,
    end_page: int,
    naming_patterns: List[str],
    scan_pages: int = 3,
    header_lines_for_name: int = 20,
) -> Optional[str]:
    """
    Toma unas pocas páginas de la sección (desde start_page) y busca
    una línea tipo 'Etiqueta: Valor' según naming_patterns. Devuelve 'Valor'.
    """
    naming_regexes = [re.compile(pat, re.IGNORECASE) for pat in naming_patterns]

    # Abrimos de nuevo con pdfplumber para extraer más líneas en la zona de nombre
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        stop = min(end_page, start_page + scan_pages)
        buf = []
        for p in range(start_page, stop):
            txt = (pdf.pages[p].extract_text() or "")
            lines = txt.splitlines()
            # para buscar el nombre, leamos más líneas (ej. 20) por si la etiqueta no está en la cabecera-cabecera
            buf.append("\n".join(lines[:header_lines_for_name]))
        text_block = "\n".join(buf)

    return extract_name_from_text_block(text_block, naming_regexes)


def export_ranges_to_zip(
    pdf_bytes: bytes,
    ranges: List[Tuple[int, int, Optional[str]]],
    naming_patterns: List[str],
    prefix: str = "",
) -> bytes:
    """Crea un ZIP; para cada rango intenta poner el nombre detectado."""
    reader = PdfReader(io.BytesIO(pdf_bytes))
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, (start, end, label_maybe) in enumerate(ranges, 1):
            # Si no hay label todavía, intenta extraerlo con patrones de nombre
            if not label_maybe:
                name = extract_section_name(pdf_bytes, start, end, naming_patterns)
            else:
                name = label_maybe

            if not name:
                name = f"Plan_{idx:03d}"

            writer = PdfWriter()
            for p in range(start, end):
                writer.add_page(reader.pages[p])
            out_bytes = io.BytesIO()
            writer.write(out_bytes)
            out_bytes.seek(0)
            fname = f"{prefix}{idx:03d}_{sanitize_filename(name)}.pdf"
            zf.writestr(fname, out_bytes.read())
    mem_zip.seek(0)
    return mem_zip.getvalue()


# ========= UI =========
st.set_page_config(page_title="PDF Splitter por Criterios (Profes/Planes)", page_icon="📄")

st.title("📄 Dividir PDF y nombrar por 'Etiqueta: Valor'")
st.caption(
    "Separa un PDF por patrones (inicio de plan) y nombra cada archivo con el texto que sigue a ':' en una etiqueta (por ejemplo, "
    "'NOMBRE DEL PROFESOR(A): Nombre Apellido')."
)

with st.sidebar:
    st.header("⚙️ Configuración")

    mode = st.radio(
        "Modo de división",
        options=["Por patrones de inicio", "Cada N páginas"],
    )

    default_start_patterns = (
        r"^\s*Plan\s+de\s+clase\n"
        r"^\s*Profesor(?:a)?\s*:\s*(.+)$\n"
        r"^\s*Docente\s*:\s*(.+)$"
    )
    start_patterns_text = st.text_area(
        "Patrones de INICIO (uno por línea). Usa (.+) si quieres capturar un nombre en esa MISMA línea.",
        value=default_start_patterns if mode == "Por patrones de inicio" else "",
        height=120,
        help="Ejemplos:\n"
             r"^\s*Plan\s+de\s+clase"
             "\n"
             r"^\s*Profesor\s*:\s*(.+)$"
             "\n"
             r"^\s*Docente\s*:\s*(.+)$"
    )

    header_lines_start = st.number_input(
        "Líneas a leer por página (detección de INICIO)",
        min_value=0, max_value=50, value=8, step=1
    )

    n_pages = st.number_input(
        "N páginas por bloque (si eliges 'Cada N páginas')",
        min_value=1, max_value=20, value=2, step=1
    )

    st.markdown("---")
    st.subheader("📛 Patrones para NOMBRE")
    default_name_patterns = (
        r"^\s*NOMBRE\s+DEL\s+PROFESOR\(A\)\s*:\s*(.+)$\n"
        r"^\s*Profesor(?:a)?\s*:\s*(.+)$\n"
        r"^\s*Docente\s*:\s*(.+)$"
    )
    naming_patterns_text = st.text_area(
        "Patrones de NOMBRE (uno por línea). Tomaremos lo que sigue a ':' (o el primer grupo capturado).",
        value=default_name_patterns,
        height=120,
        help="Ejemplos:\n"
             r"^\s*NOMBRE\s+DEL\s+PROFESOR\(A\)\s*:\s*(.+)$"
             "\n"
             r"^\s*Profesor\s*:\s*(.+)$"
             "\n"
             r"^\s*Docente\s*:\s*(.+)$"
    )
    header_lines_name = st.number_input(
        "Líneas a leer por página (búsqueda de NOMBRE)",
        min_value=1, max_value=80, value=20, step=1
    )
    scan_pages = st.number_input(
        "Páginas a escanear desde el INICIO de la sección para hallar el NOMBRE",
        min_value=1, max_value=10, value=3, step=1
    )

    prefix = st.text_input(
        "Prefijo para nombre de archivos (opcional)",
        value="",
        help="Ej: '2025_Profesores_'."
    )

file = st.file_uploader("Sube tu PDF", type=["pdf"])

if file is not None:
    pdf_bytes = file.read()
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    st.info(f"PDF cargado: **{file.name}** — {total_pages} páginas")

    if st.button("🔍 Previsualizar detecciones"):
        if mode == "Por patrones de inicio":
            if not start_patterns_text.strip():
                st.error("Agrega al menos un patrón de INICIO.")
            else:
                patterns = [p for p in start_patterns_text.splitlines() if p.strip()]
                texts_for_start = get_page_texts(pdf_bytes, header_lines=header_lines_start)
                starts = detect_starts_by_patterns(texts_for_start, patterns)
                if not starts:
                    st.warning("No se detectaron INICIOS. Ajusta los patrones y prueba otra vez.")
                else:
                    ranges = build_ranges_from_starts(total_pages, starts)
                    st.success(f"Detectados {len(ranges)} cortes/secciones.")
                    st.dataframe(
                        {
                            "Inicio (página 1-based)": [a+1 for a, _ in starts],
                            "Etiqueta capturada en INICIO": [b or "" for _, b in starts],
                            "Fin (pág. 1-based)": [r[1] for r in ranges],
                        }
                    )
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)
            st.success(f"Se crearían {len(ranges)} archivos (bloques de {n_pages} páginas).")
            st.dataframe(
                {
                    "Inicio (pág. 1-based)": [r[0] + 1 for r in ranges],
                    "Fin (pág. 1-based)": [r[1] for r in ranges],
                    "Etiqueta capturada en INICIO": [r[2] or "" for r in ranges],
                }
            )

    st.divider()

    if st.button("✂️ Dividir, nombrar y descargar ZIP"):
        naming_patterns = [p for p in naming_patterns_text.splitlines() if p.strip()]

        if mode == "Por patrones de inicio":
            patterns = [p for p in start_patterns_text.splitlines() if p.strip()]
            texts_for_start = get_page_texts(pdf_bytes, header_lines=header_lines_start)
            starts = detect_starts_by_patterns(texts_for_start, patterns)
            if not starts:
                st.error("No se detectaron INICIOS. Ajusta patrones o usa el modo 'Cada N páginas'.")
            else:
                ranges = build_ranges_from_starts(total_pages, starts)
                # Empaquetar a ZIP con nombres
                # (Para buscar nombre, leeremos hasta 'scan_pages' y 'header_lines_name')
                # Reutilizamos export_ranges_to_zip que llama extract_section_name internamente.
                zip_bytes = export_ranges_to_zip(
                    pdf_bytes,
                    ranges,
                    naming_patterns=naming_patterns,
                    prefix=prefix,
                )
                st.success(f"Listo: {len(ranges)} archivos generados.")
                st.download_button(
                    "⬇️ Descargar ZIP",
                    data=zip_bytes,
                    file_name=f"{Path(file.name).stem}_split.zip",
                    mime="application/zip",
                )
        else:
            ranges = build_ranges_every_n(total_pages, n_pages)
            zip_bytes = export_ranges_to_zip(
                pdf_bytes,
                ranges,
                naming_patterns=naming_patterns,
                prefix=prefix,
            )
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
- **Patrones de INICIO**: sirven para marcar dónde empieza cada plan. Si pones `(.*)` en el patrón, lo capturado puede usarse como nombre si no encuentras uno mejor con los **Patrones de NOMBRE**.
- **Patrones de NOMBRE**: líneas tipo `Etiqueta: Valor`. Tomamos el **Valor** que sigue a `:` o el **grupo capturado** `(.*)` según tu regex.
- Suele funcionar bien con:
  - `^\\s*NOMBRE\\s+DEL\\s+PROFESOR\\(A\\)\\s*:\\s*(.+)$`
  - `^\\s*Profesor(?:a)?\\s*:\\s*(.+)$`
  - `^\\s*Docente\\s*:\\s*(.+)$`
- Si tu PDF es escaneado (imágenes), primero OCR.
"""
    )
