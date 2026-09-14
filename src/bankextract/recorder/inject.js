/*
 * Capteur d'actions injecté dans chaque page du portail pendant l'enregistrement.
 *
 * Il n'envoie JAMAIS la valeur d'un champ de mot de passe : elle est remplacée
 * par un jeton avant même de quitter la page.
 *
 * Pour chaque action il construit plusieurs sélecteurs candidats, du plus stable
 * au plus fragile. Le rejeu les essaiera dans l'ordre, ce qui absorbe les petits
 * remaniements de mise en page entre l'enregistrement et l'exécution.
 */
(() => {
  if (window.__bankextractInstalled) return;
  window.__bankextractInstalled = true;

  const send = (payload) => {
    try {
      window.__bankextractRecord(JSON.stringify(payload));
    } catch (_) {
      /* le pont Python n'est pas encore prêt : l'action est perdue, sans gravité */
    }
  };

  const escapeQuotes = (value) => String(value).replace(/(["\\])/g, "\\$1");

  /* Un identifiant généré à chaque chargement (« input-8f3a2 ») ne sert à rien au rejeu. */
  const looksGenerated = (value) => {
    if (!value) return true;
    if (/^\d+$/.test(value)) return true;
    if (/[0-9a-f]{8,}/i.test(value)) return true;
    if (/^(ember|react|ng|mui|radix)[-_]?\d/i.test(value)) return true;
    return value.length > 60;
  };

  const isVisibleText = (element) => {
    const text = (element.innerText || element.textContent || "").trim();
    return text.length > 0 && text.length <= 60 ? text.replace(/\s+/g, " ") : null;
  };

  /* Chemin CSS en dernier recours : remonte jusqu'à un ancêtre identifiable. */
  const cssPath = (element) => {
    const parts = [];
    let node = element;
    while (node && node.nodeType === 1 && parts.length < 6) {
      if (node.id && !looksGenerated(node.id)) {
        parts.unshift(`#${CSS.escape(node.id)}`);
        break;
      }
      const parent = node.parentElement;
      if (!parent) {
        parts.unshift(node.tagName.toLowerCase());
        break;
      }
      const siblings = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
      const index = siblings.indexOf(node) + 1;
      parts.unshift(
        siblings.length > 1
          ? `${node.tagName.toLowerCase()}:nth-of-type(${index})`
          : node.tagName.toLowerCase()
      );
      node = parent;
    }
    return parts.join(" > ");
  };

  /* Candidats ordonnés du plus stable au plus fragile. */
  const buildSelectors = (element) => {
    const candidates = [];
    const tag = element.tagName.toLowerCase();
    const add = (selector) => {
      if (selector && !candidates.includes(selector)) candidates.push(selector);
    };

    if (element.id && !looksGenerated(element.id)) add(`#${CSS.escape(element.id)}`);

    for (const attribute of ["data-testid", "data-test", "data-qa", "data-cy"]) {
      const value = element.getAttribute(attribute);
      if (value) add(`[${attribute}="${escapeQuotes(value)}"]`);
    }

    /* Touches de clavier virtuel : viser l'attribut plutôt que le texte affiché,
       qui pourrait désigner un « 4 » situé ailleurs dans la page. */
    for (const attribute of ["data-value", "data-key", "data-digit", "data-char"]) {
      const value = element.getAttribute(attribute);
      if (value && value.length === 1) add(`[${attribute}="${escapeQuotes(value)}"]`);
    }

    const name = element.getAttribute("name");
    if (name) add(`${tag}[name="${escapeQuotes(name)}"]`);

    const ariaLabel = element.getAttribute("aria-label");
    if (ariaLabel) add(`[aria-label="${escapeQuotes(ariaLabel)}"]`);

    const placeholder = element.getAttribute("placeholder");
    if (placeholder) add(`${tag}[placeholder="${escapeQuotes(placeholder)}"]`);

    if (tag === "input") {
      const type = element.getAttribute("type");
      if (type && ["submit", "checkbox", "radio"].includes(type)) {
        add(`input[type="${type}"]`);
      }
    }

    /* Le libellé visible d'un bouton ou d'un lien survit souvent à une refonte CSS. */
    if (["a", "button"].includes(tag) || element.getAttribute("role") === "button") {
      const text = isVisibleText(element);
      if (text) add(`text="${escapeQuotes(text)}"`);
    }

    const className = typeof element.className === "string" ? element.className.trim() : "";
    if (className) {
      const stable = className
        .split(/\s+/)
        .filter((c) => c && !looksGenerated(c) && !/^(is|has)-/.test(c))
        .slice(0, 2);
      if (stable.length) add(`${tag}.${stable.map((c) => CSS.escape(c)).join(".")}`);
    }

    add(cssPath(element));
    return candidates;
  };

  /* L'utilisateur clique souvent sur l'icône DANS le bouton, pas sur le bouton. */
  const interactiveAncestor = (element) => {
    let node = element;
    for (let depth = 0; node && depth < 5; depth += 1) {
      const tag = node.tagName ? node.tagName.toLowerCase() : "";
      if (["a", "button", "label", "select", "summary"].includes(tag)) return node;
      if (tag === "input") return node;
      const role = node.getAttribute ? node.getAttribute("role") : null;
      if (role && ["button", "link", "tab", "menuitem"].includes(role)) return node;
      if (node.onclick) return node;
      node = node.parentElement;
    }
    return element;
  };

  const frameUrl = () => (window.top === window ? null : window.location.href);

  const describe = (element) => {
    const text = isVisibleText(element);
    const tag = element.tagName.toLowerCase();
    if (text) return `${tag} « ${text} »`;
    const placeholder = element.getAttribute("placeholder") || element.getAttribute("name");
    return placeholder ? `${tag} « ${placeholder} »` : tag;
  };

  /* Dernière valeur connue de chaque champ, pour distinguer ce que la page
     écrit elle-même de ce que l'utilisateur a déjà dicté. */
  const valeursConnues = new WeakMap();

  /* Une valeur qui ressemble à une date — même expression que côté Python. */
  const ressembleAUneDate = (texte) => /^\s*\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}\s*$/.test(texte);

  const champsDeTexte = () =>
    Array.prototype.filter.call(document.querySelectorAll("input"), (element) => {
      const type = (element.getAttribute("type") || "text").toLowerCase();
      return !["password", "checkbox", "radio", "submit", "button", "image", "file"].includes(type);
    });

  document.addEventListener(
    "click",
    (event) => {
      const element = interactiveAncestor(event.target);
      if (!element || !element.tagName) return;
      const tag = element.tagName.toLowerCase();
      const type = (element.getAttribute("type") || "").toLowerCase();
      /* Les cases et boutons radio sont captés par « change » : sinon on enregistre deux fois. */
      if (tag === "input" && ["checkbox", "radio"].includes(type)) return;
      /* Un champ texte cliqué n'est pas une action : c'est la saisie qui compte. */
      if (tag === "input" && !["submit", "button", "image"].includes(type)) return;
      if (tag === "textarea" || tag === "select") return;

      send({
        action: "click",
        selectors: buildSelectors(element),
        label: describe(element),
        frame_url: frameUrl(),
      });
    },
    true
  );

  document.addEventListener(
    "change",
    (event) => {
      const element = event.target;
      if (!element || !element.tagName) return;
      const tag = element.tagName.toLowerCase();
      const type = (element.getAttribute("type") || "").toLowerCase();

      if (tag === "select") {
        const option = element.options[element.selectedIndex];
        send({
          action: "select",
          selectors: buildSelectors(element),
          value: element.value,
          label: `choisir « ${option ? option.text : element.value} »`,
          frame_url: frameUrl(),
        });
        return;
      }

      if (tag === "input" && ["checkbox", "radio"].includes(type)) {
        send({
          action: "check",
          selectors: buildSelectors(element),
          value: element.checked ? "true" : "false",
          label: describe(element),
          frame_url: frameUrl(),
        });
        return;
      }

      if (tag === "input" || tag === "textarea") {
        /* Le mot de passe ne quitte jamais la page : seul un marqueur est transmis. */
        const secret = type === "password";
        valeursConnues.set(element, element.value);
        send({
          action: "fill",
          selectors: buildSelectors(element),
          value: secret ? null : element.value,
          secret: secret,
          input_type: type,
          label: describe(element),
          frame_url: frameUrl(),
        });
      }
    },
    true
  );

  /* Un calendrier écrit la date dans le champ sans déclencher « change » : le
     geste serait alors perdu, et seuls resteraient les clics sur les cases. Or
     un clic sur une case ne désigne pas une date, mais une position dans le
     mois affiché — rejoué, il choisit un autre jour. C'est ce qui a livré une
     période d'un seul jour là où l'utilisateur en avait choisi trente.

     On capte donc l'écriture elle-même, de deux façons complémentaires :
     l'affectation `champ.value = …`, interceptée au moment où elle a lieu, et
     un relevé périodique qui rattrape les autres manières de changer un champ.
     La capture est volontairement étroite — seule une valeur qui ressemble à
     une date est retenue — pour qu'un champ recalculé par le portail
     n'encombre pas le parcours. */
  const signalerUneDate = (element, valeur) => {
    if (!ressembleAUneDate(valeur)) return;
    const type = (element.getAttribute("type") || "text").toLowerCase();
    if (type === "password" || !document.contains(element)) return;
    valeursConnues.set(element, valeur);
    send({
      action: "fill",
      selectors: buildSelectors(element),
      value: valeur,
      secret: false,
      input_type: type,
      label: describe(element),
      frame_url: frameUrl(),
    });
  };

  /* Le prototype est modifié avant que la page n'exécute le moindre script :
     aucune écriture ne peut donc passer inaperçue faute d'avoir eu lieu trop
     tôt. La frappe au clavier, elle, ne passe pas par cet accesseur — elle
     reste captée par « change », sans double enregistrement. */
  const accesseur = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
  if (accesseur && accesseur.set) {
    Object.defineProperty(HTMLInputElement.prototype, "value", {
      configurable: true,
      enumerable: accesseur.enumerable,
      get: function () {
        return accesseur.get.call(this);
      },
      set: function (nouvelle) {
        const ancienne = accesseur.get.call(this);
        accesseur.set.call(this, nouvelle);
        if (String(ancienne) !== String(nouvelle)) {
          try {
            signalerUneDate(this, String(nouvelle));
          } catch (erreur) {
            /* Jamais au détriment de la page de l'utilisateur. */
          }
        }
      },
    });
  }

  /* Le filet : un champ modifié autrement — par son attribut, par un composant
     qui court-circuite l'accesseur — finit par être vu ici. Les valeurs de
     départ sont mémorisées dès que le document existe, faute de quoi une
     modification survenue avant le premier relevé passerait pour l'état
     initial et ne serait jamais signalée. */
  const memoriserLesValeurs = () => {
    for (const element of champsDeTexte()) {
      if (!valeursConnues.has(element)) valeursConnues.set(element, element.value);
    }
  };

  const releverLesDatesEcritesParLaPage = () => {
    for (const element of champsDeTexte()) {
      if (element === document.activeElement) continue; /* saisie en cours */
      const connue = valeursConnues.get(element);
      if (connue === undefined) {
        valeursConnues.set(element, element.value);
        continue;
      }
      if (element.value === connue) continue;
      valeursConnues.set(element, element.value);
      signalerUneDate(element, element.value);
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", memoriserLesValeurs, true);
  } else {
    memoriserLesValeurs();
  }
  setInterval(releverLesDatesEcritesParLaPage, 250);

  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key !== "Enter") return;
      const element = event.target;
      if (!element || !element.tagName) return;
      send({
        action: "press",
        selectors: buildSelectors(element),
        value: "Enter",
        label: "valider au clavier",
        frame_url: frameUrl(),
      });
    },
    true
  );
})();
