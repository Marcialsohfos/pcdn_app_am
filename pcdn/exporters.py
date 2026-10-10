"""Exports BRUTS : GeoPackage, Shapefile (noms ≤ 10 car.), KML — et contrôle de réconciliation après écriture.

Garanties :
  * toutes les entités fusionnées sont écrites (y compris sans géométrie), aucune n'est filtrée ni modifiée ;
  * toutes les colonnes sources sont écrites telles quelles + 5 colonnes de traçabilité (pcdn_uid, pcdn_zone, src_*) ;
  * aucun statut QA, aucune colonne calculée n'est injecté dans les données (le diagnostic vit dans le rapport) ;
  * `verify_exports` relit les fichiers produits et compare nombre d'entités et identifiants pcdn_uid.
"""
from __future__ import annotations

import io
import re
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely

from .config import LAYERS
from .utils import raw_null, safe_name

HIDDEN = {"_rid", "geometry"}
SHP_TEXT_MAX = 254          # limite du format DBF
PALETTE = ["ff50b45a", "ff1e9bf5", "ffdc3c3c", "fff0a030", "ffa05cc8", "ffc8c846", "ff3cb4dc", "ff808080", "ff4678e6", "ff2896a0"]


def export_cols(g: gpd.GeoDataFrame) -> list[str]:
    return [c for c in g.columns if c not in HIDDEN]


def _is_text(s: pd.Series) -> bool:
    return s.dtype == object or pd.api.types.is_string_dtype(s)


def _prep(g: gpd.GeoDataFrame, shp: bool = False) -> gpd.GeoDataFrame:
    """Rend les colonnes écrivables SANS changer les valeurs : les colonnes de types mélangés (SHP + KML)
    sont écrites en texte, la représentation textuelle de chaque valeur étant conservée."""
    cols = export_cols(g)
    out = g[cols + ["geometry"]].copy()
    for c in cols:
        s = out[c]
        if _is_text(s):
            out[c] = s.map(lambda v: None if raw_null(v) else (v if isinstance(v, str) else str(v))).astype(object)
        elif shp and pd.api.types.is_datetime64_any_dtype(s):
            out[c] = s.map(lambda v: None if raw_null(v) else v.isoformat()).astype(object)
    return out


def shp_names(cols) -> dict[str, str]:
    used, m = set(), {}
    for c in cols:
        s = c[:10]
        i = 0
        while s.lower() in used:
            i += 1
            s = f"{c[:10 - len(str(i)) - 1]}_{i}"
        used.add(s.lower())
        m[c] = s
    return m


# ------------------------------------------------------------------ GeoPackage
def to_gpkg(layers: dict, extra_tables: dict | None = None) -> bytes:
    """Une table par couche fusionnée. `extra_tables` (facultatif) : tables non spatiales jointes par pcdn_uid."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "PCDN_brut.gpkg"
        for name, g in layers.items():
            _prep(g).to_file(p, layer=name, driver="GPKG", engine="pyogrio")
        for name, df in (extra_tables or {}).items():
            pyogrio.write_dataframe(df, p, layer=name, driver="GPKG")
        return p.read_bytes()


# ------------------------------------------------------------------ Shapefile
def _family(geom) -> str | None:
    if geom is None or geom.is_empty:
        return None
    t = geom.geom_type
    return {"Point": "Point", "MultiPoint": "MultiPoint", "LineString": "Ligne", "MultiLineString": "Ligne",
            "Polygon": "Polygone", "MultiPolygon": "Polygone"}.get(t, "Collection")


def shp_notes(layers: dict) -> list[str]:
    """Limites du format Shapefile à signaler (valeurs tronquées, géométries non représentables…)."""
    notes = []
    for name, g in layers.items():
        for c in export_cols(g):
            if _is_text(g[c]):
                n = int(g[c].map(lambda v: isinstance(v, str) and len(v) > SHP_TEXT_MAX).sum())
                if n:
                    notes.append(f"{name}.{c} : {n} valeur(s) > {SHP_TEXT_MAX} caractères seront tronquées dans le SHP (complètes dans GPKG et KML).")
        fam = g.geometry.map(_family)
        nc = int((fam == "Collection").sum())
        if nc:
            notes.append(f"{name} : {nc} géométrie(s) de type collection non représentable(s) en SHP (écrites SANS géométrie ; complètes dans GPKG et KML).")
        kinds = sorted({f for f in fam.dropna().unique() if f != "Collection"})
        if len(kinds) > 1:
            notes.append(f"{name} : géométries de types différents ({', '.join(kinds)}) → un fichier SHP par type.")
        if g.geometry.has_z.any():
            notes.append(f"{name} : altitudes (Z) présentes → SHP de type « Z » (les entités 2D reçoivent Z=0).")
        if (fam.isna()).any():
            notes.append(f"{name} : {int(fam.isna().sum())} entité(s) sans géométrie (écrites avec géométrie nulle dans le SHP).")
        if len(g) == 0:
            notes.append(f"{name} : couche vide (0 entité), aucun SHP écrit.")
    return notes


def to_shp_zip(layers: dict) -> bytes:
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        maps = []
        for name, g0 in layers.items():
            if g0.empty:
                continue
            g = _prep(g0, shp=True)
            fam = g.geometry.map(_family)
            g.loc[fam == "Collection", "geometry"] = None
            fam = fam.where(fam != "Collection", None)
            kinds = [k for k in ("Point", "MultiPoint", "Ligne", "Polygone") if (fam == k).any()]
            groups = [(k, g[(fam == k) | (fam.isna() & (k == kinds[0]))]) for k in kinds] if kinds else [("Table", g)]
            m = shp_names([c for c in g.columns if c != "geometry"])
            for kind, part in groups:
                stem = name if len(groups) == 1 else f"{name}__{kind}"
                d = Path(td) / stem
                d.mkdir()
                part.rename(columns=m).to_file(d / f"{stem}.shp", driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
                for f in sorted(d.iterdir()):
                    z.write(f, f"{name}/{f.name}")
                maps += [(name, f"{stem}.shp", long, short) for long, short in m.items()]
        z.writestr("correspondance_champs_shp.csv",
                   pd.DataFrame(maps, columns=["couche", "fichier_shp", "champ_complet", "champ_shp"]).to_csv(index=False, encoding="utf-8-sig"))
    return buf.getvalue()


def shp_field_map(layers: dict) -> pd.DataFrame:
    rows = []
    for name, g in layers.items():
        m = shp_names(export_cols(g))
        rows += [(name, long, short, long != short) for long, short in m.items()]
    return pd.DataFrame(rows, columns=["couche", "champ_complet", "champ_shp", "tronque"])


# ------------------------------------------------------------------ KML
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _x(v) -> str:
    return escape(_XML_BAD.sub("", str(v)))


def _fmt(v: float) -> str:
    return f"{v:.15g}"


def _coords(geom) -> str:
    z = geom.has_z
    return " ".join(",".join(_fmt(c) for c in (pt if z else pt[:2])) for pt in geom.coords)


def _geom_kml(g) -> str:
    t = g.geom_type
    if t == "Point":
        return f"<Point><coordinates>{_coords(g)}</coordinates></Point>"
    if t in ("LineString", "LinearRing"):
        return f"<LineString><tessellate>1</tessellate><coordinates>{_coords(g)}</coordinates></LineString>"
    if t == "Polygon":
        inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{_coords(r)}</coordinates></LinearRing></innerBoundaryIs>"
                        for r in g.interiors)
        return (f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{_coords(g.exterior)}</coordinates></LinearRing>"
                f"</outerBoundaryIs>{inner}</Polygon>")
    return "<MultiGeometry>" + "".join(_geom_kml(p) for p in g.geoms) + "</MultiGeometry>"


def kml_folder(g: gpd.GeoDataFrame, layer: str, style: str = "s0") -> str:
    cols = export_cols(g)
    idf = LAYERS[layer].id_field if layer in LAYERS else None
    out = [f"<Folder><name>{_x(layer)}</name>"]
    for _, r in g.iterrows():
        label = next((r[c] for c in (idf, "name", "pcdn_uid") if c and c in g.columns and not raw_null(r[c]) and str(r[c]) != ""), "")
        data = "".join(f'<Data name="{_x(c)}"><value>{_x(r[c])}</value></Data>' for c in cols if not raw_null(r[c]))
        geom = r.geometry
        gx = "" if (geom is None or geom.is_empty) else _geom_kml(geom)
        out.append(f"<Placemark><name>{_x(label)}</name><styleUrl>#{style}</styleUrl><ExtendedData>{data}</ExtendedData>{gx}</Placemark>")
    out.append("</Folder>")
    return "".join(out)


def _kml_doc(title: str, body: str, n_styles: int = 1, offset: int = 0) -> str:
    st = "".join(
        f'<Style id="s{k}"><IconStyle><color>{PALETTE[(k + offset) % len(PALETTE)]}</color><scale>1.1</scale></IconStyle>'
        f'<LineStyle><color>{PALETTE[(k + offset) % len(PALETTE)]}</color><width>3</width></LineStyle></Style>' for k in range(n_styles))
    return (f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{_x(title)}</name>{st}{body}</Document></kml>")


def to_kml_files(layers: dict) -> dict[str, str]:
    names = list(layers)
    files = {f"{safe_name(n)}.kml": _kml_doc(n, kml_folder(g, n, "s0"), 1, i) for i, (n, g) in enumerate(layers.items())}
    files["PCDN_toutes_couches.kml"] = _kml_doc(
        "PCDN (brut)", "".join(kml_folder(g, n, f"s{i}") for i, (n, g) in enumerate(layers.items())), len(names))
    return files


# ------------------------------------------------------------------ contrôle de réconciliation
def verify_exports(layers: dict, gpkg: bytes | None = None, shp_zip: bytes | None = None,
                   kml_files: dict | None = None) -> pd.DataFrame:
    """Relit les fichiers PRODUITS et compare, couche par couche, effectifs et identifiants pcdn_uid à la donnée fusionnée."""
    exp = {n: set(g["pcdn_uid"]) for n, g in layers.items()}
    res: dict[str, dict] = {n: {"Couche": n, "Fusionné": len(g)} for n, g in layers.items()}
    fmt_ok: dict[str, list] = {n: [] for n in layers}

    def check(n, col, uids, count):
        res[n][col] = count
        ok = count == len(exp[n]) and (uids is None or set(uids) == exp[n])
        fmt_ok[n].append(ok)

    with tempfile.TemporaryDirectory() as td:
        if gpkg is not None:
            p = Path(td) / "v.gpkg"
            p.write_bytes(gpkg)
            have = set(pyogrio.list_layers(p)[:, 0])
            for n in layers:
                if n in have:
                    df = pyogrio.read_dataframe(p, layer=n, columns=["pcdn_uid"], read_geometry=False)
                    check(n, "GeoPackage", df["pcdn_uid"].tolist(), len(df))
                else:
                    check(n, "GeoPackage", None, 0)
        if shp_zip is not None:
            d = Path(td) / "shp"
            with zipfile.ZipFile(io.BytesIO(shp_zip)) as z:
                z.extractall(d)
            for n in layers:
                files = sorted((d / n).glob("*.shp")) if (d / n).exists() else []
                uids, cnt = [], 0
                for f in files:
                    df = pyogrio.read_dataframe(f, columns=["pcdn_uid"], read_geometry=False)
                    uids += df["pcdn_uid"].tolist()
                    cnt += len(df)
                check(n, "Shapefile", uids if files else None, cnt)
        if kml_files is not None:
            for n in layers:
                x = kml_files.get(f"{safe_name(n)}.kml", "")
                uids = re.findall(r'<Data name="pcdn_uid"><value>([^<]*)</value>', x)
                check(n, "KML", uids, x.count("<Placemark>"))
    out = pd.DataFrame(list(res.values()))
    out["Contrôle"] = ["✅ identique" if all(fmt_ok[n]) else "❌ ÉCART" for n in out["Couche"]]
    return out


# ------------------------------------------------------------------ archive
def build_bundle(parts: dict[str, bytes | str]) -> bytes:
    """parts : {chemin dans l'archive: contenu}"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in parts.items():
            z.writestr(path, content)
    return buf.getvalue()
