(function () {
  const LIGHT_BG_TOKENS = [
    "#fff",
    "#ffffff",
    "white",
    "rgb(255, 255, 255)",
    "rgba(255, 255, 255",
    "#fefefe",
    "#fafafa",
    "#f8fafc",
    "#f4f5f7",
    "#f1f5f9",
    "#e5e7eb"
  ];

  const PENCIL_SVG =
    '<svg viewBox="0 0 24 24" aria-hidden="true">' +
    '<path d="M12 20h9"></path>' +
    '<path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"></path>' +
    "</svg>";

  const TRASH_SVG =
    '<svg viewBox="0 0 24 24" aria-hidden="true">' +
    '<path d="M3 6h18"></path>' +
    '<path d="M8 6V4h8v2"></path>' +
    '<path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path>' +
    '<path d="M10 11v6"></path>' +
    '<path d="M14 11v6"></path>' +
    "</svg>";

  const teamPaletteById = {};
  let isApplyingTheme = false;
  let lastAppliedColor = "";

  function isLightColor(val) {
    if (!val) return false;
    const v = String(val).toLowerCase().trim();
    return LIGHT_BG_TOKENS.some((token) => v.includes(token));
  }

  function extractPaletteColor(obj) {
    if (!obj || typeof obj !== "object") return null;
    return (
      obj.color ||
      obj.hex_color ||
      obj.palette_color ||
      obj.team_color ||
      obj.accent_color ||
      obj.primary_color ||
      null
    );
  }

  function registerTeamsList(teams) {
    if (!Array.isArray(teams)) return;
    teams.forEach((t) => {
      if (!t || typeof t !== "object") return;
      const col = extractPaletteColor(t);
      if (!col || isLightColor(col)) return;
      ["id", "team_id", "key", "slug", "name"].forEach((k) => {
        if (t[k] !== undefined && t[k] !== null) {
          teamPaletteById[String(t[k])] = col;
          teamPaletteById[String(t[k]).toLowerCase()] = col;
        }
      });
    });
  }

  function harvestFromAnyObject(data) {
    if (!data || typeof data !== "object") return;
    if (Array.isArray(data)) {
      registerTeamsList(data);
      return;
    }
    if (Array.isArray(data.teams)) {
      registerTeamsList(data.teams);
    }
  }

  function applyTeamPalette(rawColor) {
    if (!rawColor || typeof rawColor !== "string") return;
    const clean = rawColor.trim();
    if (!clean || isLightColor(clean) || clean === lastAppliedColor) return;
    isApplyingTheme = true;
    lastAppliedColor = clean;
    try {
      document.documentElement.style.setProperty("--team-color", clean);
      document.body.style.setProperty("--team-color", clean);
      if (!document.body.classList.contains("play-team-theme")) {
        document.body.classList.add("play-team-theme");
      }
    } finally {
      setTimeout(() => {
        isApplyingTheme = false;
      }, 0);
    }
  }

  function syncTeamSelectColor(selectEl) {
    if (isApplyingTheme) return;
    const sel = selectEl || document.getElementById("team-select");
    if (!sel || !sel.options || !sel.options.length) return;
    const opt = sel.options[sel.selectedIndex] || sel.options[0];
    if (!opt) return;

    const attrColor =
      opt.getAttribute("data-color") ||
      opt.getAttribute("data-palette-color") ||
      opt.getAttribute("data-team-color") ||
      opt.dataset.color ||
      opt.dataset.paletteColor ||
      opt.dataset.teamColor;

    if (attrColor && !isLightColor(attrColor)) {
      applyTeamPalette(attrColor);
      return;
    }

    const val = String(opt.value || "");
    const textClean = String(opt.textContent || "").replace(/\s*\([^)]*\)\s*$/, "").trim();
    const match =
      teamPaletteById[val] ||
      teamPaletteById[val.toLowerCase()] ||
      teamPaletteById[textClean] ||
      teamPaletteById[textClean.toLowerCase()];

    if (match) {
      applyTeamPalette(match);
    }
  }

  function transformEditDeleteButtons() {
    const btns = document.querySelectorAll("button.btn.btn-sm, a.btn.btn-sm, button.btn-sm");
    btns.forEach((btn) => {
      if (btn.dataset.iconDone === "1") return;
      const txt = (btn.textContent || "").trim().toLowerCase();
      if (/^изм\.?$/i.test(txt) || /^изменить$/i.test(txt) || /^ред\.?$/i.test(txt)) {
        btn.dataset.iconDone = "1";
        btn.innerHTML = PENCIL_SVG;
        btn.classList.add("btn", "btn-sm", "icon-action-btn", "icon-action-btn--edit");
        btn.setAttribute("data-action-icon", "edit");
        if (!btn.getAttribute("title")) btn.setAttribute("title", "Изменить");
        if (!btn.getAttribute("aria-label")) btn.setAttribute("aria-label", "Изменить");
      } else if (/^уд\.?$/i.test(txt) || /^удалить$/i.test(txt)) {
        btn.dataset.iconDone = "1";
        btn.innerHTML = TRASH_SVG;
        btn.classList.add("btn", "btn-sm", "icon-action-btn", "icon-action-btn--delete");
        btn.setAttribute("data-action-icon", "delete");
        if (!btn.getAttribute("title")) btn.setAttribute("title", "Удалить");
        if (!btn.getAttribute("aria-label")) btn.setAttribute("aria-label", "Удалить");
      }
    });
  }

  function ensureGeoBackground() {
    if (document.querySelector(".geo-bg-layer")) return;
    const layer = document.createElement("div");
    layer.className = "geo-bg-layer";
    layer.setAttribute("aria-hidden", "true");
    layer.innerHTML =
      '<span class="geo-shape geo-shape--diamond"></span>' +
      '<span class="geo-shape geo-shape--ring"></span>' +
      '<span class="geo-shape geo-shape--tri"></span>';
    document.body.prepend(layer);
  }

  function cleanTargetedBlocks() {
    const selectors = ".top-header, .team-line-item, .option-tile, .org-card-box, #quiz-ready-overlay, .quiz-full-overlay";
    document.querySelectorAll(selectors).forEach((el) => {
      if (el.closest(".geo-bg-layer")) return;
      const bg = el.style.backgroundColor || el.style.background || "";
      if (isLightColor(bg)) {
        el.style.backgroundColor = "";
        el.style.background = "";
      }
      if (isLightColor(el.style.color || "")) {
        el.style.color = "";
      }
    });
  }

  function convertAddFileButtons() {
    const candidates = document.querySelectorAll("button, label, a, input[type='button'], input[type='submit']");
    candidates.forEach((el) => {
      if (el.dataset.uploadConverted === "1") return;
      const rawText = (el.tagName === "INPUT" ? el.value : el.textContent || "").trim();
      if (/^добавить\s+файл$/i.test(rawText)) {
        el.dataset.uploadConverted = "1";
        el.classList.add("btn", "btn-sm", "quiz-upload-btn");
        if (el.tagName === "INPUT") {
          el.value = "Загрузить";
        } else {
          const nestedInput = el.querySelector("input[type='file']");
          el.textContent = "Загрузить";
          if (nestedInput) {
            el.appendChild(nestedInput);
          }
        }
      }
    });
  }

  function harvestTeamsFromGlobalsAndApi() {
    ["teams", "TEAMS", "initialTeams", "pageData", "state", "gameState", "adminState"].forEach((key) => {
      try {
        const candidate = window[key];
        if (candidate) harvestFromAnyObject(candidate);
      } catch (_) {}
    });

    if (typeof fetch === "function") {
      ["/api/teams", "/api/state"].forEach((url) => {
        fetch(url, { credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : null))
          .then((data) => {
            if (!data) return;
            harvestFromAnyObject(data);
            syncTeamSelectColor(document.getElementById("team-select"));
          })
          .catch(() => {});
      });
    }
  }

  function hookPlayTeamPalette() {
    const isPlayPage = window.location.pathname.indexOf("/play") === 0 || !!document.getElementById("team-select");
    if (!isPlayPage) return;

    harvestTeamsFromGlobalsAndApi();

    const selectEl = document.getElementById("team-select");
    if (selectEl) {
      syncTeamSelectColor(selectEl);
      selectEl.addEventListener("change", () => syncTeamSelectColor(selectEl));
      selectEl.addEventListener("input", () => syncTeamSelectColor(selectEl));
    }

    if (typeof window.renderTeamState === "function") {
      const origRender = window.renderTeamState;
      window.renderTeamState = function (state) {
        const res = origRender.apply(this, arguments);
        const s = state || {};
        harvestFromAnyObject(s);
        const t = s.team || {};
        const col =
          extractPaletteColor(t) ||
          extractPaletteColor(s) ||
          (t.id && teamPaletteById[String(t.id)]) ||
          (t.name && teamPaletteById[String(t.name)]);
        if (col) {
          applyTeamPalette(col);
        }
        return res;
      };
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    ensureGeoBackground();
    cleanTargetedBlocks();
    convertAddFileButtons();
    transformEditDeleteButtons();
    hookPlayTeamPalette();
  });
})();
