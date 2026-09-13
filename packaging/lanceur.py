"""Point d'entrée de l'exécutable Windows.

Double-cliqué, il ouvre l'interface graphique dans le navigateur : c'est
l'usage attendu. Lancé depuis une invite de commandes avec des arguments, il se
comporte comme l'outil en ligne de commande habituel.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _dossier_de_travail() -> Path:
    """Où lire la configuration et déposer les relevés.

    Un exécutable est souvent posé dans Téléchargements puis lancé de là : on
    travaille donc à côté de lui, et non dans le dossier courant du système,
    qui peut être n'importe lequel.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path.cwd()


def main() -> int:
    import contextlib
    import os

    dossier = _dossier_de_travail()
    # Dossier en lecture seule : on garde alors le dossier courant.
    with contextlib.suppress(OSError):
        os.chdir(dossier)

    from bankextract.cli import app

    double_clic = len(sys.argv) == 1
    if double_clic:
        print("BankExtract — ouverture de l'interface…")
        print(f"Dossier de travail : {dossier}\n")
        sys.argv.append("app")

    try:
        app()
    except SystemExit as sortie:
        code = sortie.code if isinstance(sortie.code, int) else 0
    except KeyboardInterrupt:
        print("\nArrêt demandé.")
        code = 0
    except Exception as erreur:  # une trace brute n'aiderait pas l'utilisateur
        print(f"\nErreur : {erreur}", file=sys.stderr)
        code = 1
        if double_clic:
            input("\nAppuyez sur Entrée pour fermer…")
    else:
        code = 0

    return code


if __name__ == "__main__":
    sys.exit(main())
