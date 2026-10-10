"""Démarrage de l'UI et parcours complet (lecture → rapport → export) : python tests/test_app.py"""
import sys
import tempfile
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from streamlit.testing.v1 import AppTest

from pcdn import qa, readers
from pcdn.config import load_domains
from pcdn.sources import build_sources, fetch
from tests.test_pipeline import make

td = Path(tempfile.mkdtemp())
make(td)
doms = load_domains()
srcs = build_sources([(str(p.relative_to(td)).replace("\\", "/"), p.stat().st_size) for p in td.rglob("*") if p.is_file()])
res = [r for s in srcs for r in fetch(s, Path(tempfile.mkdtemp()), None, td)]
raw, struct, inv = readers.ingest(res, lambda m: None)
issues = qa.run_all_qa(raw, doms, struct)

at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
at.run()
assert not at.exception, at.exception
print("démarrage OK")
at.session_state["raw"], at.session_state["struct"], at.session_state["inventory"], at.session_state["issues"] = raw, struct, inv, issues
at.run()
assert not at.exception, at.exception
print("fusion / diagnostic / rapport OK")
btn = [b for b in at.button if "Générer" in b.label][0]
btn.click()
at.run()
assert not at.exception, at.exception
v = at.session_state["verify"]
assert (v["Contrôle"] == "✅ identique").all(), v
assert at.session_state["bundle"]
import io, zipfile
names = zipfile.ZipFile(io.BytesIO(at.session_state["bundle"])).namelist()
print("export OK", len(names), "fichiers :", [n for n in names if not n.startswith(("shp/", "kml/"))])
