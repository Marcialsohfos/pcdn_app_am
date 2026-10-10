"""PCDN Corridor — récupération FTP, fusion BRUTE par couche, diagnostic, rapport de traitement, export GPKG / SHP / KML."""
from __future__ import annotations

import io
import tempfile
import uuid
import warnings
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st
import streamlit.components.v1 as components

from pcdn import exporters, qa, readers, report
from pcdn.config import LAYERS, META_COLS, check_spec, load_domains
from pcdn.sources import FtpClient, build_sources, fetch

warnings.filterwarnings("ignore")
st.set_page_config(page_title="PCDN · Fusion brute & rapport", page_icon="🚆", layout="wide")
DOMAINS = load_domains()
SS = st.session_state
for k, v in {"srcs": None, "root": None, "raw": None, "struct": None, "inventory": None, "issues": None,
             "log": [], "ftp_cfg": None, "failures": [], "excluded": [], "expected": "", "exports": {}, "verify": None,
             "bundle": None}.items():
    SS.setdefault(k, v)


def log(msg: str):
    SS.log.append(msg)


def secret(key, default=""):
    try:
        return st.secrets["ftp"].get(key, default)
    except Exception:  # noqa
        return default


def expected_zones() -> list[str]:
    return [x.strip().upper() for x in SS.expected.split(",") if x.strip()]


def tables():
    return report.build_tables(SS.raw, SS.issues, SS.inventory, qa.completeness(SS.raw), SS.log, expected_zones(), SS.verify)


def embed_html(doc: str, height: int = 800):
    """Aperçu du rapport (contenu intégralement échappé). st.iframe sur Streamlit récent, components.html sinon."""
    if hasattr(st, "iframe"):
        st.iframe(doc, height=height)
    else:  # pragma: no cover
        components.html(doc, height=height, scrolling=True)


def need_data() -> bool:
    if SS.raw is None:
        st.info("Récupérez d'abord les données (onglet 1).")
        return True
    return False


# ================================================================== SIDEBAR
with st.sidebar:
    st.title("🚆 PCDN Corridor")
    st.caption("FTP → fusion brute → diagnostic → rapport → export")
    st.success("**Mode brut** : aucune entité supprimée, aucune valeur corrigée. Les corrections se font en aval, guidées par le rapport.")
    probs = check_spec(DOMAINS)
    if probs:
        st.error("Dictionnaires JSON incohérents :\n\n" + "\n".join(f"- {p}" for p in probs[:6]))
    dup_tol = st.number_input("Seuil de doublon de points (m)", 0.5, 100.0, 3.0, 0.5,
                              help="Sert au diagnostic uniquement : les points proches sont signalés, jamais supprimés.")
    st.divider()
    st.markdown("**Convention d'ID** : `ZONE<n>` + 2 premières lettres de la couche + n°")
    st.dataframe(pd.DataFrame({"Couche": list(LAYERS), "Préfixe": [l.prefix for l in LAYERS.values()],
                               "Exemple": [f"ZONE1{l.prefix}1" for l in LAYERS.values()]}), hide_index=True, width="stretch")
    if st.button("♻ Réinitialiser la session"):
        st.session_state.clear()
        st.rerun()

tabs = st.tabs(["1 · Source", "2 · Fusion", "3 · Diagnostic", "4 · Rapport", "5 · Export"])

# ================================================================== 1. SOURCE
with tabs[0]:
    mode = st.radio("Origine des données", ["Serveur FTP", "Téléversement manuel (secours)"], horizontal=True)
    workdir = Path(tempfile.gettempdir()) / "pcdn_work"

    if mode == "Serveur FTP":
        st.caption("🔒 Lecture seule : l'application liste et télécharge (LIST/RETR). Elle n'écrit, ne déplace et ne supprime rien sur le serveur des data collectors.")
        c1, c2, c3, c4 = st.columns([3, 1, 2, 2])
        host = c1.text_input("Hôte", secret("host"))
        port = c2.number_input("Port", 1, 65535, int(secret("port", 21)))
        user = c3.text_input("Utilisateur", secret("user"))
        pwd = c4.text_input("Mot de passe", secret("password"), type="password")
        c5, c6, c7, c8 = st.columns([3, 1, 2, 2])
        root = c5.text_input("Dossier racine", secret("root", "/"))
        depth = c6.number_input("Profondeur", 1, 10, 5)
        tls = c7.checkbox("Utiliser FTPS (chiffré)", True)
        plain = c8.checkbox("Autoriser repli FTP non chiffré", False, help="À éviter : identifiants et données circulent en clair.")
        if not secret("host"):
            st.info("Astuce : placez les identifiants dans `.streamlit/secrets.toml` (voir `secrets.toml.example`) plutôt que de les saisir ou de les écrire dans le code.")
        if st.button("🔎 Scanner le serveur", type="primary", disabled=not (host and user)):
            try:
                with st.spinner("Connexion et exploration…"), FtpClient(host, user, pwd, port, tls, plain) as cl:
                    entries = cl.walk(root, depth)
                    SS.ftp_cfg = dict(host=host, user=user, pwd=pwd, port=port, tls=tls, allow_plain=plain)
                    st.success(f"Connecté ({cl.mode}) — {len(entries)} fichiers vus.")
                SS.srcs, SS.root = build_sources(entries), None
            except Exception as e:  # noqa
                st.error(f"Échec : {e}")
    else:
        ups = st.file_uploader("Shapefiles (.shp + .shx + .dbf + .prj + .cpg), KML, KMZ ou ZIP", accept_multiple_files=True,
                               type=["shp", "shx", "dbf", "prj", "cpg", "kml", "kmz", "zip"])
        if st.button("📂 Analyser les fichiers", type="primary", disabled=not ups):
            rootp = workdir / f"up_{uuid.uuid4().hex[:8]}"
            rootp.mkdir(parents=True, exist_ok=True)
            entries = []
            for u in ups:
                (rootp / u.name).write_bytes(u.getbuffer())
                entries.append((u.name, u.size))
            SS.srcs, SS.root, SS.ftp_cfg = build_sources(entries), rootp, None

    if SS.srcs is not None:
        st.subheader("Fichiers détectés")
        st.caption("Vérifiez / corrigez la couche et la zone détectées (nom ou chemin). Un fichier sans couche est quand même "
                   "récupéré et exporté dans une couche « Non_classe__… » : rien n'est écarté en silence.")
        df = pd.DataFrame([{"inclure": s.include, "couche": s.layer, "zone": s.zone, "format": s.fmt,
                            "taille_ko": round(s.size / 1024, 1), "chemin": s.path} for s in SS.srcs])
        if df.empty:
            st.warning("Aucun fichier .shp / .kml / .kmz / .zip trouvé.")
        else:
            ed = st.data_editor(df, hide_index=True, width="stretch", disabled=["format", "taille_ko", "chemin"],
                                column_config={"couche": st.column_config.SelectboxColumn(options=list(LAYERS)),
                                               "zone": st.column_config.TextColumn(help="Ex. ZONE1")})
            miss = ed[ed.inclure & (ed.couche.isna() | ed.zone.isna() | (ed.zone == ""))]
            if len(miss):
                st.warning(f"{len(miss)} fichier(s) sans couche ou zone : la zone sera déduite des ID si possible ; "
                           "sans couche, les données sont exportées dans « Non_classe__… » et signalées dans le rapport.")
            if st.button("⬇ Récupérer et lire les données", type="primary"):
                for s, (_, r) in zip(SS.srcs, ed.iterrows()):
                    s.include = bool(r.inclure)
                    s.layer = r.couche if isinstance(r.couche, str) and r.couche else None
                    s.zone = str(r.zone).upper().replace(" ", "") if isinstance(r.zone, str) and r.zone.strip() else None
                SS.log, SS.failures = [], []
                SS.excluded = [(s.path, "décoché à l'étape 1") for s in SS.srcs if not s.include]
                resolved, bar = [], st.progress(0.0, "Téléchargement…")
                todo = [s for s in SS.srcs if s.include]
                try:
                    cl = FtpClient(**SS.ftp_cfg).connect() if SS.ftp_cfg else None
                    for i, s in enumerate(todo, 1):
                        bar.progress(i / max(len(todo), 1), f"{s.path}")
                        try:
                            resolved += fetch(s, workdir / "dl", cl, SS.root)
                        except Exception as e:  # noqa
                            log(f"❌ {s.path}: {e}")
                            SS.failures.append((s.path, e))
                    if cl:
                        cl.close()
                    SS.raw, SS.struct, SS.inventory = readers.ingest(resolved, log, SS.failures, SS.excluded)
                    SS.issues = qa.run_all_qa(SS.raw, DOMAINS, SS.struct, dup_tol)
                    SS.exports, SS.verify, SS.bundle = {}, None, None
                    bar.empty()
                    st.success(f"{sum(len(g) for g in SS.raw.values())} entités brutes lues dans {len(SS.raw)} couche(s). "
                               "Passez à l'onglet « Fusion ».")
                    bad = SS.inventory[~SS.inventory.statut.str.startswith(("✔", "⏸"))]
                    if len(bad):
                        st.error(f"{len(bad)} fichier(s) NON intégrés — voir l'onglet « Fusion » et le rapport.")
                except Exception as e:  # noqa
                    bar.empty()
                    st.error(f"Erreur : {e}")
    if SS.log:
        with st.expander("Journal", expanded=False):
            st.code("\n".join(SS.log))


# ================================================================== carte
def show_map(layers: dict, issues: pd.DataFrame, key: str):
    name = st.selectbox("Couche à afficher", list(layers), key=key)
    g = layers[name]
    g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
    n_nogeom = len(layers[name]) - len(g)
    if n_nogeom:
        st.caption(f"{n_nogeom} entité(s) sans géométrie : conservées dans l'export, non affichables sur la carte.")
    if g.empty:
        st.info("Aucune géométrie.")
        return
    mode = st.radio("Colorer par", ["Diagnostic", "Zone"], horizontal=True, key=key + "_m")
    palette = [[31, 119, 180, 210], [255, 127, 14, 210], [44, 160, 44, 210], [148, 103, 189, 210], [140, 86, 75, 210], [23, 190, 207, 210]]
    if mode == "Diagnostic":
        col = {"OK": [60, 180, 90, 200], "WARNING": [245, 155, 30, 220], "ERROR": [220, 60, 60, 230]}
        g["statut"] = g["_rid"].map(qa.status_series(issues, name)).fillna("OK")
        g["color"] = g["statut"].map(col)
    else:
        zs = sorted(g["pcdn_zone"].dropna().unique())
        g["statut"] = g["pcdn_zone"].fillna("(zone ?)")
        g["color"] = g["pcdn_zone"].map({z: palette[i % len(palette)] for i, z in enumerate(zs)}).map(lambda c: c if isinstance(c, list) else [120, 120, 120, 200])
    idf = LAYERS[name].id_field if name in LAYERS else "pcdn_uid"
    g["etiquette"] = g[idf].where(g[idf].notna(), g["pcdn_uid"]).astype(str) if idf in g.columns else g["pcdn_uid"]
    layer = pdk.Layer("GeoJsonLayer", data=g[["etiquette", "statut", "color", "geometry"]].__geo_interface__,
                      get_fill_color="properties.color", get_line_color="properties.color",
                      point_radius_min_pixels=6, line_width_min_pixels=3, pickable=True)
    xmin, ymin, xmax, ymax = g.total_bounds
    view = pdk.ViewState(latitude=(ymin + ymax) / 2, longitude=(xmin + xmax) / 2, zoom=6.5 if xmax - xmin > 1 else 10)
    st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view, map_style=None, tooltip={"text": "{etiquette}\n{statut}"}))


# ================================================================== 2. FUSION
with tabs[1]:
    if not need_data():
        data = SS.raw
        st.subheader("Couches fusionnées (brut)")
        m1, m2, m3, m4 = st.columns(4)
        inv = SS.inventory
        m1.metric("Entités brutes", sum(len(g) for g in data.values()))
        m2.metric("Couches", len(data))
        m3.metric("Fichiers intégrés", int(inv.statut.str.startswith("✔").sum()))
        bad = inv[~inv.statut.str.startswith(("✔", "⏸"))]
        m4.metric("Fichiers NON intégrés", len(bad), delta=None if bad.empty else "à traiter", delta_color="inverse")
        if len(bad):
            st.error("Fichiers non intégrés (leurs données ne sont PAS dans l'export) : " + ", ".join(bad["fichier"].head(8)))
        SS.expected = st.text_input("Zones attendues (facultatif, ex. ZONE1,ZONE2,ZONE3,ZONE4)", SS.expected)
        cov = report.coverage_table(data, expected_zones())
        zc = [c for c in cov.columns if c not in ("Couche", "Total", "(zone ?)")]
        st.caption("Matrice de couverture couche × zone — les 0 signalent des levés manquants.")
        st.dataframe(cov.style.map(lambda v: "background-color:#fde2e2" if v == 0 else "", subset=zc), hide_index=True, width="stretch")
        missing_layers = [n for n in LAYERS if n not in data]
        if missing_layers:
            st.warning("Couches non reçues : " + ", ".join(missing_layers))
        with st.expander(f"Fichiers sources ({len(inv)})", expanded=bool(len(bad))):
            st.dataframe(inv.drop(columns=["chemin"]), hide_index=True, width="stretch")
        show_map(data, SS.issues, "map_fusion")

# ================================================================== 3. DIAGNOSTIC
with tabs[2]:
    if not need_data():
        issues = SS.issues
        if st.button("🔄 Relancer le diagnostic"):
            SS.issues = qa.run_all_qa(SS.raw, DOMAINS, SS.struct, dup_tol)
            SS.verify, SS.bundle = None, None
            st.rerun()
        st.subheader("Diagnostic qualité (lecture seule)")
        st.caption("Les anomalies sont signalées pour guider le traitement ; aucune donnée n'est modifiée ni retirée.")
        summ = qa.summarize(SS.raw, issues)
        tot, nerr, nwar = int(summ["Entités"].sum()), int(summ["Erreurs (entités)"].sum()), int(summ["Avertissements seuls (entités)"].sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Entités", tot)
        m2.metric("En erreur (P1)", nerr)
        m3.metric("À vérifier (P2)", nwar)
        m4.metric("Sans anomalie", f"{100 * (tot - nerr - nwar) / tot:.1f} %" if tot else "—")
        st.dataframe(summ, hide_index=True, width="stretch")

        st.markdown("#### Explorateur d'anomalies")
        f1, f2, f3, f4 = st.columns(4)
        sel_l = f1.multiselect("Couche", sorted(issues.layer.unique()))
        sel_s = f2.multiselect("Sévérité", ["ERROR", "WARNING", "INFO"], ["ERROR", "WARNING"])
        sel_r = f3.multiselect("Règle", sorted(issues.rule.unique()))
        sel_z = f4.multiselect("Zone", sorted(issues.zone.dropna().unique()))
        v = issues.copy()
        for col, sel in (("layer", sel_l), ("severity", sel_s), ("rule", sel_r), ("zone", sel_z)):
            if sel:
                v = v[v[col].isin(sel)]
        st.caption(f"{len(v)} anomalie(s) affichée(s)")
        show = v.drop(columns="_rid").assign(severity=v.severity.astype(str))
        st.dataframe(show, hide_index=True, width="stretch")
        c1, c2 = st.columns(2)
        c1.download_button("⬇ Anomalies filtrées (CSV)", show.to_csv(index=False, encoding="utf-8-sig"), "anomalies_filtrees.csv", "text/csv")
        top = issues[issues.severity != "INFO"].groupby("rule").size().sort_values(ascending=False).head(15)
        if len(top):
            c2.bar_chart(top, horizontal=True)
        with st.expander("Complétude des champs (% renseignés)"):
            comp = qa.completeness(SS.raw)
            if len(comp):
                st.dataframe(comp.pivot(index="Champ", columns="Couche", values="Renseigné (%)").dropna(how="all"), width="stretch")

# ================================================================== 4. RAPPORT
with tabs[3]:
    if not need_data():
        st.subheader("Rapport de guidage pour les équipes de traitement")
        st.caption("Quoi corriger, où (zone · fichier · ligne · UID), pourquoi et comment — dans l'ordre recommandé. "
                   "Le contrôle de relecture des exports est ajouté dès que les exports ont été générés (onglet 5).")
        T = tables()
        icon = {"err": st.error, "warn": st.warning, "ok": st.success, "info": st.info}
        for lv, tx in T["points"]:
            icon[lv](tx)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        html_doc = report.to_html(T)
        c1, c2, c3 = st.columns(3)
        c1.download_button("⬇ Rapport (HTML)", html_doc, f"Rapport_traitement_PCDN_{stamp}.html", "text/html", type="primary")
        c2.download_button("⬇ Classeur de suivi (XLSX)", report.to_xlsx(T), f"Rapport_traitement_PCDN_{stamp}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        c3.download_button("⬇ Anomalies (CSV)", T["anomalies"].to_csv(index=False, encoding="utf-8-sig"), f"anomalies_{stamp}.csv", "text/csv")
        st.markdown("#### Plan de traitement")
        st.dataframe(T["plan"][T["plan"]["Priorité"] != report.PRIO["INFO"]], hide_index=True, width="stretch")
        with st.expander("Aperçu du rapport imprimable", expanded=False):
            embed_html(html_doc, 800)

# ================================================================== 5. EXPORT
with tabs[4]:
    if not need_data():
        st.subheader("Export des données brutes")
        st.caption("Chaque couche est écrite telle que fusionnée : toutes les entités (même sans géométrie), toutes les colonnes sources, "
                   f"valeurs inchangées. Seules colonnes ajoutées : {', '.join(f'`{c}`' for c in META_COLS)}. "
                   "Aucun statut QA ni colonne calculée n'est injecté.")
        fmts = st.multiselect("Formats", ["GeoPackage", "Shapefile", "KML"], ["GeoPackage", "Shapefile", "KML"])
        with_tab = st.checkbox("Ajouter la table des anomalies au GeoPackage (table non spatiale `_rapport_anomalies`, jointe par `pcdn_uid`)", False)
        st.caption("Shapefile : noms de champs limités à 10 caractères (table de correspondance fournie) et texte à 254 caractères. "
                   "Le GeoPackage est le format de référence.")
        notes = exporters.shp_notes(SS.raw) if "Shapefile" in fmts else []
        if notes:
            with st.expander(f"⚠ {len(notes)} limite(s) du format Shapefile pour ce jeu de données"):
                st.write("\n".join(f"- {n}" for n in notes[:40]))
        if st.button("📦 Générer les exports et le rapport", type="primary", disabled=not fmts):
            with st.spinner("Écriture et relecture des fichiers…"):
                gp = exporters.to_gpkg(SS.raw, {"_rapport_anomalies": SS.issues.drop(columns="_rid").assign(severity=SS.issues.severity.astype(str))}
                                       if with_tab else None) if "GeoPackage" in fmts else None
                shp = exporters.to_shp_zip(SS.raw) if "Shapefile" in fmts else None
                kml = exporters.to_kml_files(SS.raw) if "KML" in fmts else None
                SS.verify = exporters.verify_exports(SS.raw, gp, shp, kml)
                T = tables()
                stamp = datetime.now().strftime("%Y%m%d_%H%M")
                xlsx, html_doc = report.to_xlsx(T), report.to_html(T)
                csv = T["anomalies"].to_csv(index=False, encoding="utf-8-sig")
                readme = (
                    "EXPORT PCDN — DONNÉES BRUTES\n============================\n"
                    f"Généré le {T['generated']} par l'application PCDN {report.APP_VERSION}.\n\n"
                    "Contenu\n  - PCDN_brut_*.gpkg : une table par couche (format de référence)\n"
                    "  - shp/<Couche>/ : Shapefiles (noms de champs ≤ 10 car. : voir shp/correspondance_champs_shp.csv)\n"
                    "  - kml/ : un KML par couche + PCDN_toutes_couches.kml (attributs en ExtendedData)\n"
                    "  - rapport/ : rapport HTML, classeur XLSX de suivi, anomalies.csv, journal.txt\n\n"
                    "Garanties\n  - Toutes les entités des fichiers intégrés sont présentes (y compris sans géométrie) ; aucune n'est supprimée.\n"
                    "  - Aucune valeur n'est corrigée ; aucun identifiant n'est renuméroté ; les colonnes hors schéma sont conservées.\n"
                    f"  - Colonnes ajoutées (traçabilité) : {', '.join(META_COLS)}.\n"
                    "  - Les fichiers exportés ont été RELUS : effectifs et pcdn_uid comparés à la fusion (voir rapport, section Réconciliation).\n"
                    "  - Les corrections sont à faire en aval, guidées par le rapport (liaison par pcdn_uid, fichier source et ligne).\n")
                parts = {"LISEZMOI.txt": readme, "rapport/Rapport_traitement_PCDN.html": html_doc, "rapport/Rapport_traitement_PCDN.xlsx": xlsx,
                         "rapport/anomalies.csv": csv, "rapport/journal.txt": "\n".join(SS.log)}
                if gp:
                    parts[f"PCDN_brut_{stamp}.gpkg"] = gp
                if shp:
                    with zipfile.ZipFile(io.BytesIO(shp)) as z:
                        parts.update({f"shp/{n}": z.read(n) for n in z.namelist()})
                if kml:
                    parts.update({f"kml/{n}": x for n, x in kml.items()})
                SS.bundle = exporters.build_bundle(parts)
                SS.exports = {"GeoPackage": gp, "Shapefile": shp, "KML": kml["PCDN_toutes_couches.kml"] if kml else None,
                              "html": html_doc, "xlsx": xlsx, "stamp": stamp}
        if SS.bundle:
            v = SS.verify
            if (v["Contrôle"] == "✅ identique").all():
                st.success("Contrôle de réconciliation : les fichiers exportés, relus, contiennent exactement les mêmes entités que la fusion.")
            else:
                st.error("ÉCART détecté entre la fusion et les exports relus — ne pas utiliser avant vérification.")
            st.dataframe(v, hide_index=True, width="stretch")
            e = SS.exports
            st.download_button("⬇ Archive complète (ZIP : GPKG + SHP + KML + rapport)", SS.bundle, f"PCDN_brut_{e['stamp']}.zip",
                               "application/zip", type="primary")
            c1, c2, c3, c4 = st.columns(4)
            if e["GeoPackage"]:
                c1.download_button("GeoPackage (.gpkg)", e["GeoPackage"], f"PCDN_brut_{e['stamp']}.gpkg", "application/geopackage+sqlite3")
            if e["Shapefile"]:
                c2.download_button("Shapefiles (ZIP)", e["Shapefile"], "PCDN_brut_shp.zip", "application/zip")
            if e["KML"]:
                c3.download_button("KML (toutes couches)", e["KML"], "PCDN_brut.kml", "application/vnd.google-earth.kml+xml")
            c4.download_button("Rapport (HTML)", e["html"], "Rapport_traitement_PCDN.html", "text/html")
