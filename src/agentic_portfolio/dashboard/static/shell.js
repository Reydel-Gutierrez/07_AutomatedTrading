(() => {
  const key = "agentic-sidebar-collapsed";
  const btn = document.getElementById("sidebar-toggle");
  if (btn) {
    if (localStorage.getItem(key) === "1") document.body.classList.add("sidebar-collapsed");
    btn.addEventListener("click", () => {
      document.body.classList.toggle("sidebar-collapsed");
      localStorage.setItem(key, document.body.classList.contains("sidebar-collapsed") ? "1" : "0");
    });
  }

  const tabs = document.querySelectorAll(".page-tabs [data-tab]");
  const panels = document.querySelectorAll(".tab-panel[data-panel]");

  function showTab(id) {
    tabs.forEach((tab) => {
      const on = tab.getAttribute("data-tab") === id;
      tab.classList.toggle("on", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
    });
    panels.forEach((panel) => {
      panel.hidden = panel.getAttribute("data-panel") !== id;
    });
    window.dispatchEvent(new Event("resize"));
  }

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => showTab(tab.getAttribute("data-tab") || "overview"));
  });
  if (window.location.hash === "#discovery") showTab("discovery");

  let expanded = null;
  let placeholder = null;
  let backdrop = null;

  function closeExpanded() {
    if (!expanded) return;
    expanded.classList.remove("is-expanded");
    if (placeholder && placeholder.parentNode) {
      placeholder.parentNode.insertBefore(expanded, placeholder);
      placeholder.remove();
    }
    if (backdrop) backdrop.remove();
    document.body.classList.remove("card-modal-open");
    expanded = null;
    placeholder = null;
    backdrop = null;
    window.dispatchEvent(new Event("resize"));
  }

  function openExpanded(card) {
    if (expanded === card) {
      closeExpanded();
      return;
    }
    closeExpanded();
    placeholder = document.createElement("div");
    placeholder.className = "card-placeholder";
    placeholder.style.height = `${card.getBoundingClientRect().height}px`;
    card.parentNode.insertBefore(placeholder, card);
    backdrop = document.createElement("div");
    backdrop.className = "card-backdrop";
    backdrop.addEventListener("click", closeExpanded);
    document.body.appendChild(backdrop);
    document.body.appendChild(card);
    card.classList.add("is-expanded");
    document.body.classList.add("card-modal-open");
    expanded = card;
    window.dispatchEvent(new Event("resize"));
  }

  document.querySelectorAll("article.card, article.kpi").forEach((card) => {
    if (card.closest(".login-box")) return;
    card.classList.add("can-expand");
    const expand = document.createElement("button");
    expand.type = "button";
    expand.className = "expand-card";
    expand.setAttribute("aria-label", "Expand card");
    expand.textContent = "⤢";
    card.appendChild(expand);
  });

  document.addEventListener("click", (event) => {
    const expandBtn = event.target.closest(".expand-card");
    if (expandBtn) {
      event.preventDefault();
      event.stopPropagation();
      const card = expandBtn.closest("article.card, article.kpi");
      if (card) openExpanded(card);
      return;
    }
    const card = event.target.closest("article.card, article.kpi");
    if (!card || card.closest(".login-box")) return;
    if (card.classList.contains("is-expanded")) return;
    if (event.target.closest("a, button, input, select, textarea, label, form, .ranges, .page-tabs")) return;
    openExpanded(card);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeExpanded();
  });
})();
