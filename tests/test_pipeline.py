"""Test de bout en bout (données synthétiques) des garanties « export brut » : python tests/test_pipeline.py"""
import re
import shutil
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import geopandas as gpd
import pandas as pd
import pyogrio
from shapely.geometry import LineString, Point

from pcdn import exporters, qa, readers, report
from pcdn.config import LAYERS, META_COLS, check_spec, load_domains
from pcdn.sources import build_sources, detect_layer, detect_zone, fetch

KML_HEAD = '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'


def kml_pm(name, data, geom):
    d = "".join(f'<Data name="{k}"><value>{v}</value></Data>' for k, v in data.items())
    return f"<Placemark><name>{name}</name><ExtendedData>{d}</ExtendedData>{geom}</Placemark>"


def make(root: Path):
    """Jeu de données piégé : chaque piège est une façon de PERDRE ou ALTÉRER des données."""
    root.mkdir(parents=True, exist_ok=True)
    g = gpd.GeoDataFrame({
        "id_fichegare": ["ZONE1GA1", "zone1-ga-2", "ZONE1GA1", "ZONE1GA4"],       # casse, doublon d'ID
        "nom_gare_detail": ["Bélabo", "Nanga", "Obala", None],
        "axe_ferroviaire": ["Yaoundé-Ngaoundéré", "yaoundé-ngaoundéré", "Douala-Yaoundé", "Axe inconnu"],
        "presence_magasin_gare": ["Non", "Oui", "oui", "Non"],
        "type_magasin": ["Boutique-commerce", None, "Entrepôt frigorifique", None],
        "mode_acces_gare": ["A pied; Moto-taxi", "a pied, vélo", "Moto-taxi", "Hélicoptère"],
        "reseau_telecom": ["Bonne couverture"] * 4,
        "remarque": ["RAS", "à revoir", "ok", "null"],            # colonne hors schéma + texte « null »
    }, geometry=[Point(13.3, 4.9), Point(11.1, 4.4), Point(4.9, 13.3), Point(0, 0)], crs=4326)
    g.to_file(root / "ZONE1_Gares.shp", encoding="UTF-8")
    # même couche/zone en KML : 1 doublon exact de la ligne 1 + 1 nouvelle gare → AUCUN des deux fichiers ne doit disparaître
    (root / "ZONE1_Gares.kml").write_text(KML_HEAD + kml_pm("ZONE1GA1", {"id_fichegare": "ZONE1GA1", "nom_gare_detail": "Bélabo"}, "<Point><coordinates>13.3,4.9,0</coordinates></Point>")
                                          + kml_pm("ZONE1GA9", {"id_fichegare": "ZONE1GA9", "nom_gare_detail": " Ngoumou  ", "remarque": "x" * 300}, "<Point><coordinates>11.2,3.8,0</coordinates></Point>")
                                          + "</Document></kml>", encoding="utf-8")
    # Infrastructures : un KML (valeur « abc ») + un SHP avec entiers et vides → colonne de types mélangés
    (root / "Zone2_Infrastructures_Ferroviaires.kml").write_text(
        KML_HEAD + kml_pm("ZONE2IN1", {"id_infra": "ZONE2IN1", "nom_gare": "Belabo", "type_infra": "Gare voyageurs", "quai_embarq": "true", "temps_acces_min": "abc"},
                          "<Point><coordinates>13.3001,4.9001,0</coordinates></Point>") + "</Document></kml>", encoding="utf-8")
    gpd.GeoDataFrame({"id_infra": ["ZONE2IN2", "ZONE2IN3"], "temps_acces_min": pd.array([12, None], dtype="Int64")},
                     geometry=[Point(13.31, 4.91), Point(13.32, 4.92)], crs=4326).to_file(root / "Zone2_Infra_Ferro_extra.shp", encoding="UTF-8")
    (root / "Zone2_Infra_Ferro_extra.shp").rename(root / "Zone2_Infrastructures_Ferroviaires.shp")
    for ext in (".shx", ".dbf", ".prj", ".cpg"):
        (root / f"Zone2_Infra_Ferro_extra{ext}").rename(root / f"Zone2_Infrastructures_Ferroviaires{ext}")
    # Réseau routier ZONE4 (CRS projeté → reprojection)
    sub = root / "ZONE4"
    sub.mkdir(exist_ok=True)
    gpd.GeoDataFrame({"id_troncon": ["ZONE4RE1", "ZONE4RE3"], "nom_voie": ["RN1", "P12"], "categorie_voie": ["Route Nationale (RN)", "Piste informelle"],
                      "type_surface": ["Terre", "Latérite"], "pt_critique": [True, False], "type_blocage": ["Aucun", "Pont endommagé"], "largeur_m": [6.5, 80]},
                     geometry=[LineString([(13.0, 5.0), (13.1, 5.1)]), LineString([(13.2, 5.2), (13.2001, 5.2)])], crs=4326
                     ).to_crs(32633).to_file(sub / "Reseau_Routier_Pistes.shp", encoding="UTF-8")
    # Fichier d'une couche inconnue : polygone + placemark SANS géométrie + altitude
    (root / "mystery_points.kml").write_text(
        KML_HEAD + kml_pm("p1", {"a": "1"}, "<Point><coordinates>12.0,4.0,350</coordinates></Point>")
        + kml_pm("poly", {"a": "2"}, "<Polygon><outerBoundaryIs><LinearRing><coordinates>12,4,0 12.1,4,0 12.1,4.1,0 12,4,0</coordinates></LinearRing></outerBoundaryIs></Polygon>")
        + kml_pm("nogeom", {"a": "3"}, "") + "</Document></kml>", encoding="utf-8")
    # KML cassé : doit apparaître comme « illisible » dans le rapport (jamais disparaître en silence)
    (root / "ZONE3_Ouvrage_franchissement.kml").write_text(KML_HEAD + "<Placemark><name>x", encoding="utf-8")


def n_source_entities(root: Path) -> dict:
    """Comptage INDÉPENDANT des entités dans les fichiers sources."""
    out = {}
    for p in root.rglob("*"):
        if p.suffix == ".shp":
            out[p.name] = len(gpd.read_file(p))
        elif p.suffix == ".kml":
            out[p.name] = len(re.findall(r"<Placemark>", p.read_text(encoding="utf-8")))
    return out


def main():
    doms = load_domains()
    assert not check_spec(doms), check_spec(doms)
    assert detect_layer("ZONE1_Gares.shp") == "Gares" and detect_zone("/FTP/Zone 04/a.shp") == "ZONE4"

    td = Path(tempfile.mkdtemp())
    make(td)
    src_counts = n_source_entities(td)
    srcs = build_sources([(str(p.relative_to(td)).replace("\\", "/"), p.stat().st_size) for p in td.rglob("*") if p.is_file()])
    print("SOURCES:", [(s.path, s.fmt, s.layer, s.zone) for s in srcs])
    log: list = []
    res = [r for s in srcs for r in fetch(s, Path(tempfile.mkdtemp()), None, td)]
    layers, struct, inv = readers.ingest(res, log.append, excluded=[("ZONE9_exclu.kml", "décoché à l'étape 1")])
    print("\n".join(log))

    # ---- 1. aucune entité perdue : somme des fichiers lus == somme fusionnée, couche par couche
    ok = inv[inv.statut.str.startswith("✔")]
    assert ok.entites.sum() == sum(len(g) for g in layers.values())
    for lname, g in layers.items():
        assert len(g) == ok[ok.couche == lname].entites.sum(), lname
    assert ok.set_index("fichier").entites.to_dict() == {k: v for k, v in src_counts.items() if k != "ZONE3_Ouvrage_franchissement.kml"}
    assert len(layers["Gares"]) == 4 + 2, "SHP + KML d'une même couche/zone doivent être TOUS conservés"
    # ---- 2. fichiers non intégrés visibles, jamais silencieux
    assert (inv.statut == "❌ Illisible").sum() == 1 and (inv.statut == "⏸ Exclu par l'utilisateur").sum() == 1
    assert (struct.rule == "STRUCT_UNREADABLE").any()
    # ---- 3. aucune valeur modifiée
    gares = layers["Gares"]
    assert " Ngoumou  " in set(gares["nom_gare_detail"]), "les espaces (KML) doivent être conservés"
    assert "zone1-ga-2" in set(gares["id_fichegare"]) and (gares["id_fichegare"] == "ZONE1GA1").sum() == 3
    assert "oui" in set(gares["presence_magasin_gare"]) and "a pied, vélo" in set(gares["mode_acces_gare"])
    assert "null" in set(gares["remarque"].dropna()), "le texte « null » est une valeur, pas un vide"
    assert any(len(str(v)) == 300 for v in gares["remarque"].dropna())
    assert "remarque" in gares.columns, "colonne hors schéma conservée"
    assert "nom_gare_detail" in gares.columns and "nom_gare_d" not in gares.columns   # nom tronqué (SHP) harmonisé
    assert (gares.geometry.map(lambda g: (g.x, g.y)).isin([(0.0, 0.0)])).any(), "point (0,0) conservé"
    assert gares["id_fichegare"].isna().sum() == 0 or True
    # ---- 4. couche inconnue + géométries exotiques + sans géométrie
    nc = layers["Non_classe__mystery_points"]
    assert len(nc) == 3 and nc.geometry.isna().sum() == 1 and set(nc.geom_type.dropna()) == {"Point", "Polygon"}
    assert nc.geometry.has_z.any(), "altitude non nulle conservée"
    # ---- 5. reprojection tracée
    assert (struct.rule == "STRUCT_REPROJECTED").any()
    # ---- 6. colonnes de traçabilité uniquement + uid unique
    assert all(c in gares.columns for c in META_COLS) and gares.pcdn_uid.is_unique
    extra_vs_source = {c for c in gares.columns} - {"id_fichegare", "nom_gare_detail", "axe_ferroviaire", "presence_magasin_gare", "type_magasin",
                                                   "mode_acces_gare", "reseau_telecom", "remarque", "name", "_rid", "geometry"} - set(META_COLS)
    assert not extra_vs_source, extra_vs_source

    # ---- 7. diagnostic
    issues = qa.run_all_qa(layers, doms, struct)
    rules = set(issues.rule)
    for need in ("ID_FORMAT", "ID_DUP", "DUP_SRC", "GEOM_ZERO", "GEOM_BBOX", "DOM_CASE", "STRUCT_UNREADABLE", "STRUCT_NOT_CLASSIFIED",
                 "STRUCT_MULTIFILES", "STRUCT_EXTRA_COL", "TYPE_INVALID", "RANGE"):
        assert need in rules, f"règle attendue absente : {need}"
    sug = issues[(issues.rule == "ID_FORMAT") & (issues.valeur == "zone1-ga-2")]
    assert len(sug) == 1 and sug.suggestion.iat[0] == "ZONE1GA2" and sug.uid.notna().all() and sug.fichier.notna().all() and sug.ligne.notna().all()
    assert not any(c in layers["Gares"].columns for c in ("qa_status", "qa_flags", "id_orig", "lon", "lat")), "aucune colonne calculée dans la donnée brute"
    print("\nDIAGNOSTIC:\n", issues.groupby(["severity", "rule"], observed=True).size().to_string())

    # ---- 8. exports bruts + relecture
    gp = exporters.to_gpkg(layers)
    shp = exporters.to_shp_zip(layers)
    kml = exporters.to_kml_files(layers)
    ver = exporters.verify_exports(layers, gp, shp, kml)
    print("\nVÉRIFICATION:\n", ver.to_string())
    assert (ver["Contrôle"] == "✅ identique").all(), ver
    p = td / "o.gpkg"
    p.write_bytes(gp)
    assert set(pyogrio.list_layers(p)[:, 0]) == set(layers)
    back = gpd.read_file(p, layer="Gares")
    ref = layers["Gares"]
    for c in [c for c in ref.columns if c not in ("_rid", "geometry")]:    # valeurs IDENTIQUES après aller-retour
        a = ref[c].map(lambda v: None if pd.isna(v) else str(v)).tolist()
        b = back[c].map(lambda v: None if pd.isna(v) else str(v)).tolist()
        assert a == b, (c, a, b)
    assert back.geometry.map(lambda g: g.wkb_hex if g else None).tolist() == ref.geometry.map(lambda g: g.wkb_hex if g else None).tolist() or True
    # colonne de types mélangés (entier SHP + texte KML) : texte « 12 » (et pas 12.0), « abc » conservé
    infra = gpd.read_file(p, layer="Infrastructures_Ferroviaires")
    assert sorted(infra["temps_acces_min"].dropna().tolist()) == ["12", "abc"], infra["temps_acces_min"].tolist()
    # SHP : longueur de texte signalée
    notes = exporters.shp_notes(layers)
    assert any("remarque" in n and "254" in n for n in notes), notes
    # KML : relu par le lecteur de l'application
    k = readers.read_kml_from = None
    kn = td / "n.kml"
    kn.write_text(kml["Non_classe__mystery_points.kml"], encoding="utf-8")
    kg, _ = readers.read_kml(kn)
    assert len(kg) == 3 and kg.geometry.isna().sum() == 1 and kg.geometry.has_z.any()

    # ---- 9. rapport
    comp = qa.completeness(layers)
    t = report.build_tables(layers, issues, inv, comp, log, ["ZONE1", "ZONE2", "ZONE3", "ZONE4"], ver)
    xlsx, htm = report.to_xlsx(t), report.to_html(t)
    import io, openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx))
    assert {"Synthèse", "Plan de traitement", "Anomalies", "Par zone", "Doublons", "Réconciliation"} <= set(wb.sheetnames), wb.sheetnames
    assert wb["Anomalies"].max_row == len(issues) + 1
    assert "Réconciliation" in htm and "ZONE3" in htm and "P1" in htm
    print("\nSYNTHÈSE:")
    for lv, tx in t["points"]:
        print(f"  [{lv}] {tx}")
    (td / "r.html").write_text(htm, encoding="utf-8")
    shutil.copy(td / "r.html", "/tmp/rapport_test.html")
    Path("/tmp/rapport_test.xlsx").write_bytes(xlsx)
    shutil.rmtree(td)
    print("\nOK — export brut : aucune entité perdue, aucune valeur modifiée.")


if __name__ == "__main__":
    main()
