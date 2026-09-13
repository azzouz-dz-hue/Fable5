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
