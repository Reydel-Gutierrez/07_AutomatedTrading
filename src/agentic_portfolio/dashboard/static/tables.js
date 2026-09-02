(() => {
  const DEFAULT_SIZE = 8;

  function rowsOf(table) {
    const body = table.tBodies[0];
    if (!body) return [];
    return [...body.rows].filter((row) => !row.classList.contains("empty") && row.getAttribute("data-empty") !== "1");
  }

  function filtered(rows) {
    return rows.filter((row) => row.getAttribute("data-filter-hide") !== "1");
  }

  function ensurePager(table) {
    const wrap = table.closest(".table-wrap") || table.parentElement;
    let pager = wrap.querySelector(":scope > .pager");
    if (!pager) {
      pager = document.createElement("div");
      pager.className = "pager";
      wrap.appendChild(pager);
    }
    return pager;
  }

  function paginateTable(table) {
    const size = Number(table.getAttribute("data-page-size") || DEFAULT_SIZE) || DEFAULT_SIZE;
    table._page = table._page || 1;

    function render() {
      const all = rowsOf(table);
      const visible = filtered(all);
      const total = visible.length;
      const pages = Math.max(1, Math.ceil(total / size) || 1);
      if (table._page > pages) table._page = pages;
      const start = (table._page - 1) * size;
      all.forEach((row) => {
        row.style.display = "none";
      });
      visible.forEach((row, index) => {
        row.style.display = index >= start && index < start + size ? "" : "none";
      });
      const empty = table.querySelector("tr.empty, tr[data-empty='1']");
      if (empty) empty.style.display = total ? "none" : "";

      const pager = ensurePager(table);
      pager.innerHTML = "";
      const maxButtons = 5;
      let from = Math.max(1, table._page - 2);
      let to = Math.min(pages, from + maxButtons - 1);
      from = Math.max(1, to - maxButtons + 1);
      for (let i = from; i <= to; i += 1) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = String(i);
        btn.classList.toggle("on", i === table._page);
        btn.addEventListener("click", () => {
          table._page = i;
          render();
        });
        pager.appendChild(btn);
      }
      const next = document.createElement("button");
      next.type = "button";
      next.textContent = "Next";
      next.disabled = table._page >= pages;
      next.addEventListener("click", () => {
        if (table._page < pages) {
          table._page += 1;
          render();
        }
      });
      pager.appendChild(next);
      const meta = document.createElement("span");
      meta.className = "meta";
      if (!total) {
        meta.textContent = "No rows";
      } else {
        const last = Math.min(start + size, total);
        meta.textContent = `Showing ${start + 1} to ${last} of ${total} results`;
      }
      pager.appendChild(meta);
    }

    table._renderPage = render;
    render();
  }

  function initFilters() {
    document.querySelectorAll("[data-filter-for]").forEach((nav) => {
      const table = document.querySelector(nav.getAttribute("data-filter-for"));
      if (!table) return;
      nav.querySelectorAll("[data-filter]").forEach((btn) => {
        btn.addEventListener("click", () => {
          nav.querySelectorAll("[data-filter]").forEach((other) => other.classList.toggle("on", other === btn));
          const wanted = btn.getAttribute("data-filter") || "all";
          rowsOf(table).forEach((row) => {
            const bucket = row.getAttribute("data-bucket") || "all";
            const show = wanted === "all" || wanted === "evaluated" || bucket === wanted;
            row.setAttribute("data-filter-hide", show ? "0" : "1");
          });
          table._page = 1;
          if (table._renderPage) table._renderPage();
        });
      });
    });
  }

  function initAutoRefresh() {
    const toggle = document.getElementById("auto-refresh");
    if (!toggle) return;
    const key = "agentic-auto-refresh";
    const saved = localStorage.getItem(key);
    toggle.checked = saved !== "0";
    let timer = null;
    function arm() {
      if (timer) window.clearInterval(timer);
      if (toggle.checked) {
        timer = window.setInterval(() => window.location.reload(), 30000);
      }
    }
    toggle.addEventListener("change", () => {
      localStorage.setItem(key, toggle.checked ? "1" : "0");
      arm();
    });
    arm();
  }

  document.querySelectorAll(".table-wrap table").forEach(paginateTable);
  initFilters();
  initAutoRefresh();
})();
