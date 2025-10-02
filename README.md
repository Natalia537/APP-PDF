# PDF Splitter por Criterios (Streamlit)

App en Streamlit para dividir un PDF grande:
- **Por patrones de texto** (ej. `Profesor: Nombre`)
- **Cada N páginas** (rápido)

## Uso local
```bash
python -m venv .venv
source .venv/bin/activate   # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
