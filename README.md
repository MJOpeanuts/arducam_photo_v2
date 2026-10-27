# Arducam Photo V2

Application locale Windows 11 x64 / PySide6 pour photographier exclusivement
des **cartes électroniques, composants électroniques et conditionnements de
composants** avec l’Arducam B0494C. Elle conserve l’acquisition existante et
comporte trois onglets intégrés : **Preview**, **Banques**, **Capture**.
Pas de navigateur, de serveur ni d’onglet Traitement.

## Mise à jour et lancement sous Windows

Prérequis : Python **3.12 x64**, pilote Arducam/UVC, caméra USB connectée.
Dans PowerShell, depuis la racine du dépôt :

```powershell
git pull --ff-only
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m arducam_photo_v2.app
```

La création du venv est également utilisable à la première installation.
Les dépendances et la commande de lancement restent celles du projet existant.
Ne pas installer simultanément `opencv-python` et `opencv-python-headless` :
la dépendance Windows est `opencv-python`, avec DirectShow et Media Foundation.

Pour construire l’exécutable portable depuis les sources :

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1
.\dist\ArducamPhotoV2.exe
```

Le fichier de correction **local** `arducam_108mp.json` est recherché à la
racine du dépôt pour le lancement depuis les sources en mode éditable ; pour
l’exécutable portable, à côté de `ArducamPhotoV2.exe`. La recherche ne dépend
pas du répertoire courant. Ce fichier n’est ni suivi par Git ni inclus dans
le paquet ou l’exécutable : sa présence locale n’accorde aucun droit de
redistribution. L’obtenir séparément auprès d’une source autorisée.
Sans correction valide, les modes couleur restent utilisables et le RAW
correctement acquis reste sauvegardé ; seul son rendu couleur échoue.

## Preview : cadrage et focus

Sélectionner explicitement la caméra ; aucune caméra n’est choisie silencieusement.
Le Preview conserve un flux technique **couleur 1280 × 720**, indépendamment du
mode photo des banques. Il n’a pas de sélecteur permanent de mode photo.

Le focus manuel est une **consigne entière de 0 à 1023**, sans unité physique.
Le curseur s’applique au relâchement ; « Saisir » ouvre un pavé numérique et
applique la valeur à sa validation. Les boutons − et + avancent d’un pas,
avec répétition après 450 ms puis toutes les 100 ms pendant le maintien.
Le propriétaire caméra ne garde que la commande en attente la plus récente.

L’interface distingue demande, acceptation, relecture éventuelle et erreur.
**Une commande acceptée ou relue n’est jamais une preuve de netteté.**
Les rapports détaillés restent dans Diagnostic. Aucun réglage opérateur
d’exposition, gain, balance des blancs, luminosité, contraste ou saturation.

« Créer une banque avec ce focus » ouvre un formulaire court : nom, focus
repris, mode photo. Seul « Enregistrer » crée la banque ; aucune photo n’est
prise et le Preview reste en 720p. Une consigne en attente, périmée ou refusée
ne peut pas être sauvegardée. Une relecture absente ou différente exige une
confirmation explicite de la limite de vérification.

## Banques : configurations sauvegardées

Chaque banque possède un identifiant indépendant, un nom, un mode photo,
un focus, un contexte caméra et une révision.

| Mode | Photo produite |
|---|---|
| `color_720p` | Couleur 1280 × 720 |
| `color_4k` | Couleur 3840 × 2160 |
| `color_12mp` | Couleur 4000 × 3000 |
| `raw_108mp` | Bayer reconstruit 12000 × 9000 et rendu couleur automatique |

« Nom et mode » modifie une banque ; « Dupliquer » copie le focus et la
caméra dans une banque avec **un nouvel identifiant**, puis permet de choisir
le nom et le mode. On peut ainsi préparer une banque RAW depuis une banque
12 MP. Cela ne garantit pas une netteté identique entre modes.

« Régler le focus dans Preview » charge la banque, l’identifie à l’écran et
applique sa consigne dans le Preview technique fixe. La consigne de travail
reste distincte de la banque sauvegardée. « Sauvegarder le focus » enregistre
explicitement la modification, **sans changer son mode photo**. Avant de
quitter avec des modifications, choisir Sauvegarder, Abandonner ou Rester.
Chaque modification sauvegardée incrémente la révision.

La suppression demande confirmation, actualise les cases de Capture et ne
touche jamais aux anciennes photos, RAW ou JSON.

## Capture : une photo par banque cochée

Cocher une ou plusieurs banques. L’ordre affiché, trié par nom puis identifiant
pour départager les noms identiques, est l’ordre stable d’exécution.
« N banques sélectionnées = N photos ». Sans sélection, Capturer est désactivé.

Les champs facultatifs « ID de l’objet » et « Description de l’objet »
décrivent l’objet photographié, pas une banque. L’ID est du texte : `000042`,
les caractères Unicode et les espaces significatifs sont conservés.
Un champ vide ou seulement composé d’espaces devient `null` dans les JSON.
Ces champs ne construisent jamais les chemins de fichiers.
Ils restent visibles après la séquence ; « Effacer les informations objet »
est la seule action qui les vide.

Au déclenchement sont figés l’ordre, les banques complètes et leurs révisions,
ainsi que les informations objet. La configuration, la navigation et les
déclenchements supplémentaires sont bloqués pendant la séquence.
La progression indique « Photo 2 sur 3 — Banque [nom] ».

Pour chaque banque, l’application résout sa caméra, ouvre le backend prévu,
configure le mode, applique le focus, attend **6 lectures conformes consécutives**
(30 tentatives maximum), valide et copie le buffer, puis libère la caméra
**avant** l’archivage et le rendu. Aucun accès concurrent, aucun flux continu
au repos dans Capture, aucun retour ou redémarrage automatique du Preview.

Après une erreur de prise avec libération correcte, la banque suivante est
exécutée sans réessayer la prise échouée. En cas de défaut de libération,
d’accès occupé ou d’état matériel incertain, les suivantes sont marquées
« Non exécutée ». Un défaut matériel impose le redémarrage de l’application.
Les résultats précédents restent disponibles.

Capture conserve les résultats sous forme de vignettes sélectionnables,
avec banque, mode et statut, ainsi qu’un bilan réussites/échecs/non exécutées.
La photo sélectionnée s’affiche en grand sans déformation ; « Photo » ouvre
son fichier, « Ouvrir » son dossier. Un RAW sans rendu reste accessible dans
son dossier. Si une photo sauvegardée ne peut pas être décodée, le message est
« Photo enregistrée, affichage indisponible ».
Décodage et réduction sont effectués hors du thread graphique ; seuls les
aperçus autonomes réduits et les vignettes sont conservés pour l’affichage,
pas tous les rendus pleine résolution.

## Originaux et métadonnées

Chaque photo possède son UUID technique unique, distinct de l’ID objet et
de l’UUID commun de séquence. Le format existant **PNG sans perte** est
conservé. Dans un dossier par capture :

```text
<identifiant_capture>/
    <identifiant_capture>.png
    <identifiant_capture>.json
    <identifiant_capture>.raw   # uniquement pour raw_108mp
```

Le RAW et la photo couleur sont des fichiers distincts partageant le même
identifiant. Aucun dossier ou fichier de capture existant n’est écrasé.
Toutes les références du manifeste correspondent aux fichiers effectivement
produits ; si le rendu RAW échoue, le PNG n’est pas annoncé comme produit.

Le JSON, schéma **2**, contient : capture, séquence et position, date UTC avec
fuseau, objet facultatif, copie figée de la banque (identifiant, nom, révision,
mode, focus, caméra), configuration demandée, caméra/backend effectivement
résolus, acceptation et relecture du focus, forme/type/format/octets reçus,
stabilisation, libération, erreurs, fichiers et caractéristiques
(dimensions, taille, format et SHA-256), durées et recette de rendu.
Une banque renommée, modifiée ou supprimée ne modifie jamais ces archives.

Réglages : `%LOCALAPPDATA%\ArducamPhotoV2\banks.sqlite3`.
Photos par défaut : `%USERPROFILE%\Pictures\ArducamPhotoV2`, modifiable via
« Dossier… ». Le schéma SQLite existant est conservé ; aucune migration ou
remise à zéro n’est nécessaire pour cette évolution. Comme auparavant, une
base de réglages de développement incompatible ou corrompue est réinitialisée
avec notification, **uniquement dans le stockage de configuration V2**.
Les dossiers photos ne sont jamais concernés.

## Invariants RAW 108 MP et backends

Sous Windows : Preview et photos couleur sur **DirectShow (`DSHOW`)** ;
RAW sur **Media Foundation (`MSMF`)**, explicitement. Sous Linux : V4L2.
Le périphérique est retrouvé par chemin matériel ou, à défaut, nom unique ;
l’index seul n’est pas une identité. Chaque banque résout sa propre caméra,
sans exiger un flux Preview préalable. Backend indisponible, caméra absente
ou ambiguë : erreur explicite, **aucun repli de backend ou résolution**.

RAW : transport **6000 × 9000**, `CAP_PROP_CONVERT_RGB=0`,
**108 000 000 octets uint8 exactement**, reconstruction **9000 lignes ×
12000 colonnes**. Le tableau `(9000, 6000, 3)` de 162 000 000 octets est rejeté :
ni troncature ni extraction de canal.

Le Bayer est écrit avant tout traitement. Le rendu automatique conserve la
recette existante : soustraction saturante du niveau noir 16, dématriçage
BayerGR2RGB, CCM constructeur interpolée à 4000 K, saturation fixe 1.0.
Les coefficients sont lus depuis `arducam_108mp.json`, jamais recopiés en dur.
La recette, l’emplacement et l’empreinte SHA-256 du fichier utilisé sont
consignés. Le rendu reste **12000 × 9000 sans upscale**.
Une correction absente/invalide ou une erreur de rendu produit le statut
`render_error_original_preserved`, pas un échec d’acquisition : le RAW reste
intact et la séquence peut continuer si la caméra est correctement libérée.

## Identité visuelle et contrôles

Les logos locaux `logo_ref.png`, `poweredby.png`, `icon_ref.png`, l’icône
Windows et les ressources Lucide restent embarqués à l’identique.
L’icône **squirrel** est conservée sur Capturer ; licence ISC dans
`resources/LICENSE-lucide.txt`.
Textes courants à 15 px, boutons autour de 16 px, cibles d’au moins 48 × 48 px
logiques, curseur tactile de 56 px. Les diagnostics ne repoussent pas les
commandes essentielles ; leurs détails sont dans un panneau ou dialogue dédié.

Tests automatiques, Linux / Qt offscreen :

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
```

Sous Windows :

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:\QT_QPA_PLATFORM
```

Les tests couvrent les banques, le focus et sa confirmation, les séquences
figées, l’objet vide/avec zéros initiaux/Unicode, l’archivage, les identifiants,
les erreurs partielles et l’arrêt sûr, les quatre modes, le RAW préservé et
les images autonomes hors thread graphique. Les petites matrices CCM et les
buffers caméra de test sont **synthétiques**, pas une calibration ni une
validation matérielle du constructeur.

Reproduire les contrôles visuels à 1280 × 800 et 1920 × 1080 :

```bash
QT_QPA_PLATFORM=offscreen:configfile=tests/qt_offscreen.json .venv/bin/python tests/capture_ui.py --width 1280 --height 800 --output /tmp/arducam-ui-1280
QT_QPA_PLATFORM=offscreen:configfile=tests/qt_offscreen.json .venv/bin/python tests/capture_ui.py --width 1920 --height 1080 --output /tmp/arducam-ui-1920
```

Le script génère une carte de démonstration et des captures d’interface
**sans caméra réelle**. Qt 6 utilise des dimensions logiques et suit la mise
à l’échelle de l’écran ; un contrôle offscreen ne valide pas Windows.
Restent à vérifier sur matériel : les quatre modes et leurs dimensions,
l’identité USB entre DSHOW/MSMF, l’effet optique du focus (pas 1, plage 0–1023),
la stabilisation et les débranchements, le RAW exact et son rendu avec le vrai
fichier CCM autorisé, la consommation mémoire à 108 MP, le tactile et la mise
à l’échelle Windows à 100 %, 125 % et 150 %.
