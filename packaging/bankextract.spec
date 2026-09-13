# -*- mode: python ; coding: utf-8 -*-
"""Recette PyInstaller : produit bankextract.exe pour Windows.

Double-cliqué, l'exécutable ouvre l'interface graphique dans le navigateur ;
lancé avec des arguments, il se comporte comme l'outil en ligne de commande.

Compilée par GitHub Actions (.github/workflows/build-windows.yml), sur un vrai
Windows — PyInstaller ne sait pas produire un exécutable Windows depuis Linux.

Le navigateur n'est PAS embarqué : il pèse 150 Mo et se télécharge au premier
lancement, via « bankextract setup ». L'exécutable, lui, reste transportable.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

# Playwright embarque un pilote Node : sans collect_all, l'exécutable démarre
# mais ne sait pas ouvrir de navigateur.
for paquet in ("playwright", "bankextract"):
    paquet_datas, paquet_binaries, paquet_hidden = collect_all(paquet)
    datas += paquet_datas
    binaries += paquet_binaries
    hiddenimports += paquet_hidden

# Ces bibliothèques chargent des modules par leur nom : PyInstaller ne peut pas
# les deviner en lisant le code.
for paquet in ("uvicorn", "pdfminer", "pydantic"):
    datas += collect_data_files(paquet)
    hiddenimports += collect_all(paquet)[2]

hiddenimports += [
    # Trousseau Windows, chargé dynamiquement par keyring.
    "keyring.backends.Windows",
    "win32timezone",
    # Dialecte SQLite, résolu par nom au moment de la connexion.
    "sqlalchemy.dialects.sqlite",
    # Moteur des classeurs Excel.
    "openpyxl",
    # Serveur du tableau de bord.
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# SPECPATH : le chemin du présent fichier, quel que soit le dossier d'appel.
analysis = Analysis(
    [os.path.join(SPECPATH, "lanceur.py")],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Allège l'exécutable : ces paquets ne servent qu'aux tests et au tracé.
    excludes=["matplotlib", "pytest", "reportlab", "aiosmtpd", "IPython"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="bankextract",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # c'est un outil en ligne de commande
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
