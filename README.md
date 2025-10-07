# PDF Splitter (Profes + Excel)

Divide un PDF grande en secciones y nombra cada PDF con el valor que sigue a:
**NOMBRE DEL PROFESOR(A): ...**

- Soporta separadores `:`, `-`, `–`, `—`, `=`
- Limpia prefijos (DR., DRA., LIC., ING., MSC., MAG., MTR., PHD, PROF., etc.)
- Genera `reporte.xlsx` con hojas `detalles` y `resumen` (openpyxl)
- Liviano para Streamlit Cloud (no usa pandas/numpy)

## Despliegue
1. Asegura `APP.py` como Main file (coincide con tu despliegue).
2. `requirements.txt` y `runtime.txt` como en el repo.
3. Si se queda en "Your app is in the oven", revisa logs y confirma estas versiones.

## Local
