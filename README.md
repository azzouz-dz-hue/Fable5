# BankExtract

Extraction automatisée des relevés de comptes bancaires depuis les portails
e-banking, avec normalisation comptable, base de données et tableau de bord.

Conçu pour les banques algériennes, qui n'exposent aucune API : le logiciel
pilote le portail dans un navigateur, comme le ferait un opérateur, puis
normalise ce qu'il en ramène.

---

## Ce que fait le logiciel

| Étape | Détail |
|---|---|
| **Connexion** | Identifiants lus dans le trousseau système, jamais dans le dépôt. |
| **Authentification forte** | Code OTP récupéré automatiquement (passerelle SMS, e-mail, TOTP) ou saisi au clavier. |
| **Extraction** | Comptes, soldes et écritures, pagination suivie jusqu'au bout. |
| **Relevés officiels** | Téléchargement et archivage des PDF, renommés et classés par compte. |
| **Normalisation** | Montants et dates ramenés à un format unique, quel que soit le portail. |
| **Stockage** | SQLite (ou PostgreSQL), avec déduplication : relancer n'ajoute aucun doublon. |
| **Exports** | CSV et Excel prêts pour la comptabilité, plus OFX pour les logiciels comptables. |
| **Consultation** | Tableau de bord web local : soldes, flux mensuels, écritures, journal des exécutions. |

---

## État du projet

**Opérationnel et testé de bout en bout** : le socle complet — connexion, OTP
automatique, pagination, téléchargement, normalisation, base, exports et
tableau de bord — tourne et est couvert par 108 tests, dont un parcours complet
contre un faux portail e-banking fourni.

**Ce qui reste à faire pour VOS banques** : renseigner les sélecteurs CSS de
chaque portail dans `config/banks.yaml`. Aucune banque algérienne ne publie la
structure de son site et les portails ne sont pas accessibles publiquement :
ces sélecteurs se relèvent en une quinzaine de minutes par banque, avec la
commande `bankextract inspect` prévue pour cela (voir plus bas). Le code
Python n'a pas à être modifié.

---

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium        # une seule fois
```

Vérifiez l'installation sur la banque de démonstration, sans toucher à une
vraie banque :

```bash
# Terminal 1 — faux portail e-banking
python tests/fixtures/fake_bank/server.py

# Terminal 2 — extraction
cp .env.example .env
# dans config/banks.yaml, sous la section « demo », passez « enabled: false » à « true »
bankextract extract demo --since 2026-02-01 --until 2026-03-31
bankextract dashboard
```

---

## Utilisation courante

```bash
bankextract login bna                  # enregistre les identifiants dans le trousseau
bankextract extract                    # toutes les banques activées
bankextract extract bna --days 30      # une banque, 30 derniers jours
bankextract extract bna --headed       # en voyant le navigateur (mise au point)
bankextract accounts                   # soldes connus
bankextract export --since 2026-01-01  # export CSV + Excel de l'historique
bankextract history                    # journal des exécutions
bankextract dashboard                  # http://127.0.0.1:8000
```

Automatisation quotidienne (cron, 6 h du matin) :

```cron
0 6 * * * cd /opt/bankextract && .venv/bin/bankextract extract >> logs/cron.log 2>&1
```

---

## Décrire une nouvelle banque

Tout se passe dans `config/banks.yaml` — aucun code à écrire.

**1. Relever les sélecteurs.** La commande ouvre le portail dans un navigateur
visible et vous laisse inspecter les pages :

```bash
bankextract inspect bna
```

Sur chaque page (connexion, liste des comptes, opérations), clic droit sur
l'élément → *Inspecter* → *Copier le sélecteur*.

**2. Les reporter dans la configuration :**

```yaml
banks:
  bna:
    connector: bna
    enabled: true
    username_env: BNA_USERNAME
    password_env: BNA_PASSWORD
    options:
      base_url: https://ebanking.bna.dz
      login:
        url: /login
        username_selector: "#username"
        password_selector: "#password"
        submit_selector: "button[type=submit]"
        success_selector: ".dashboard"     # visible UNIQUEMENT une fois connecté
        error_selector: ".alert-danger"    # message d'erreur de la banque
        otp:
          input_selector: "#otp"
          submit_selector: "#otp-submit"
      accounts:
        url: /accounts
        row_selector: "table.accounts tbody tr"
        columns:
          number: "td:nth-child(1)"
          label: "td:nth-child(2)"
          balance: "td:nth-child(5)"
      transactions:
        url_template: "/accounts/{number}/operations?du={start}&au={end}"
        row_selector: "table.operations tbody tr"
        next_page_selector: "a.pagination-next"
        columns:
          date: "td:nth-child(1)"
          label: "td:nth-child(3)"
          debit: "td:nth-child(4)"
          credit: "td:nth-child(5)"
```

**3. Tester :**

```bash
bankextract extract bna --headed --days 15
```

En cas d'échec, une capture d'écran est déposée dans `logs/screenshots/` et le
message d'erreur de la banque est remonté tel quel.

Les trois présentations d'écritures sont gérées : colonnes *débit*/*crédit*
séparées, montant unique signé, ou montant positif accompagné d'une colonne
*sens* (D/C). Les formats « 1 500,50 », « 1.500,50 » et « 1,500.50 » sont
reconnus indifféremment.

---

## Authentification forte automatique

Le code OTP provient d'une source enfichable, choisie par banque :

| `provider` | Source | Intervention |
|---|---|---|
| `sms_gateway` | Passerelle HTTP lisant les SMS d'un téléphone Android | aucune |
| `imap` | Boîte e-mail (banques envoyant le code par courriel) | aucune |
| `totp` | Token logiciel (RFC 6238) | aucune |
| `file` | Fichier déposé par un outil tiers | aucune |
| `manual` | Saisie au clavier | à chaque connexion |

### Mettre en place la passerelle SMS

L'automatisation complète d'un OTP par SMS suppose un appareil qui reçoit ces
SMS. En pratique : un téléphone Android dédié, resté au bureau sur le Wi-Fi,
avec une application de passerelle SMS exposant les messages reçus en JSON.

1. Installez sur ce téléphone une application de passerelle SMS HTTP
   (SMS Gateway for Android, SMSSync, ou un script Termux).
2. Notez son adresse sur le réseau local et son jeton d'accès.
3. Renseignez la configuration :

```yaml
    otp:
      provider: sms_gateway
      timeout_seconds: 180
      options:
        url: http://192.168.1.50:8080/messages
        token_env: SMS_GATEWAY_TOKEN     # le jeton reste dans .env
        sender: BNA                       # ne retenir que les SMS de la banque
        text_field: body                  # champ portant le texte du SMS
        date_field: date                  # horodatage (epoch ou ISO 8601)
        code_pattern: '\b(\d{6})\b'
```

Les noms de champs sont paramétrables : n'importe quelle passerelle renvoyant
du JSON convient, sans écrire de code.

Seuls les SMS **postérieurs à la demande de connexion** sont acceptés, afin
qu'un ancien code ne soit jamais réutilisé.

> Si vous n'avez pas encore de passerelle, laissez `provider: manual` : le robot
> se connecte seul et vous demande le code au terminal, puis poursuit sans
> intervention. Vous pourrez basculer vers `sms_gateway` plus tard sans rien
> changer d'autre.

### Éviter l'OTP à chaque exécution

Le profil de navigateur est conservé entre deux exécutions
(`browser-profiles/`). Sur les banques proposant « appareil de confiance »,
l'OTP n'est alors demandé que la première fois. Si le champ OTP n'apparaît pas,
le connecteur poursuit sans attendre.

---

## Sécurité

- **Aucun identifiant dans le dépôt.** Les mots de passe vivent dans le
  trousseau système (`bankextract login`), à défaut dans `.env`, lui-même
  ignoré par git.
- **Rien ne sort du poste.** Base, exports et relevés restent en local ; aucun
  service tiers n'est appelé.
- **Les traces ne révèlent rien.** Les identifiants ne sont jamais affichés en
  clair, y compris dans les messages d'erreur.
- **Le tableau de bord est en lecture seule** : il ne peut déclencher aucune
  opération bancaire.
- **À protéger** : le dossier `data/` contient vos relevés. Sur un poste
  partagé, chiffrez-le et restreignez ses droits (`chmod 700 data`).

Point juridique : ce logiciel accède à **vos propres comptes**, avec **vos
propres identifiants** — même geste qu'une connexion manuelle. Vérifiez
néanmoins les conditions générales de votre banque : certaines encadrent
l'accès automatisé. En cas de doute, demandez à votre chargé de compte si un
accès par fichier (relevés déposés sur un espace dédié) est possible : c'est
plus stable qu'un portail web, qui peut changer sans préavis.

---

## Architecture

```
src/bankextract/
├── models.py           Account, Transaction, StatementFile (schéma normalisé)
├── normalize.py        Montants, dates, libellés — cœur de la fiabilité
├── config.py           Chargement de config/banks.yaml + .env
├── secrets.py          Trousseau système, variables d'environnement
├── browser.py          Session Playwright, profil persistant, captures d'erreur
├── connectors/
│   ├── base.py         Orchestration : une banque en échec n'arrête pas les autres
│   ├── generic.py      Moteur déclaratif piloté par les sélecteurs YAML
│   ├── algeria.py      BNA, CPA, BEA, BADR, BDL, SGA, AGB, BNP, Trust, CCP
│   └── demo.py         Banque fictive des tests
├── otp/                manual · sms_gateway · imap · totp · file
├── parsers/            Relevés PDF et CSV/Excel déjà téléchargés
├── storage/db.py       SQLAlchemy + déduplication par empreinte
├── export/writers.py   CSV, Excel (une feuille par compte), OFX
├── dashboard/          Tableau de bord FastAPI en lecture seule
├── pipeline.py         Extraction → base → exports
└── cli.py              Interface en ligne de commande
```

**Le choix structurant** : les sélecteurs vivent dans le YAML, pas dans le
Python. Quand une banque refond son portail — ce qui arrive — la correction est
une ligne de configuration, pas une modification de code.

Pour une banque au parcours atypique (clavier virtuel, iframe, canvas), on
hérite de `GenericPortalConnector` et on ne redéfinit que la méthode concernée ;
tout le reste est acquis.

---

## Ajouter un connecteur sur mesure

```python
from bankextract.connectors import register
from bankextract.connectors.generic import GenericPortalConnector

@register
class MaBanqueConnector(GenericPortalConnector):
    name = "mabanque"
    display_name = "Ma Banque"
    base_url = "https://ebanking.mabanque.dz"

    def login(self, session, credentials):
        """Clavier virtuel : le mot de passe se saisit en cliquant des touches."""
        session.goto(self._url("/login"))
        session.page.fill("#user", credentials.username)
        for chiffre in credentials.password:
            session.page.click(f"button.key[data-value='{chiffre}']")
        session.page.click("#valider")
        self._handle_otp(session, self.spec["login"]["otp"], datetime.now(timezone.utc))
```

---

## Tests

```bash
pytest                      # 108 tests
pytest -m "not e2e" -q      # sans le navigateur
```

Les tests de bout en bout tournent contre le faux portail de
`tests/fixtures/fake_bank/` : connexion, OTP lu automatiquement, pagination sur
deux pages, téléchargement de PDF, déduplication à la relance. Aucune banque
réelle n'est sollicitée.

---

## Limites connues

- **Les relevés PDF scannés** (images) ne sont pas lus : il faudrait un OCR.
  Les PDF texte, eux, sont analysés.
- **Les portails à CAPTCHA** ne sont pas contournés — et ne doivent pas l'être.
  Sur ces banques, utilisez `--headed` et résolvez le CAPTCHA à la main, ou
  demandez un accès par fichier à votre banque.
- **Un portail peut changer sans préavis.** C'est la contrepartie de l'absence
  d'API : prévoyez de revérifier les sélecteurs après une refonte. Les captures
  d'écran d'erreur sont là pour rendre le diagnostic immédiat.
