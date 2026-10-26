# Arducam Photo v2

Application locale Windows de prise de vue pour la caméra Arducam B0494C.

## Statut

Nouveau projet à développer. Ce README décrit le périmètre cible,
pas des fonctionnalités déjà réalisées ou validées.

## Principe

Trois onglets intégrés dans une interface adaptée à un écran tactile :

- **Preview** : cadrage, réglage du focus et création des banks.
- **Capture** : prise de photo avec une bank sauvegardée.
- **Traitement** : traitement des fichiers enregistrés, sans accès caméra.

Le focus manuel est le seul réglage caméra piloté par l’application.

## Référence existante

Ancien dépôt : https://github.com/MJOpeanuts/arducam_photo

Il sert de référence pour :
- les mécanismes d’acquisition ;
- la reconstruction du RAW ;
- les traitements existants ;
- les ressources graphiques et l’identité visuelle.

L’ancien projet ne doit pas être modifié.
Aucune migration de ses banks, profils ou préférences n’est demandée.

## Bases techniques

- Python
- PySide6
- OpenCV
- NumPy
- Application locale, sans navigateur ni serveur
- Cible : Windows 11 x64

## Modes d’acquisition

| Identifiant | Données attendues |
| --- | --- |
| `color_720p` | Couleur, 1280 × 720 |
| `color_4k` | Couleur, 3840 × 2160 |
| `color_12mp` | Couleur, 4000 × 3000 |
| `raw_108mp` | Bayer reconstruit, 12000 × 9000 |

La disponibilité des modes doit être vérifiée sur le matériel.
Aucun repli silencieux vers une autre résolution.

## Focus manuel

- Consigne entière de 0 à 1023.
- Pas de saisie logiciel : 1.
- Aucune unité physique.
- Aucune correspondance supposée avec une distance.
- Pas d’autofocus.

La plage correspond à la convention actuellement retenue dans le projet.
Elle ne constitue pas une calibration optique.

## Banks

Une bank représente le paramétrage sauvegardé utilisé pour une capture.

Elle contient notamment :
- un identifiant et un nom ;
- le mode d’acquisition ;
- la consigne de focus ;
- une version de schéma et une révision ;
- le contexte caméra/backend nécessaire.

Modifier ne sauvegarde pas.
Sauvegarder ne prend pas de photo.
Capture utilise une configuration sauvegardée et figée.

## Invariants

- Un seul propriétaire de la caméra.
- Aucun flux continu au repos dans Capture.
- Aucun retour automatique au Preview après une photo.
- Aucun accès caméra depuis Traitement.
- Aucun réglage d’exposition, gain, balance des blancs, luminosité,
  contraste ou saturation envoyé par le parcours caméra de l’application.
- Les paramètres techniques nécessaires au transport restent internes.
- Conservation de l’original RAW avant son traitement.
- Aucun écrasement des captures originales.
- Données utilisateur séparées du dossier du programme.

## Interface

Reprendre l’identité visuelle Nuts de l’ancien projet, avec une image
dominante et l’orange comme couleur d’action principale.

Les trois onglets doivent former une seule application cohérente :
navigation persistante, composants homogènes et transitions explicites.

Les commandes essentielles doivent être utilisables au doigt,
sans survol ni clavier physique obligatoire.

## Développement et validation

L’agent doit compléter ce README avec :
- les prérequis et versions prises en charge ;
- les commandes réelles d’installation et de lancement ;
- la procédure de build Windows portable ;
- les emplacements de stockage ;
- les tests exécutés ;
- les limites et vérifications matérielles restantes.

Ne pas présenter les tests simulés comme une validation sur la caméra.
