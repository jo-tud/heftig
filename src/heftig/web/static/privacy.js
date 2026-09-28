"use strict";
/*
 * Privacy mode: blurs amounts, account/contract numbers, IBANs, card numbers and values after
 * words like "Passwort"/"PIN" in text, plus whole images/PDF previews. Protection against
 * shoulder surfing only - the data is still in the page. Tap a blurred item to reveal it.
 */
(function () {
  const root = document.documentElement;
  const KEY = "heftig-privacy";

  // [regex, group] - group 0 blurs the whole match, n blurs only that capture group
  const PATTERNS = [
    // amounts with currency: "1.234,56 EUR", "€ 12,40", "39,95€"
    [/(?:€|EUR)\s?-?\d{1,3}(?:[.  ]\d{3})*,\d{2}|-?\d{1,3}(?:[.  ]\d{3})*,\d{2}\s?(?:€|EUR|Euro)\b/g, 0],
    // amounts after typical keywords without currency
    [/(?:Betrag|Summe|Gesamt|Saldo|Kontostand|Guthaben|Gehalt|Brutto|Netto|Beitrag|Abschlag|Zahlbetrag)[^\d\n]{0,25}(-?\d{1,3}(?:[.  ]\d{3})*,\d{2})/gi, 1],
    // IBAN
    [/\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,4})?\b/g, 0],
    // card numbers
    [/\b(?:\d{4}[ -]?){3}\d{4}\b/g, 0],
    // identifiers after their label
    [/(?:Vertrags|Kunden|Versicherten|Versicherungs|Mitglieds|Personal|Konto|Rechnungs|Steuer|Aktenzeichen|Policen|Zähler|Referenz)[-‑]?(?:nummer|nr\.?|-?Nr\.?|ID|zeichen)?\s*[:.#]?\s*([A-Z0-9][A-Z0-9 ./-]{3,30}\d)/gi, 1],
    [/\bSteuer-?ID\s*[:.]?\s*(\d{2}\s?\d{3}\s?\d{3}\s?\d{3})\b/gi, 1],
    // secrets after keywords
    [/\b(?:Passwort|Kennwort|Password|Passphrase|PIN|PUK|TAN|Zugangscode|Zugangsdaten|Benutzerkennung|Login|Benutzername)\s*[:=]\s*(\S{3,})/gi, 1],
  ];

  function knownValues() {
    const el = document.getElementById("pv-values");
    if (!el) return [];
    try {
      return JSON.parse(el.textContent).filter((v) => typeof v === "string" && v.trim().length >= 3);
    } catch (e) {
      return [];
    }
  }

  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  function ranges(text, extra) {
    const out = [];
    for (const [re, group] of PATTERNS.concat(extra)) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(text))) {
        if (!m[0]) { re.lastIndex++; continue; }
        if (group && m[group]) {
          const start = m.index + m[0].lastIndexOf(m[group]);
          out.push([start, start + m[group].length]);
        } else if (!group) {
          out.push([m.index, m.index + m[0].length]);
        }
      }
    }
    out.sort((a, b) => a[0] - b[0]);
    const merged = [];
    for (const r of out) {
      const last = merged[merged.length - 1];
      if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
      else merged.push([r[0], r[1]]);
    }
    return merged;
  }

  function wrapTextNode(node, extra) {
    const text = node.nodeValue;
    const rs = ranges(text, extra);
    if (!rs.length) return;
    const frag = document.createDocumentFragment();
    let pos = 0;
    for (const [a, b] of rs) {
      if (a > pos) frag.appendChild(document.createTextNode(text.slice(pos, a)));
      const span = document.createElement("span");
      span.className = "pv";
      span.textContent = text.slice(a, b);
      frag.appendChild(span);
      pos = b;
    }
    if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
    node.parentNode.replaceChild(frag, node);
  }

  function scan(container, extra) {
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, {
      acceptNode: (n) => (n.parentElement && n.parentElement.closest(".pv") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach((n) => wrapTextNode(n, extra));
  }

  function extraPatterns() {
    return knownValues().map((v) => {
      // allow any separators between the characters of a known number: "8372 9381" / "83729381"
      const compact = v.replace(/[\s./-]/g, "");
      const body = /^\w+$/.test(compact) && compact.length >= 4
        ? compact.split("").map(esc).join("[\\s./-]?")
        : esc(v);
      return [new RegExp(body, "gi"), 0];
    });
  }

  function setMode(on) {
    root.classList.toggle("privacy", on);
    try { localStorage.setItem(KEY, on ? "on" : "off"); } catch (e) { /* ignore */ }
    const b = document.getElementById("privacy-toggle");
    if (b) {
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.textContent = on ? t("Private: on") : t("Private: off");
      b.title = on ? t("Sensitive data is hidden (Shift+P)") : t("All data visible (Shift+P)");
    }
    if (on) document.querySelectorAll(".pv-open").forEach((el) => el.classList.remove("pv-open"));
  }

  document.addEventListener("DOMContentLoaded", () => {
    const extra = extraPatterns();
    document.querySelectorAll("[data-pv]").forEach((el) => scan(el, extra));
    setMode(root.classList.contains("privacy"));
    const b = document.getElementById("privacy-toggle");
    if (b) b.addEventListener("click", () => setMode(!root.classList.contains("privacy")));
    document.addEventListener("keydown", (e) => {
      if (e.key === "P" && e.shiftKey && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
        setMode(!root.classList.contains("privacy"));
      }
    });
    // tap to reveal a single item
    document.addEventListener("click", (e) => {
      if (!root.classList.contains("privacy")) return;
      // a blurred page viewer: the whole area reveals on click (the label lets the mouse
      // wheel and dragging through; its button stays for keyboard users)
      const hide = e.target.closest(".pv-hide");
      if (hide) {
        e.preventDefault();
        hide.closest(".pv-media").classList.remove("pv-open");
        return;
      }
      const media = e.target.closest(".pv-media");
      if (media && !media.classList.contains("pv-open")) {
        const v = media.querySelector(".viewer");
        if (v && performance.now() - Number(v.dataset.dragEnd || 0) < 500) return; // end of a drag
        e.preventDefault();
        media.classList.add("pv-open");
        return;
      }
      const item = e.target.closest(".pv");
      if (item && !item.classList.contains("pv-open")) {
        e.preventDefault();
        item.classList.add("pv-open");
      }
    }, true);
  });

  window.heftigPrivacy = { ranges, setMode };
})();
