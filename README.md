# PCDN Corridor — fusion BRUTE par couche + rapport de traitement (Streamlit)

FTP/FTPS → lecture SHP/KML/KMZ → **fusion par couche (brute)** → diagnostic qualité → **rapport de guidage** → export **GPKG / SHP / KML**.

## Principe : données brutes, rien n'est supprimé
- Aucune entité retirée : pas de dédoublonnage, pas de rejet, pas de « format prioritaire » (SHP **et** KML d'une même couche/zone sont tous fusionnés), entités sans géométrie conservées.
- Aucune valeur modifiée : pas de nettoyage, de typage forcé, de correction de domaine ni de renumérotation d'ID. Le texte « null » reste « null ».
- Aucune colonne retirée : les colonnes hors schéma sont conservées. Seul le *nom* des colonnes du schéma est harmonisé (casse, noms tronqués à 10 car. par le Shapefile).
- Colonnes ajoutées (traçabilité) : `pcdn_uid`, `pcdn_zone`, `src_file`, `src_fmt`, `src_row`. Aucun statut QA n'est écrit dans les données.
- Fichiers jamais perdus en silence : illisibles, téléchargement échoué, exclus ou de couche inconnue (exportés sous `Non_classe__<nom>`) figurent dans le rapport.
- Après écriture, les exports sont **relus** : effectifs et `pcdn_uid` comparés à la fusion (section « Réconciliation »).
- Serveur FTP en **lecture seule** (LIST/RETR uniquement).
- Seules opérations techniques : reprojection en EPSG:4326 (tracée dans le rapport) et lecture de l'encodage.

## Lancer
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # hôte, utilisateur, mot de passe
streamlit run app.py
```
## Tests
```bash
python tests/test_pipeline.py   # garanties « brut » de bout en bout (données piégées)
python tests/test_app.py        # UI : lecture → rapport → export
python tests/test_ftp.py        # client FTP (pip install pyftpdlib)
```

## Onglets
1 Source · 2 Fusion (couverture couche × zone, fichiers non intégrés, carte) · 3 Diagnostic (lecture seule) · 4 Rapport · 5 Export.

## Contenu de l'archive d'export
`PCDN_brut_*.gpkg` · `shp/<Couche>/` + `correspondance_champs_shp.csv` · `kml/` · `rapport/` (HTML, XLSX de suivi, anomalies.csv, journal.txt) · `LISEZMOI.txt`.

## Le rapport (guide des équipes de traitement)
- **À lire d'abord** : constats clés (fichiers non intégrés, couches/zones manquantes, doublons, priorité 1).
- **Méthode recommandée** dans l'ordre : sources → géométries → identifiants → doublons → domaines → types → champs obligatoires → cohérence.
- **Plan de traitement** par couche : règle, nb d'entités, zones, exemples, action recommandée ; **répartition par zone**.
- **XLSX de suivi** : une ligne par anomalie (UID, fichier, ligne source, valeur actuelle, valeur proposée) + colonnes Statut / Correction / Traité par / Date.
- Doublons probables (même ID + même position), sources, réconciliation, complétude, limites Shapefile.

## Limites connues des formats
Shapefile : noms de champs ≤ 10 car., texte ≤ 254 car. (valeurs plus longues tronquées par le format → signalé ; GPKG/KML complets), espaces de début/fin retirés par le DBF. Altitudes Z : conservées si non nulles (Z=0 partout en KML = « sans altitude »).

## Contrôles (code → sens)
STRUCT_* fichiers/structure · GEOM_NULL/TYPE/INVALID/ZERO/BBOX/DUP · LINE_SHORT · ID_MISSING/FORMAT/PREFIX/ZONE/DUP/GAP · DUP_SRC · REQ_MISSING · DOM_INVALID/DOM_CASE · TYPE_INVALID/DECIMAL · RANGE · MULTI_EXCL · L_* cohérence métier · XL_GARE / XL_DIST inter-couches. Règles : `LOGIC` dans `pcdn/qa.py` ; libellés et actions : `RULES` dans `pcdn/report.py`.

## Organisation
`pcdn/config.py` schéma des 10 couches · `sources.py` FTP/découverte · `readers.py` lecture/fusion brute · `qa.py` diagnostic · `report.py` rapport · `exporters.py` exports + relecture. Le module `preprocess.py` (qui corrigeait/supprimait) a été retiré du flux.
