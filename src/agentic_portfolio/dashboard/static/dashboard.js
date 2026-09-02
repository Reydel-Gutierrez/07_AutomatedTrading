(() => {
  const SLEEVE_COLORS = {
    CASH: "#5b8c5a",
    CORE_GROWTH: "#4a7fb5",
    OPPORTUNISTIC: "#c4843c",
    TACTICAL: "#7a6bb5",
    SPECULATIVE: "#8a8680",
  };
  const RANGE_MS = {
    "1M": 30 * 86400000,
    "3M": 91 * 86400000,
    "6M": 182 * 86400000,
    "1Y": 365 * 86400000,
  };

  function readJson(id) {
    const node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || "null");
    } catch (err) {
      return null;
    }
  }

  const centerLabel = {
    id: "centerLabel",
    afterDraw(chart) {
      if (chart.config.type !== "doughnut") return;
      const { ctx, chartArea } = chart;
      const x = (chartArea.left + chartArea.right) / 2;
      const y = (chartArea.top + chartArea.bottom) / 2;
      ctx.save();
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillStyle = "#2c2a26";
      ctx.font = "700 18px Segoe UI, system-ui, sans-serif";
      ctx.fillText("100%", x, y - 8);
      ctx.fillStyle = "#6f6b63";
      ctx.font = "700 10px Segoe UI, system-ui, sans-serif";
      ctx.fillText("TOTAL", x, y + 10);
      ctx.restore();
    },
  };

  function drawAllocation() {
    const canvas = document.getElementById("allocation-chart");
    const data = readJson("allocation-data");
    if (!canvas || !data || typeof Chart === "undefined") return;
    const keys = data.keys || [];
    const colors = keys.map((key) => SLEEVE_COLORS[key] || "#8a8680");
    new Chart(canvas, {
      type: "doughnut",
      plugins: [centerLabel],
      data: {
        labels: data.labels || [],
        datasets: [
          {
            data: data.values || [],
            backgroundColor: colors,
            borderColor: "#fffcf7",
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        cutout: "68%",
      },
    });
  }

  function cutoff(range) {
    if (!range || range === "ALL") return null;
    if (range === "YTD") return Date.UTC(new Date().getUTCFullYear(), 0, 1);
    const span = RANGE_MS[range];
    return span ? Date.now() - span : null;
  }

  function slicePerformance(data, range) {
    const start = cutoff(range);
    const labels = data.labels || [];
    if (start == null) {
      return { labels, nav: data.nav || [], spy: data.spy || [] };
    }
    const nav = [];
    const spy = [];
    const kept = [];
    labels.forEach((label, i) => {
      const t = Date.parse(label);
      if (Number.isNaN(t) || t >= start) {
        kept.push(label);
        nav.push((data.nav || [])[i]);
        spy.push((data.spy || [])[i]);
      }
    });
    return { labels: kept, nav, spy };
  }

  function drawPerformance() {
    const canvas = document.getElementById("performance-chart");
    const data = readJson("performance-data");
    if (!canvas || !data || !data.ready || typeof Chart === "undefined") return;
    let chart;

    function render(range) {
      const sliced = slicePerformance(data, range);
      const datasets = [
        {
          label: "Portfolio (Paper Book)",
          data: sliced.nav,
          borderColor: "#4f6f52",
          backgroundColor: "rgba(79, 111, 82, 0.08)",
          fill: true,
          tension: 0.25,
          pointRadius: 2,
        },
      ];
      if (data.has_spy) {
        datasets.push({
          label: "SPY",
          data: sliced.spy,
          borderColor: "#c4843c",
          backgroundColor: "transparent",
          spanGaps: true,
          tension: 0.25,
          pointRadius: 2,
        });
      }
      const config = {
        type: "line",
        data: { labels: sliced.labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { labels: { color: "#6f6b63", boxWidth: 10 } },
          },
          scales: {
            x: {
              ticks: {
                color: "#6f6b63",
                maxRotation: 0,
                autoSkip: true,
                maxTicksLimit: 6,
                callback(value) {
                  const label = this.getLabelForValue(value);
                  const stamp = Date.parse(label);
                  if (Number.isNaN(stamp)) return label;
                  return new Intl.DateTimeFormat("en-US", {
                    timeZone: "America/New_York",
                    month: "short",
                    day: "numeric",
                  }).format(new Date(stamp));
                },
              },
              grid: { color: "rgba(221, 216, 206, 0.8)" },
            },
            y: {
              ticks: { color: "#6f6b63" },
              grid: { color: "rgba(221, 216, 206, 0.8)" },
            },
          },
        },
      };
      if (chart) {
        chart.data = config.data;
        chart.update();
        return;
      }
      chart = new Chart(canvas, config);
    }

    document.querySelectorAll(".ranges button").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".ranges button").forEach((other) => other.classList.remove("on"));
        btn.classList.add("on");
        render(btn.getAttribute("data-range") || "ALL");
      });
    });
    render("ALL");
  }

  drawAllocation();
  drawPerformance();
})();
