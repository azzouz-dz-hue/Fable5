# Premier essai — guide pas à pas

Ce document s'adresse à qui veut faire fonctionner le logiciel, pas le modifier.
Comptez vingt minutes, dont quinze d'attente pendant les téléchargements.

---

## 1. Télécharger et lancer

**[Télécharger BankExtract pour Windows](https://github.com/azzouz-dz-hue/Fable5/releases/download/windows-latest/BankExtract-Windows.zip)** *(environ 100 Mo)*

1. décompressez le fichier `BankExtract-Windows.zip` où vous voulez —
   par exemple sur votre Bureau ;
2. double-cliquez sur **`bankextract.exe`**.

Une page s'ouvre dans votre navigateur : c'est l'interface du logiciel. Une
petite fenêtre noire reste ouverte derrière — **ne la fermez pas**, c'est elle
qui fait tourner le programme. Pour quitter, fermez-la.

> **Au premier lancement**, cliquez sur *Préparer le poste* si l'interface vous
> le propose : le navigateur nécessaire est téléchargé une seule fois, environ
> 150 Mo.

> **Windows affichera sans doute un avertissement** (« Windows a protégé votre
> ordinateur ») : le fichier n'est pas signé numériquement. Cliquez sur
> *Informations complémentaires*, puis *Exécuter quand même*. Votre antivirus
> peut aussi être méfiant — c'est courant avec ce type de programme.

### Autre méthode : l'installation complète

Si vous comptez utiliser l'outil régulièrement, le script d'installation crée
un raccourci sur le Bureau et facilite les mises à jour. Dans PowerShell :

```powershell
irm https://raw.githubusercontent.com/azzouz-dz-hue/Fable5/claude/bank-statement-extraction-5uas3w/install.ps1 | iex
```

---

## 2. Ajouter votre banque

Dans l'interface, remplissez le formulaire **Ajouter une banque** :

| Champ | Ce qu'il faut mettre |
|---|---|
| Nom court | `bna` — sans espace ni accent, il sert de repère interne |
| Libellé | `BNA — compte SARL`, ce que vous verrez à l'écran |
| Adresse du portail | `https://ebanking.bna.dz` |
| Numéro de compte | tel qu'il figure sur votre relevé |
| Code de connexion | laissez `manual` : le code SMS vous sera demandé |

Cliquez sur **Enregistrer la banque**. Elle apparaît dans la liste, avec deux
points orange : il reste deux choses à faire.

---

## 3. Déposer vos identifiants

Sur la ligne de votre banque, cliquez sur **Déposer les identifiants**, puis
saisissez ceux de votre accès e-banking.

Ils sont rangés dans le coffre de Windows — ni dans un fichier, ni dans le
logiciel. Le premier point passe au vert.

---

## 4. Enregistrer le parcours

C'est l'étape qui apprend au logiciel comment récupérer votre relevé. Elle ne
se fait **qu'une fois par banque**.

Cliquez sur **Enregistrer le parcours**.

Une fenêtre de navigateur s'ouvre sur le site de votre banque. Faites
exactement ce que vous faites d'habitude :

1. connectez-vous ;
2. saisissez le code reçu par SMS s'il est demandé ;
3. allez jusqu'à votre relevé et **téléchargez-le** ;
4. **fermez la fenêtre du navigateur.**

L'interface affiche alors la liste des gestes qu'il a retenus, et le second
point passe au vert. Il n'y a rien à recopier : tout est enregistré.

**Votre mot de passe n'est pas enregistré.** Il apparaît comme `•••` dans la
liste et le fichier ne le contient pas. C'est vérifié automatiquement.

---

## 5. Lancer l'extraction

Cliquez sur **Lancer l'extraction**. Le logiciel refait seul tout le parcours,
et vous suivez sa progression à l'écran.

Si votre banque demande un code SMS, il vous est demandé dans la fenêtre noire ;
tapez-le, le logiciel continue.

> La première fois, préférez **Essai visible** : le navigateur reste affiché et
> vous voyez exactement ce que fait le logiciel — et où il bloque, le cas
> échéant.

À la fin, vos fichiers sont dans :

- `data\downloads\` — les relevés tels que la banque les fournit ;
- `data\exports\` — les mêmes données en CSV et en Excel, prêtes pour la compta.

Le bouton **Voir les relevés**, en haut de l'interface, ouvre un tableau de
bord : soldes par devise, flux mensuels, écritures et journal des exécutions.

---

## 6. Programmer l'envoi automatique

Cette étape passe encore par les fichiers. Ouvrez `config\schedules.yaml` avec
le Bloc-notes.

Renseignez votre serveur de messagerie et l'adresse de destination, puis passez
`enabled: false` à `enabled: true` sur la tâche voulue. Vérifiez l'envoi :

```
bankextract mail-test vous@exemple.dz
```

Puis, pour que le logiciel travaille seul, créez une tâche planifiée Windows
qui lance chaque heure :

```
bankextract scheduler --once
```

*(Menu Démarrer → « Planificateur de tâches » → Créer une tâche de base.)*

---

## Si quelque chose ne va pas

| Ce que vous voyez | Ce qu'il faut faire |
|---|---|
| La page ne s'ouvre pas | Regardez l'adresse affichée dans la fenêtre noire (`http://127.0.0.1:…`) et tapez-la dans votre navigateur. |
| `Le navigateur nécessaire n'est pas encore installé` | Fermez, rouvrez l'interface : le téléchargement se relance. Sinon, en invite de commandes : `bankextract.exe setup`. |
| Le bouton *Lancer l'extraction* reste gris | L'interface indique juste au-dessus ce qui manque : identifiants ou parcours. |
| `aucun sélecteur ne correspond` | Le site de la banque a changé. Refaites l'étape 4. |
| `aucun code reçu` | Le code SMS n'est pas arrivé à temps. Relancez l'extraction. |
| L'extraction s'arrête sans rien dire | Utilisez **Essai visible** : le navigateur s'affiche et vous voyez où ça bloque. |

Une capture d'écran est déposée dans `logs\screenshots\` à chaque échec : c'est
la pièce la plus utile pour comprendre ce qui s'est passé.

---

## Ce qu'il faut savoir avant de commencer

- Le logiciel n'a **jamais tourné contre une vraie banque**. Tout est vérifié
  contre un portail simulé. Votre premier essai est donc aussi le premier essai
  réel : prévoyez que quelque chose demande un ajustement.
- Le logiciel accède à **vos** comptes avec **vos** identifiants, comme si vous
  vous connectiez vous-même. Vérifiez tout de même les conditions générales de
  votre banque concernant l'accès automatisé.
- Le dossier `data\` contiendra vos relevés. Sur un poste partagé, protégez-le.
- Si quelque chose bloque, gardez le message d'erreur complet et la capture
  d'écran : ce sont eux qui permettent de corriger.
