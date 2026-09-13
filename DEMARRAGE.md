# Premier essai — guide pas à pas

Ce document s'adresse à qui veut faire fonctionner le logiciel, pas le modifier.
Comptez vingt minutes, dont quinze d'attente pendant les téléchargements.

---

## 1. Installer

Ouvrez **PowerShell** : touche Windows, tapez `powershell`, appuyez sur Entrée.

Copiez-collez cette ligne, puis Entrée :

```powershell
irm https://raw.githubusercontent.com/azzouz-dz-hue/Fable5/claude/bank-statement-extraction-5uas3w/install.ps1 | iex
```

Le script installe tout dans `C:\Users\VotreNom\BankExtract` et place un
raccourci **BankExtract** sur votre Bureau. Il ne touche à rien d'autre.

Si Python manque, il vous proposera de l'installer. Acceptez, **fermez la
fenêtre**, rouvrez PowerShell et relancez la même ligne.

> **Autre méthode — le fichier .exe.** Si vous préférez ne rien installer :
>
> 1. ouvrez <https://github.com/azzouz-dz-hue/Fable5/actions/workflows/build-windows.yml> ;
> 2. cliquez sur l'exécution la plus récente marquée d'une coche verte ;
> 3. tout en bas, section **Artifacts**, téléchargez `BankExtract-Windows`
>    (environ 100 Mo — il faut être connecté à GitHub) ;
> 4. décompressez le dossier où vous voulez, puis ouvrez une invite de
>    commandes dedans (clic droit dans le dossier → *Ouvrir dans le Terminal*).
>
> Lancez d'abord `bankextract.exe setup` : il télécharge le navigateur, environ
> 150 Mo, une seule fois. Remplacez ensuite `bankextract` par `bankextract.exe`
> dans toutes les commandes ci-dessous.
>
> Ces archives sont conservées 90 jours. Une nouvelle est produite à chaque
> modification du logiciel.

---

## 2. Enregistrer vos identifiants

Double-cliquez sur le raccourci **BankExtract**. Une fenêtre noire s'ouvre.

```
bankextract login bna
```

Il demande votre identifiant et votre mot de passe de la banque. Ils sont
rangés dans le coffre de Windows — ni dans un fichier, ni dans le logiciel.

> Remplacez `bna` par le nom que vous donnerez à votre banque. Ce nom est
> libre : `bna`, `cpa`, `bna_sarl`… Gardez le même partout.

---

## 3. Enregistrer le parcours

C'est l'étape qui apprend au logiciel comment récupérer votre relevé. Elle ne
se fait **qu'une fois par banque**.

```
bankextract record bna --url https://ebanking.bna.dz
```

Une fenêtre de navigateur s'ouvre sur le site de votre banque. Faites
exactement ce que vous faites d'habitude :

1. connectez-vous ;
2. saisissez le code reçu par SMS s'il est demandé ;
3. allez jusqu'à votre relevé et **téléchargez-le** ;
4. **fermez la fenêtre du navigateur.**

Le logiciel affiche alors la liste des gestes qu'il a retenus, et vous donne
quelques lignes à coller dans le fichier de configuration. Ouvrez
`config\banks.yaml` avec le Bloc-notes, collez-les à la fin, enregistrez.

**Votre mot de passe n'est pas enregistré.** Il apparaît comme `•••` dans la
liste et le fichier ne le contient pas. C'est vérifié automatiquement.

---

## 4. Vérifier que le rejeu fonctionne

```
bankextract replay bna
```

Le logiciel refait seul tout le parcours. Si votre banque demande un code SMS,
il s'arrête et vous le demande dans la fenêtre noire ; tapez-le, il continue.

À la fin, vos fichiers sont dans :

- `data\downloads\` — les relevés tels que la banque les fournit ;
- `data\exports\` — les mêmes données en CSV et en Excel, prêtes pour la compta.

Pour voir le tout dans une page web :

```
bankextract dashboard
```

Puis ouvrez `http://127.0.0.1:8000` dans votre navigateur. Pour arrêter :
`Ctrl` + `C` dans la fenêtre noire.

---

## 5. Programmer l'envoi automatique

Une fois le rejeu au point, ouvrez `config\schedules.yaml` avec le Bloc-notes.

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
| `bankextract n'est pas reconnu` | Passez par le raccourci BankExtract du Bureau, pas par une fenêtre PowerShell quelconque. |
| `Le navigateur nécessaire n'est pas encore installé` | Lancez `bankextract setup`. |
| `Identifiants manquants` | Lancez `bankextract login bna`. |
| `aucun sélecteur ne correspond` | Le site de la banque a changé. Refaites l'étape 3. |
| `aucun code reçu` | Le code SMS n'est pas arrivé à temps. Relancez ; au besoin mettez `provider: manual` dans `config\banks.yaml`. |
| Le rejeu s'arrête sans rien dire | Relancez avec `bankextract replay bna --headed` : le navigateur devient visible et vous voyez où il bloque. |

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
