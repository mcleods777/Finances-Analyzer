(function () {
  "use strict";

  const state = {
    horizon: 90,
    data: null,
    visibleMonth: null, // Date, first-of-month, in the currently rendered calendar
  };

  const horizonSelector = document.getElementById("horizon-selector");
  const startingBalanceEl = document.getElementById("starting-balance-value");
  const minBalanceCard = document.getElementById("min-balance-card");
  const minBalanceEl = document.getElementById("min-balance-value");
  const bodyEl = document.getElementById("forecast-body");

  function formatCurrency(value) {
    const num = Number(value) || 0;
    const sign = num < 0 ? "-" : "";
    return `${sign}$${Math.abs(num).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;
  }

  function parseISODate(iso) {
    return new Date(`${iso}T00:00:00`);
  }

  function toISO(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, "0");
    const d = String(date.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
  }

  function formatDateLabel(iso) {
    return parseISODate(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }

  async function loadForecast(horizon) {
    bodyEl.innerHTML = '<div class="forecast-loading">Loading forecast…</div>';
    try {
      const res = await fetch(`/api/forecast?horizon=${horizon}`);
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      const json = await res.json();
      state.data = json;

      if (!json.days || json.days.length === 0) {
        bodyEl.innerHTML = '<div class="forecast-empty">No forecast data available yet.</div>';
        return;
      }

      const firstDay = parseISODate(json.days[0].date);
      state.visibleMonth = new Date(firstDay.getFullYear(), firstDay.getMonth(), 1);

      renderSummaryCards();
      renderBody();
    } catch (err) {
      bodyEl.innerHTML = '<div class="forecast-empty">Failed to load forecast.</div>';
      console.error("Forecast load failed:", err);
    }
  }

  function renderSummaryCards() {
    const d = state.data;
    startingBalanceEl.textContent = formatCurrency(d.starting_balance);

    const w = d.warnings || {};
    minBalanceCard.classList.toggle("warning", !!w.goes_negative);
    minBalanceEl.textContent = `${formatCurrency(w.min_balance)} on ${formatDateLabel(w.min_balance_date)}`;
  }

  function renderBody() {
    bodyEl.innerHTML = "";
    bodyEl.appendChild(buildCalendarNav());
    bodyEl.appendChild(buildCalendarGrid());
    bodyEl.appendChild(buildMonthlySummaryStrip());
  }

  function dayMapByDate() {
    const map = {};
    for (const day of state.data.days) {
      map[day.date] = day;
    }
    return map;
  }

  function horizonBounds() {
    const days = state.data.days;
    return {
      min: parseISODate(days[0].date),
      max: parseISODate(days[days.length - 1].date),
    };
  }

  function buildCalendarNav() {
    const nav = document.createElement("div");
    nav.className = "calendar-nav";

    const prevBtn = document.createElement("button");
    prevBtn.textContent = "‹ Prev";
    prevBtn.type = "button";

    const nextBtn = document.createElement("button");
    nextBtn.textContent = "Next ›";
    nextBtn.type = "button";

    const bounds = horizonBounds();
    const prevMonth = new Date(state.visibleMonth.getFullYear(), state.visibleMonth.getMonth() - 1, 1);
    const nextMonth = new Date(state.visibleMonth.getFullYear(), state.visibleMonth.getMonth() + 1, 1);
    const prevMonthEnd = new Date(prevMonth.getFullYear(), prevMonth.getMonth() + 1, 0);

    prevBtn.disabled = prevMonthEnd < bounds.min;
    nextBtn.disabled = nextMonth > bounds.max;

    prevBtn.addEventListener("click", () => {
      if (prevBtn.disabled) return;
      state.visibleMonth = prevMonth;
      renderBody();
    });
    nextBtn.addEventListener("click", () => {
      if (nextBtn.disabled) return;
      state.visibleMonth = nextMonth;
      renderBody();
    });

    const label = document.createElement("h2");
    label.textContent = state.visibleMonth.toLocaleDateString("en-US", { month: "long", year: "numeric" });

    nav.appendChild(prevBtn);
    nav.appendChild(label);
    nav.appendChild(nextBtn);
    return nav;
  }

  function buildCalendarGrid() {
    const grid = document.createElement("div");
    grid.className = "calendar-grid";

    const weekdayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    for (const wd of weekdayNames) {
      const el = document.createElement("div");
      el.className = "calendar-weekday";
      el.textContent = wd;
      grid.appendChild(el);
    }

    const year = state.visibleMonth.getFullYear();
    const month = state.visibleMonth.getMonth();
    const firstOfMonth = new Date(year, month, 1);
    const startOffset = firstOfMonth.getDay(); // 0 = Sunday
    const gridStart = new Date(year, month, 1 - startOffset);

    const dayMap = dayMapByDate();
    const todayISO = toISO(new Date());

    for (let i = 0; i < 42; i++) {
      const cellDate = new Date(gridStart.getFullYear(), gridStart.getMonth(), gridStart.getDate() + i);
      const iso = toISO(cellDate);
      const dayData = dayMap[iso];

      const cell = document.createElement("div");
      cell.className = "calendar-day";
      if (cellDate.getMonth() !== month) cell.classList.add("outside-month");
      if (iso === todayISO) cell.classList.add("today");
      if (dayData && dayData.projected_balance < 0) cell.classList.add("negative");

      const numberEl = document.createElement("div");
      numberEl.className = "day-number";
      numberEl.textContent = String(cellDate.getDate());
      cell.appendChild(numberEl);

      if (dayData) {
        if (dayData.events.length > 0) {
          const eventsEl = document.createElement("div");
          eventsEl.className = "day-events";
          for (const ev of dayData.events) {
            const chip = document.createElement("div");
            chip.className = `event-chip ${ev.type}`;
            chip.textContent = `${ev.name} ${formatCurrency(ev.amount)}`;
            chip.title = `${ev.name}: ${formatCurrency(ev.amount)}`;
            eventsEl.appendChild(chip);
          }
          cell.appendChild(eventsEl);
        }

        const balEl = document.createElement("div");
        balEl.className = "day-balance";
        balEl.textContent = formatCurrency(dayData.projected_balance);
        cell.appendChild(balEl);
      }

      grid.appendChild(cell);
    }

    return grid;
  }

  function buildMonthlySummaryStrip() {
    const wrapper = document.createElement("div");
    wrapper.className = "monthly-summary-strip";

    for (const m of state.data.monthly) {
      const item = document.createElement("div");
      item.className = "monthly-summary-item";

      const label = document.createElement("div");
      label.className = "month-label";
      label.textContent = parseISODate(`${m.month}-01`).toLocaleDateString("en-US", {
        month: "short",
        year: "numeric",
      });

      const net = document.createElement("div");
      net.className = `month-net ${m.projected_net >= 0 ? "positive" : "negative"}`;
      net.textContent = formatCurrency(m.projected_net);

      const detail = document.createElement("div");
      detail.className = "month-detail";
      detail.textContent = `In ${formatCurrency(m.projected_in)} · Out ${formatCurrency(m.projected_out)}`;

      item.appendChild(label);
      item.appendChild(net);
      item.appendChild(detail);
      wrapper.appendChild(item);
    }

    return wrapper;
  }

  horizonSelector.addEventListener("click", (e) => {
    const btn = e.target.closest(".horizon-btn");
    if (!btn) return;

    state.horizon = parseInt(btn.dataset.horizon, 10);
    for (const b of horizonSelector.querySelectorAll(".horizon-btn")) {
      b.classList.toggle("active", b === btn);
    }
    loadForecast(state.horizon);
  });

  loadForecast(state.horizon);
})();
