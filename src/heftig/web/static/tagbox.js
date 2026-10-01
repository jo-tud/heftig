"use strict";
// Tags as chips (document page): each tag a small pill with ×, new ones typed at the end and
// finished with Enter or a comma, or picked from the suggestions - so the tags cannot be
// mistaken for the text fields next to them. The comma-separated input stays what the form
// sends (and is shown as it is without JavaScript).
(function () {
  const split = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);
  document.querySelectorAll("input[data-tagbox]").forEach((input) => {
    const box = document.createElement("div");
    box.className = "tagbox";
    const entry = document.createElement("input");
    entry.className = "tagbox-entry";
    entry.id = input.id; // the label now points here
    input.removeAttribute("id");
    entry.setAttribute("list", input.getAttribute("list") || "");
    input.removeAttribute("list");
    entry.autocomplete = "off";
    entry.placeholder = t("Add tag …");
    input.type = "hidden";
    input.before(box);
    box.append(entry, input);

    let tags = split(input.value);
    const sync = () => { input.value = [...tags, entry.value.trim()].filter(Boolean).join(", "); };
    function render() {
      box.querySelectorAll(".tag").forEach((c) => c.remove());
      tags.forEach((name, i) => {
        const chip = document.createElement("span");
        chip.className = "tag";
        chip.textContent = name;
        const x = document.createElement("button");
        x.type = "button";
        x.className = "tag-x";
        x.dataset.i = String(i);
        x.textContent = "×";
        x.title = t("Remove tag");
        x.setAttribute("aria-label", t("Remove tag %(name)s", { name }));
        chip.append(x);
        entry.before(chip);
      });
    }
    function add(names) {
      for (const n of names.flatMap(split)) {
        if (!tags.some((x) => x.toLowerCase() === n.toLowerCase())) tags.push(n);
      }
      render();
    }
    render();

    entry.addEventListener("input", (e) => {
      if (!(e instanceof InputEvent) || e.inputType === "insertReplacementText") {
        // picked from the suggestions
        add([entry.value]);
        entry.value = "";
      } else if (entry.value.includes(",")) {
        const parts = entry.value.split(",");
        entry.value = parts.pop().trimStart();
        add(parts);
      }
      sync();
    });
    entry.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && entry.value.trim()) {
        e.preventDefault();
        e.stopPropagation(); // a tag finished, not the field (that is an Enter on an empty entry)
        add([entry.value]);
        entry.value = "";
        sync();
      } else if (e.key === "Backspace" && !entry.value && tags.length) {
        tags.pop();
        render();
        sync();
      }
    });
    // leaving the box: what is typed becomes a tag (before the form saves the field)
    box.addEventListener("focusout", (e) => {
      if ((e.relatedTarget && box.contains(e.relatedTarget)) || !entry.value.trim()) return;
      add([entry.value]);
      entry.value = "";
      sync();
    });
    // × keeps the focus where it is (a click must not end typing in the entry)
    box.addEventListener("mousedown", (e) => { if (e.target.closest(".tag-x")) e.preventDefault(); });
    box.addEventListener("click", (e) => {
      const x = e.target.closest(".tag-x");
      if (!x) { if (e.target === box) entry.focus(); return; }
      const fromKeyboard = document.activeElement === x;
      tags.splice(Number(x.dataset.i), 1);
      render();
      sync();
      if (fromKeyboard) entry.focus(); // go on in the box, saved when it is left
      else if (!box.contains(document.activeElement)) input.dispatchEvent(new Event("change", { bubbles: true })); // finished: save now
    });
    // the value set from outside (undo)
    input.addEventListener("input", () => { tags = split(input.value); entry.value = ""; render(); });
  });
})();
