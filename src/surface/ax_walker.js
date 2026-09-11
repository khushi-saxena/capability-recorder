// Walks a frame's DOM and returns AX-style records.
// Boxes come back in frame-local CSS pixels; Python converts to top-level ratios.
(startHandle) => {
  const NAME_MAX = 120;

  const ROLE_BY_TAG = {
    A: "link",
    BUTTON: "button",
    SELECT: "combobox",
    TEXTAREA: "textbox",
    OPTION: "option",
    TH: "columnheader",
    TD: "cell",
    H1: "heading", H2: "heading", H3: "heading",
    H4: "heading", H5: "heading", H6: "heading",
    FORM: null,
  };

  const INPUT_ROLE = {
    text: "textbox", password: "textbox", search: "textbox",
    email: "textbox", tel: "textbox", number: "textbox",
    submit: "button", button: "button", reset: "button",
    checkbox: "checkbox", radio: "radio",
  };

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.trim();
    if (el.tagName === "INPUT") {
      return INPUT_ROLE[(el.type || "text").toLowerCase()] || "textbox";
    }
    return ROLE_BY_TAG[el.tagName] || null;
  }

  // Text belonging to this element, not to the layout hanging off it. A cell
  // that wraps a nested table still keeps its own prose — these pages put the
  // error banner in a <td> that also holds the next table down.
  function shallowText(el) {
    let out = "";
    for (const n of el.childNodes) {
      if (n.nodeType === 3) {
        out += n.nodeValue;
      } else if (n.nodeType === 1 && !n.matches("table, input, select, textarea") &&
                 !n.querySelector("table, input, select, textarea")) {
        out += " " + (n.textContent || "");
      }
    }
    return out.replace(/\s+/g, " ").trim();
  }

  // Own text only — a <td> wrapping a <font> counts, a <table> wrapping
  // fifty rows does not.
  function ownText(el) {
    if (el.tagName === "TD" || el.tagName === "TH" || el.tagName === "A" ||
        el.tagName === "BUTTON" || /^H[1-6]$/.test(el.tagName)) {
      if (el.querySelector("table, input, select, textarea")) return shallowText(el);
      return (el.textContent || "").replace(/\s+/g, " ").trim();
    }
    let out = "";
    for (const n of el.childNodes) {
      if (n.nodeType === 3) out += n.nodeValue;
    }
    return out.replace(/\s+/g, " ").trim();
  }

  function nameOf(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria.trim().slice(0, NAME_MAX);
    if (el.tagName === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      // submit/button carry their label in value; text inputs get no name here
      if (t === "submit" || t === "button" || t === "reset") {
        return (el.value || "").trim().slice(0, NAME_MAX);
      }
      return "";
    }
    const alt = el.getAttribute("alt") || el.getAttribute("title");
    if (alt) return alt.trim().slice(0, NAME_MAX);
    return ownText(el).slice(0, NAME_MAX);
  }

  function valueOf(el) {
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") return el.value;
    if (el.tagName === "SELECT") {
      const o = el.options[el.selectedIndex];
      return o ? o.value : "";
    }
    return null;
  }

  // Stable-ish table id: position among tables in this document.
  const tables = Array.from(document.querySelectorAll("table"));
  function tableInfo(el) {
    if (el.tagName !== "TD" && el.tagName !== "TH") return [null, null, null];
    const cell = el;
    const row = cell.parentElement;
    if (!row || row.tagName !== "TR") return [null, null, null];
    let table = row;
    while (table && table.tagName !== "TABLE") table = table.parentElement;
    if (!table) return [null, null, null];
    const tIdx = tables.indexOf(table);
    const rows = Array.from(table.rows);
    const rIdx = rows.indexOf(row);
    const cIdx = Array.from(row.cells).indexOf(cell);
    return ["t" + tIdx, rIdx, cIdx];
  }

  const out = [];
  let handle = startHandle;

  const walker = document.createTreeWalker(
    document.body || document.documentElement,
    NodeFilter.SHOW_ELEMENT
  );

  let el = document.body ? document.body : null;
  while (el) {
    const role = roleOf(el);
    if (role) {
      const r = el.getBoundingClientRect();
      const visible = r.width > 0 && r.height > 0 &&
        (el.offsetParent !== null || getComputedStyle(el).position === "fixed");
      if (visible) {
        const name = nameOf(el);
        const value = valueOf(el);
        const [tid, row, col] = tableInfo(el);
        // Drop empty structural cells that carry nothing useful.
        const empty = !name && value === null && tid === null;
        if (!empty) {
          el.setAttribute("data-ax-h", String(handle));
          out.push({
            handle: handle,
            role: role,
            name: name,
            value: value,
            enabled: !el.disabled,
            box: { x: r.left, y: r.top, w: r.width, h: r.height },
            table_id: tid,
            row: row,
            col: col,
          });
          handle += 1;
        }
      }
    }
    el = walker.nextNode();
  }

  return { nodes: out, next: handle };
}
