"""Lecture SHP / KML / KMZ **sans perte** et fusion brute par couche.

Principes (données brutes) :
  * aucune entité n'est supprimée (pas de dédoublonnage, pas de rejet, pas de « format prioritaire ») ;
  * aucune valeur n'est modifiée (pas de nettoyage, de typage forcé, de correction de domaine, de renumérotation d'ID) ;
  * aucune colonne n'est supprimée : les colonnes hors schéma sont conservées telles quelles ;
  * seules opérations : harmonisation du *nom* des colonnes du schéma (casse, noms tronqués à 10 car. par le Shapefile),
    reprojection en EPSG:4326 (nécessaire pour fusionner) et ajout de colonnes de traçabilité (META_COLS).
"""
from __future__ import annotations

import os
import re
import warnings
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import (GeometryCollection, LineString, MultiLineString, MultiPoint, MultiPolygon, Point,
                              Polygon)

try:  # protège contre les XML malveillants si defusedxml est installé
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover
    import xml.etree.ElementTree as ET

from .config import CRS_OUT, LAYERS, META_COLS, UNCLASSIFIED
from .sources import Resolved
from .utils import is_null, safe_name

os.environ.setdefault("SHAPE_RESTORE_SHX", "YES")
warnings.filterwarnings("ignore", category=FutureWarning)

STRUCT_COLS = ["layer", "zone", "rule", "severity", "field", "message", "fichier"]


# ------------------------------------------------------------------ KML
def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _coords(text: str, ctx: dict) -> list[tuple]:
    """Lit un bloc <coordinates>. Tolère les espaces autour des virgules ; compte les jetons illisibles."""
    pts = []
    for tok in re.sub(r"\s*,\s*", ",", (text or "").strip()).split():
        p = tok.split(",")
        try:
            xy = (float(p[0]), float(p[1]))
        except (ValueError, IndexError):
            ctx["bad"] += 1
            continue
        if len(p) >= 3 and p[2].strip():
            try:
                z = float(p[2])
                if z != 0:
                    ctx["z"] = True
                pts.append(xy + (z,))
                continue
            except ValueError:
                pass
        pts.append(xy)
    return pts


def _child_coords(el, ctx):
    ce = next((c for c in el.iter() if _ln(c.tag) == "coordinates"), None)
    return _coords(ce.text, ctx) if ce is not None else []


def _geoms_of(el, ctx) -> list:
    out = []
    for c in el:
        n = _ln(c.tag)
        if n == "Point":
            pts = _child_coords(c, ctx)
            if pts:
                out.append(Point(pts[0]))
            else:
                ctx["bad"] += 1
        elif n in ("LineString", "LinearRing"):
            pts = _child_coords(c, ctx)
            if len(pts) >= 2:
                out.append(LineString(pts))
            else:
                ctx["bad"] += 1
        elif n == "Polygon":
            rings = []
            for b in c:
                bn = _ln(b.tag)
                if bn in ("outerBoundaryIs", "innerBoundaryIs"):
                    pts = _child_coords(b, ctx)
                    if len(pts) >= 4:
                        rings.append((bn, pts))
                    else:
                        ctx["bad"] += 1
            outer = [p for k, p in rings if k == "outerBoundaryIs"]
            if outer:
                out.append(Polygon(outer[0], [p for k, p in rings if k == "innerBoundaryIs"]))
        elif n == "MultiGeometry":
            out += _geoms_of(c, ctx)
    return out


def _combine(geoms: list):
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    kinds = {g.geom_type for g in geoms}
    if kinds == {"Point"}:
        return MultiPoint(geoms)
    if kinds == {"LineString"}:
        return MultiLineString(geoms)
    if kinds == {"Polygon"}:
        return MultiPolygon(geoms)
    return GeometryCollection(geoms)


def _placemarks(root):
    """Placemarks avec le chemin du dossier KML (<Folder>) qui les contient."""
    out = []

    def walk(el, folder):
        for c in el:
            n = _ln(c.tag)
            if n == "Placemark":
                out.append((c, folder))
            elif n == "Folder":
                nm = next((x.text for x in c if _ln(x.tag) == "name" and x.text), None)
                walk(c, f"{folder}/{nm}" if folder and nm else (nm or folder))
            elif len(c):
                walk(c, folder)
    walk(root, None)
    return out


def read_kml(path) -> tuple[gpd.GeoDataFrame, dict]:
    p = Path(path)
    if p.suffix.lower() == ".kmz":
        with zipfile.ZipFile(p) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
            if not name:
                raise ValueError("KMZ sans fichier .kml")
            data = z.read(name)
    else:
        data = p.read_bytes()
    root = ET.fromstring(data)
    ctx = {"bad": 0, "z": False}
    rows, geoms = [], []
    for pm, folder in _placemarks(root):
        props, desc = {}, ""
        for c in pm:
            n = _ln(c.tag)
            if n == "name":
                props["name"] = c.text
            elif n == "description":
                desc = c.text or ""
        pairs = {}
        for el in pm.iter():
            n = _ln(el.tag)
            if n == "Data" and el.get("name"):
                v = next((x for x in el if _ln(x.tag) == "value"), None)
                pairs[el.get("name")] = v.text if v is not None else None
            elif n == "SimpleData" and el.get("name"):
                pairs[el.get("name")] = el.text
        if not pairs and desc:  # repli : tableau HTML dans <description> (export Google Earth / QGIS)
            for k, v in re.findall(r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", desc, re.S | re.I):
                pairs[re.sub(r"<[^>]+>", "", k).strip()] = re.sub(r"<[^>]+>", "", v).strip()
            if not pairs:  # description libre : conservée telle quelle, rien n'est perdu
                props["description"] = desc
        props.update(pairs)
        if folder:
            props["kml_dossier"] = folder
        rows.append(props)
        geoms.append(_combine(_geoms_of(pm, ctx)))
    if not ctx["z"]:  # Z = 0 partout : convention KML « sans altitude » → 2D
        geoms = [shapely.force_2d(g) if g is not None else None for g in geoms]
    gdf = gpd.GeoDataFrame(pd.DataFrame(rows), geometry=gpd.GeoSeries(geoms, crs="EPSG:4326"), crs="EPSG:4326")
    return gdf, {"encoding": "XML (UTF-8)", "coord_illisibles": ctx["bad"], "altitude": ctx["z"]}


# ------------------------------------------------------------------ SHP
_MOJIBAKE = re.compile("Ã.|\ufffd|â€")


def _moji_score(gdf: gpd.GeoDataFrame) -> int:
    n = 0
    for c in gdf.columns:
        if c == "geometry":
            continue
        s = gdf[c]
        if s.dtype == object or pd.api.types.is_string_dtype(s):
            n += int(s.map(lambda v: isinstance(v, str) and bool(_MOJIBAKE.search(v))).sum())
    n += sum(1 for c in gdf.columns if isinstance(c, str) and _MOJIBAKE.search(c))
    return n


def read_shp(path) -> tuple[gpd.GeoDataFrame, dict]:
    """Lit un Shapefile ; teste plusieurs encodages et garde celui qui n'a pas de caractères corrompus."""
    best, best_enc, best_score = None, None, None
    for enc in (None, "utf-8", "cp1252"):
        try:
            g = gpd.read_file(path, engine="pyogrio", **({"encoding": enc} if enc else {}))
        except Exception:  # noqa
            continue
        sc = _moji_score(g)
        if best is None or sc < best_score:
            best, best_enc, best_score = g, enc or "auto (.cpg)", sc
        if sc == 0:
            break
    if best is None:
        raise IOError("Shapefile illisible (vérifier .shp/.shx/.dbf)")
    gdf = best
    try:  # restaure les entiers que pandas a convertis en float à cause des valeurs vides
        info = pyogrio.read_info(path, encoding=None if best_enc.startswith("auto") else best_enc)
        for f, dt in zip(info["fields"], info["dtypes"]):
            if str(dt).startswith("int") and f in gdf.columns and pd.api.types.is_float_dtype(gdf[f]):
                v = gdf[f].dropna()
                if (v % 1 == 0).all():
                    gdf[f] = gdf[f].astype("Int64")
    except Exception:  # noqa
        pass
    return gdf, {"encoding": best_enc}


def read_any(r: Resolved) -> tuple[gpd.GeoDataFrame, dict]:
    gdf, meta = read_shp(r.local) if r.fmt == "shp" else read_kml(r.local)
    meta["crs_src"] = str(gdf.crs) if gdf.crs is not None else "absent"
    meta["no_crs"], meta["reprojected"] = gdf.crs is None, False
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_OUT)
    elif gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(CRS_OUT)
        meta["reprojected"] = True
    meta["geom_types"] = ", ".join(sorted(gdf.geom_type.dropna().unique())) or "—"
    return gdf, meta


# ------------------------------------------------------------------ alignement (noms de colonnes uniquement)
def align_columns(gdf: gpd.GeoDataFrame, layer: str, fmt: str = "shp"):
    """Harmonise le NOM des colonnes du schéma (casse, noms tronqués à 10 car. par le Shapefile).
    Ne supprime ni n'ajoute aucune colonne, ne touche à aucune valeur."""
    spec = LAYERS[layer]
    exp = [f.name for f in spec.fields]
    cols = [c for c in gdf.columns if c != "geometry"]
    lower = {c.lower(): c for c in cols}
    rename, issues, pending = {}, [], []
    for e in exp:
        if e in cols:
            continue
        if e.lower() in lower:
            rename[lower[e.lower()]] = e
        else:
            pending.append(e)
    used = set(rename) | set(exp)
    cand: dict[str, list[str]] = {}
    for e in pending:
        for c in cols:
            if c not in used and len(c) >= 8 and e.lower().startswith(c.lower()):
                cand.setdefault(c, []).append(e)
    for c, es in cand.items():
        if len(es) == 1 and es[0] not in rename.values():
            rename[c] = es[0]
        else:
            issues.append(("STRUCT_AMBIGUOUS", "WARNING", c,
                           f"Colonne tronquée « {c} » ambiguë entre {es} : conservée telle quelle (non mappée)."))
    for old, new in rename.items():
        issues.append(("STRUCT_RENAMED", "INFO", new, f"Colonne « {old} » reconnue comme « {new} »."))
    gdf = gdf.rename(columns=rename)
    present = set(gdf.columns)
    for e in exp:
        if e not in present:
            issues.append(("STRUCT_MISSING_COL", "INFO" if fmt in ("kml", "kmz") else "ERROR", e,
                           f"Colonne attendue absente de ce fichier : « {e} »."
                           + (" (un KML n'exporte pas les champs vides)" if fmt in ("kml", "kmz") else "")))
    extra = [c for c in gdf.columns if c not in exp and c != "geometry"]
    if extra:
        issues.append(("STRUCT_EXTRA_COL", "INFO", ", ".join(extra[:8]),
                       f"{len(extra)} colonne(s) hors schéma CONSERVÉE(S) dans l'export : {', '.join(extra[:8])}"))
    idf = spec.id_field
    if "name" in gdf.columns:
        idnull = gdf[idf].map(is_null) if idf in gdf.columns else pd.Series(True, index=gdf.index)
        usable = int((idnull & ~gdf["name"].map(is_null)).sum())
        if usable:
            issues.append(("STRUCT_ID_FROM_NAME", "INFO", idf,
                           f"{usable} entité(s) sans « {idf} » mais avec un <name> KML : le diagnostic utilise ce nom "
                           f"comme identifiant ; la donnée exportée reste inchangée (colonne « name »)."))
    return gdf, issues


def _zone_from_ids(gdf: gpd.GeoDataFrame, layer: str):
    idf = LAYERS[layer].id_field
    if idf not in gdf.columns:
        return None
    zs = gdf[idf].dropna().astype(str).str.upper().str.extract(r"^ZONE\s*[-_]?0*(\d+)", expand=False).dropna()
    if len(zs) and (zs.value_counts(normalize=True).iloc[0] >= 0.8):
        return f"ZONE{int(zs.value_counts().index[0])}"
    return None


# ------------------------------------------------------------------ ingestion
def _order(cols: list[str], layer: str | None) -> list[str]:
    schema = [f.name for f in LAYERS[layer].fields] if layer in LAYERS else []
    first = [c for c in schema if c in cols]
    meta = [c for c in META_COLS if c in cols]
    rest = [c for c in cols if c not in first and c not in meta and c != "geometry"]
    return first + rest + meta


INV_COLS = ["fichier", "chemin", "couche", "zone", "format", "statut", "entites", "crs_origine", "encodage",
            "types_geometrie", "remarques"]


def ingest(resolved: list[Resolved], log, failures: list | None = None, excluded: list | None = None):
    """-> ({couche: GeoDataFrame fusionné BRUT}, anomalies de structure, inventaire des fichiers).

    `failures` : [(chemin, message)] fichiers dont le téléchargement a échoué.
    `excluded` : [(chemin, raison)] fichiers écartés volontairement par l'utilisateur.
    Chaque fichier est lu ET conservé : plusieurs fichiers pour une même couche/zone sont tous fusionnés.
    """
    frames: dict[str, list] = {}
    struct: list = []
    inv: list = []

    def S(layer, zone, rule, sev, field, msg, fichier):
        struct.append((layer, zone, rule, sev, field, msg, fichier))

    for r in sorted(resolved, key=lambda r: (r.layer or "~", r.zone or "~", r.origin)):
        fname = Path(r.origin.split("!")[-1]).name
        lname = r.layer or f"{UNCLASSIFIED}__{safe_name(Path(fname).stem)}"
        row = {"fichier": fname, "chemin": r.origin, "couche": lname, "zone": r.zone, "format": r.fmt.upper(),
               "statut": "", "entites": 0, "crs_origine": "", "encodage": "", "types_geometrie": "", "remarques": ""}
        try:
            gdf, meta = read_any(r)
        except Exception as e:  # noqa
            log(f"❌ Lecture impossible {r.origin} ({r.fmt}) : {e}")
            S(lname, r.zone, "STRUCT_UNREADABLE", "ERROR", "", f"Fichier illisible : {e}", fname)
            row.update(statut="❌ Illisible", remarques=str(e)[:200])
            inv.append(row)
            continue
        notes = []
        if not r.layer:
            S(lname, r.zone, "STRUCT_NOT_CLASSIFIED", "WARNING", "",
              f"Couche non identifiée à partir du nom/chemin : données EXPORTÉES quand même sous « {lname} » (à rattacher).", fname)
            notes.append("couche non identifiée")
        if meta["no_crs"]:
            S(lname, r.zone, "STRUCT_NO_CRS", "WARNING", "", "Système de coordonnées absent (.prj manquant ?) : EPSG:4326 supposé.", fname)
            notes.append("CRS absent → 4326 supposé")
        if meta["reprojected"]:
            S(lname, r.zone, "STRUCT_REPROJECTED", "INFO", "", f"Reprojeté de {meta['crs_src'][:60]} vers EPSG:4326 pour la fusion.", fname)
        if meta.get("coord_illisibles"):
            S(lname, r.zone, "STRUCT_BAD_COORDS", "WARNING", "",
              f"{meta['coord_illisibles']} jeton(s) de coordonnées illisible(s) dans le KML (géométrie partielle ou absente).", fname)
            notes.append(f"{meta['coord_illisibles']} coordonnées illisibles")
        for m in r.missing:
            sev = "WARNING" if m.startswith((".shx", ".dbf")) else "INFO"
            S(lname, r.zone, "STRUCT_SIDECAR_MISSING", sev, "", f"Fichier associé manquant : {m}.", fname)
            notes.append(f"{m} manquant")
        if r.layer:
            gdf, iss = align_columns(gdf, r.layer, r.fmt)
            for c, s, f, m in iss:
                S(lname, r.zone, c, s, f, m, fname)
        zone = r.zone
        if not zone and r.layer:
            zone = _zone_from_ids(gdf, r.layer)
            if zone:
                S(lname, zone, "STRUCT_ZONE_FROM_ID", "INFO", "", f"Zone déduite des identifiants : {zone}.", fname)
        if not zone:
            S(lname, None, "STRUCT_ZONE_MISSING", "WARNING", "", "Zone non identifiée (nom/chemin/ID) : renseigner la zone à l'onglet 1.", fname)
        if len(gdf) == 0:
            S(lname, zone, "STRUCT_EMPTY_FILE", "WARNING", "", "Fichier lu mais vide (0 entité).", fname)
        gdf = gdf.copy()
        gdf["pcdn_zone"] = zone
        gdf["src_file"] = fname
        gdf["src_fmt"] = r.fmt
        gdf["src_row"] = np.arange(1, len(gdf) + 1)
        frames.setdefault(lname, []).append(gdf)
        row.update(statut="✔ Intégré", zone=zone, entites=len(gdf), crs_origine=meta["crs_src"][:40],
                   encodage=meta.get("encoding", ""), types_geometrie=meta["geom_types"], remarques="; ".join(notes))
        inv.append(row)
        log(f"✔ {lname} / {zone or 'zone ?'} : {len(gdf)} entités brutes ({r.fmt.upper()}, {fname})")

    for path, msg in failures or []:
        inv.append({"fichier": Path(path).name, "chemin": path, "couche": "", "zone": None, "format": "",
                    "statut": "❌ Téléchargement échoué", "entites": 0, "crs_origine": "", "encodage": "",
                    "types_geometrie": "", "remarques": str(msg)[:200]})
        S("", None, "STRUCT_DOWNLOAD_FAILED", "ERROR", "", f"Téléchargement échoué : {msg}", Path(path).name)
    for path, why in excluded or []:
        inv.append({"fichier": Path(path).name, "chemin": path, "couche": "", "zone": None, "format": "",
                    "statut": "⏸ Exclu par l'utilisateur", "entites": 0, "crs_origine": "", "encodage": "",
                    "types_geometrie": "", "remarques": why})

    inv_df = pd.DataFrame(inv, columns=INV_COLS)
    ok = inv_df[inv_df.statut.str.startswith("✔")]
    for (lay, zn), grp in ok.groupby(["couche", ok["zone"].fillna("?")]):
        if len(grp) > 1:  # plusieurs fichiers pour une même couche/zone : tous conservés, doublons signalés
            fm = "/".join(sorted(grp["format"].unique()))
            S(lay, None if zn == "?" else zn, "STRUCT_MULTIFILES", "INFO", "",
              f"{len(grp)} fichiers fusionnés pour {lay}/{zn} ({fm}) : {', '.join(grp['fichier'])}. "
              "Tous conservés ; les doublons éventuels sont signalés dans le rapport (DUP_SRC), pas supprimés.", "")

    merged = {}
    for lname, fl in frames.items():
        base = [f for f in fl if len(f)] or fl[:1]
        m = pd.concat(base, ignore_index=True) if len(base) > 1 else base[0].reset_index(drop=True)
        m = gpd.GeoDataFrame(m, geometry="geometry", crs=CRS_OUT)
        m["_rid"] = np.arange(len(m))
        pfx = LAYERS[lname].prefix if lname in LAYERS else "NC"
        m["pcdn_uid"] = [f"{pfx}-{i + 1:05d}" for i in range(len(m))]
        merged[lname] = m[_order([c for c in m.columns if c not in ("_rid", "geometry")], lname) + ["_rid", "geometry"]]
    sdf = pd.DataFrame(struct, columns=STRUCT_COLS)
    return merged, sdf, inv_df
