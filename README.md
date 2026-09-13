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
| **Apprentissage** | Vous faites la manipulation **une seule fois** dans un navigateur ; le logiciel enregistre le parcours et le rejoue ensuite seul. |
| **Connexion** | Identifiants lus dans le trousseau système, jamais dans le dépôt. |
| **Authentification forte** | Code OTP récupéré automatiquement (passerelle SMS, e-mail, TOTP) ou saisi au clavier. |
| **Extraction** | Comptes, soldes et écritures, pagination suivie jusqu'au bout. |
| **Relevés officiels** | Téléchargement et archivage des PDF, renommés et classés par compte. |
| **Normalisation** | Montants et dates ramenés à un format unique, quel que soit le portail. |
| **Stockage** | SQLite (ou PostgreSQL), avec déduplication : relancer n'ajoute aucun doublon. |
| **Exports** | CSV et Excel prêts pour la comptabilité, plus OFX pour les logiciels comptables. |
| **Consultation** | Tableau de bord web local : soldes, flux mensuels, écritures, journal des exécutions. |
| **Programmation** | Extractions récurrentes : chaque jour, chaque lundi, le 1er du mois… |
| **Courriel** | Envoi automatique des relevés et exports aux adresses de votre choix. |

---

## État du projet

**Opérationnel et testé de bout en bout** : enregistrement du parcours, rejeu,
OTP automatique, téléchargement, normalisation, base, exports, tableau de bord,
récurrences et envoi par courriel. 176 tests, dont le cycle complet
*enregistrer → rejouer → analyser* contre un faux portail e-banking fourni.

**Ce qui reste à faire pour VOS banques** : enregistrer une fois le parcours de
chaque banque (`bankextract record`). Comptez cinq minutes — le temps de vous
connecter et de télécharger un relevé. Rien à programmer, aucun sélecteur à
relever.

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

Pour essayer l'enregistrement de parcours sur ce même portail fictif
(identifiants `demo` / `demo123`, le code SMS est écrit dans `data/otp.txt`) :

```bash
bankextract record demo --url http://127.0.0.1:8777
```

---

## Prise en main : enregistrer une banque

C'est la seule étape manuelle, et elle ne se fait qu'une fois par banque.

```bash
bankextract record bna --url https://ebanking.bna.dz
```

Une fenêtre s'ouvre sur le portail. Vous faites **exactement ce que vous feriez
d'habitude** :

1. vous vous connectez ;
2. vous saisissez le code SMS s'il est demandé ;
3. vous naviguez jusqu'au relevé et vous le téléchargez ;
4. vous **fermez la fenêtre**.

Le logiciel affiche alors le parcours qu'il a retenu :

```
  # Action     Détail
  1 goto       ouvrir https://ebanking.bna.dz
  2 fill       saisir « … » dans input « Identifiant »
  3 fill       saisir ••• dans input « Mot de passe »
  4 click      button « Se connecter »
  5 fill       saisir ••• dans input « Code à 6 chiffres »
  6 click      button « Valider »
  7 click      a « Mes comptes »
  8 download   télécharger (a « Relevé PDF »)

  ↳ étape 2 reconnue comme identifiant → {{username}}
  ↳ étape 5 reconnue comme code d'authentification → {{otp}}
```

Il écrit `scenarios/bna.json` et vous donne les lignes à coller dans
`config/banks.yaml`. À partir de là :

```bash
bankextract replay bna       # rejoue le parcours maintenant
bankextract scenarios        # liste les parcours enregistrés
```

### Ce qui n'est jamais enregistré

**Le mot de passe et le code SMS ne quittent pas la page.** Un champ de type
`password` est remplacé par le marqueur `{{password}}` avant même que
l'information ne parvienne au logiciel ; le champ de code devient `{{otp}}`.
Le fichier `scenarios/bna.json` peut être lu par n'importe qui sans rien
révéler. Un contrôle automatique signale toute valeur sensible qui aurait
échappé au masquage, et les tests vérifient qu'aucun mot de passe n'atteint le
disque.

### Ce qui est reconnu tout seul

| Élément | Comment il est repéré | Ce qu'il devient |
|---|---|---|
| Identifiant | champ saisi juste avant le mot de passe | `{{username}}` |
| Mot de passe | champ de type `password` | `{{password}}` |
| Code SMS | champ nommé *otp*, *code*, *sms*… après le mot de passe | `{{otp}}` |
| Dates de période | valeurs reconnues comme des dates | `{{start}}` / `{{end}}` |
| Téléchargement | clic suivi d'un fichier reçu | étape `download` |

Les dates deviennent des jetons : sans cela, le rejeu redemanderait
éternellement la même période. L'étape d'OTP est marquée facultative, car les
banques qui reconnaissent « l'appareil de confiance » ne la redemandent pas.

Tout reste modifiable : `scenarios/bna.json` est du JSON indenté, où chaque
étape porte son libellé d'origine.

### Pourquoi le rejeu résiste aux refontes

Chaque étape retient **plusieurs sélecteurs**, du plus stable au plus fragile :
identifiant, attribut `name`, libellé visible du bouton, puis chemin CSS. Au
rejeu ils sont essayés dans l'ordre — un bouton qui change de classe mais garde
son texte continue de fonctionner.

Quand tous échouent, le message nomme l'étape fautive et les candidats essayés,
et une capture d'écran est déposée dans `logs/screenshots/` :

```
✗ étape 7 (click a « Mes comptes ») : aucun sélecteur ne correspond
  — le portail a peut-être changé. Candidats essayés : #accounts-link, text="Mes comptes"…
```

Il suffit alors de réenregistrer le parcours.

---

## Extractions programmées et envoi par courriel

Les récurrences se décrivent dans `config/schedules.yaml` :

```yaml
schedules:
  - name: releve-mensuel
    enabled: true
    banks: [bna]
    period: mois_precedent        # le mois écoulé, en entier
    recurrence:
      frequency: mensuel
      day_of_month: 1
      at: "06:00"
    mail:
      enabled: true
      to: [comptabilite@medicomedline.com]
      attach_statements: true     # les relevés téléchargés depuis la banque
      attach_exports: true        # les fichiers CSV et Excel normalisés
```

| Récurrence | Réglages |
|---|---|
| `quotidien` | `at` |
| `hebdomadaire` | `at`, `day_of_week` (0 = lundi) |
| `mensuel` | `at`, `day_of_month` — un 31 demandé devient le 28 ou le 30 selon le mois |
| `intervalle` | `at`, `interval_days` |

| Période extraite | Ce qu'elle couvre |
|---|---|
| `depuis_derniere_execution` | depuis la dernière fois, avec un jour de recouvrement |
| `derniers_jours` | les `days` derniers jours |
| `mois_en_cours` | du 1er du mois à aujourd'hui |
| `mois_precedent` | le mois écoulé, du 1er au dernier jour |

Vérifiez, puis lancez :

```bash
bankextract schedules                    # prochaines échéances
bankextract mail-test vous@exemple.dz    # valide la configuration SMTP
bankextract scheduler                    # tourne en continu
bankextract scheduler --once             # vérifie une fois (pour cron)
```

Entrée cron recommandée — une vérification par heure suffit, le planificateur
sait lesquelles sont échues :

```cron
0 * * * * cd /opt/bankextract && .venv/bin/bankextract scheduler --once >> logs/cron.log 2>&1
```

Une exécution manquée — poste éteint, coupure réseau — est **rattrapée au
prochain réveil** plutôt que perdue.

### Configurer l'envoi

```yaml
smtp:
  host: smtp.gmail.com
  port: 587
  username: comptabilite@medicomedline.com
  password_env: SMTP_PASSWORD     # le mot de passe reste dans .env
  use_tls: true
  sender: comptabilite@medicomedline.com
  max_attachment_mb: 20
```

Avec Gmail ou Microsoft 365, créez un **mot de passe d'application** dédié
plutôt que d'utiliser celui du compte.

Le message récapitule l'extraction — comptes, soldes, totaux débit et crédit —
et porte les fichiers en pièces jointes. Si l'ensemble dépasse la limite, les
plus lourds sont écartés et nommés dans le corps plutôt que de faire rejeter
l'envoi. En cas d'échec d'extraction, un message d'alerte part quand même
(réglable par `send_on_error`).

---

## Utilisation courante

```bash
bankextract record bna                 # enregistre le parcours (une fois par banque)
bankextract login bna                  # enregistre les identifiants dans le trousseau
bankextract replay bna                 # rejoue le parcours maintenant
bankextract scenarios                  # parcours enregistrés

bankextract extract                    # toutes les banques activées
bankextract extract bna --days 30      # une banque, 30 derniers jours
bankextract extract bna --headed       # en voyant le navigateur (mise au point)

bankextract schedules                  # extractions programmées et prochaines échéances
bankextract scheduler --once           # exécute ce qui est échu (appel par cron)
bankextract mail-test vous@exemple.dz  # vérifie la configuration SMTP

bankextract accounts                   # soldes connus
bankextract export --since 2026-01-01  # export CSV + Excel de l'historique
bankextract history                    # journal des exécutions
bankextract dashboard                  # http://127.0.0.1:8000
```

---

## Solution de repli : décrire une banque à la main

L'enregistrement du parcours couvre la quasi-totalité des cas. Cette méthode
reste utile quand vous voulez lire les écritures **directement à l'écran**
plutôt que dans un relevé téléchargé, ou piloter finement la pagination.

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
- **Les parcours enregistrés ne contiennent aucun secret.** Mot de passe et
  code SMS sont remplacés par des marqueurs dans la page elle-même, avant tout
  enregistrement. Un contrôle automatique le vérifie, et un test le garantit.
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
├── recorder/
│   ├── inject.js       Capteur d'actions injecté dans les pages du portail
│   ├── record.py       Enregistrement, masquage des secrets, reconnaissance des champs
│   ├── scenario.py     Format du parcours (JSON lisible et modifiable)
│   └── replay.py       Rejeu, avec plusieurs sélecteurs candidats par étape
├── scheduler.py        Récurrences, fenêtres d'extraction, rattrapage
├── mailer.py           Envoi SMTP des relevés et des exports
├── parsers/            Relevés PDF et CSV/Excel déjà téléchargés
├── storage/db.py       SQLAlchemy + déduplication par empreinte
├── export/writers.py   CSV, Excel (une feuille par compte), OFX
├── dashboard/          Tableau de bord FastAPI en lecture seule
├── pipeline.py         Extraction → base → exports
└── cli.py              Interface en ligne de commande
```

**Le choix structurant** : rien de spécifique à une banque ne vit dans le code
Python. Un parcours enregistré est un fichier JSON ; une banque décrite à la
main est une section de YAML. Quand un portail est refondu — ce qui arrive — on
réenregistre le parcours en cinq minutes, sans toucher au logiciel.

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
pytest                      # 176 tests
pytest -m "not e2e" -q      # 155 tests, sans le navigateur (~9 s)
```

Les tests de bout en bout tournent contre le faux portail de
`tests/fixtures/fake_bank/`, qui reproduit un vrai parcours : identifiants, OTP
envoyé par « SMS », comptes, écritures paginées, relevés PDF et CSV.

L'enregistreur y est vérifié **sans intervention humaine** : le test pilote la
page, ce qui produit de vrais événements du navigateur — exactement ceux qu'un
utilisateur déclencherait. Sont ainsi couverts le cycle complet
*enregistrer → rejouer → télécharger → analyser*, l'absence de tout mot de
passe dans le fichier enregistré, le message d'erreur quand un sélecteur est
devenu obsolète, et le calcul des échéances (dont le 31 d'un mois de 30 jours).

L'envoi de courriel est testé contre un serveur SMTP local : pièces jointes,
dépassement de taille, échec de connexion. Aucune banque réelle n'est
sollicitée, aucun message ne part sur Internet.

---

## Limites connues

- **Les relevés PDF scannés** (images) ne sont pas lus : il faudrait un OCR.
  Les PDF texte, eux, sont analysés.
- **Les portails à CAPTCHA** ne sont pas contournés — et ne doivent pas l'être.
  Sur ces banques, utilisez `--headed` et résolvez le CAPTCHA à la main, ou
  demandez un accès par fichier à votre banque.
- **Un portail peut changer sans préavis.** C'est la contrepartie de l'absence
  d'API. Les sélecteurs multiples absorbent les petits remaniements ; après une
  refonte complète, il faut réenregistrer le parcours — cinq minutes. Le message
  d'erreur nomme l'étape fautive et une capture d'écran est déposée.
- **Le parcours enregistré est figé dans son chemin.** Si vous téléchargez le
  relevé d'un seul compte, seul celui-là sera récupéré. Pour plusieurs comptes,
  enregistrez un parcours par compte (`bna_courant`, `bna_devises`) : chacun a
  sa propre configuration et son propre numéro de compte.
- **Une fenêtre de navigateur doit pouvoir s'ouvrir pour l'enregistrement.** Sur
  un serveur sans écran, enregistrez le parcours depuis un poste de bureau puis
  copiez le fichier `scenarios/*.json` — le rejeu, lui, tourne sans écran.
