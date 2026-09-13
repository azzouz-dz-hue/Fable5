"""Point d'entrée de l'exécutable Windows.

Un exécutable est souvent lancé par double-clic, depuis n'importe quel dossier.
Sans argument on affiche donc l'aide plutôt qu'une erreur, et on laisse la
fenêtre ouverte — sinon l'utilisateur ne voit rien.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    from bankextract.cli import app

    lance_par_double_clic = len(sys.argv) == 1

    if lance_par_double_clic:
        print("BankExtract — extraction automatisée des relevés de comptes\n")
        print(f"Dossier de travail : {Path.cwd()}\n")
        sys.argv.append("--help")

    try:
        app()
    except SystemExit as sortie:
        code = sortie.code if isinstance(sortie.code, int) else 0
    except Exception as erreur:  # une trace brute n'aide pas l'utilisateur final
        print(f"\nErreur : {erreur}", file=sys.stderr)
        code = 1
    else:
        code = 0

    if lance_par_double_clic:
        print("\nLancez cet outil depuis une invite de commandes pour passer des options.")
        input("Appuyez sur Entrée pour fermer…")
    return code


if __name__ == "__main__":
    sys.exit(main())
