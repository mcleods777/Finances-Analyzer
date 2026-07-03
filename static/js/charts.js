// Chart color palette — Private Wire (brass / ledger green / red ink + earth tones).
// Canvas can't resolve CSS variables, so these are the literal token values.
const COLORS = {
    primary: '#D9A03F',                       // brass
    primaryFill: 'rgba(217, 160, 63, 0.15)',
    green: 'rgba(124, 175, 126, 0.7)',        // ledger green
    red: 'rgba(206, 106, 87, 0.7)',           // red ink
    yellow: '#D9A03F',                        // average marker -> brass
    purple: '#97907E',                        // rolling average -> muted stone
    grid: '#26231F',                          // near var(--rule), quieter on ink
    text: '#97907E',                          // muted
};

// Account line colors for net worth breakdown — earthy, desaturated
const ACCOUNT_COLORS = [
    '#D9A03F', '#7CAF7E', '#CE6A57', '#B8A276', '#8A6F3B', '#5F7A61',
];

function formatCurrency(value) {
    if (value < 0) {
        return '-$' + Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    }
    return '$' + value.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

// Polished account line colors — derived earth palette, no neon
const ACCOUNT_LINE_COLORS = [
    '#D9A03F', '#7CAF7E', '#CE6A57', '#B8A276', '#8A6F3B', '#5F7A61',
    '#A98467', '#6B5A34', '#97907E', '#C2B49A',
];

let netWorthChartInstance = null;

/**
 * Render the net worth chart.
 *
 * `data` is the /api/net-worth response shape:
 *   { labels, net_worth, assets, liabilities,
 *     accounts: [{ name, type, is_liability, data }], stats }
 *
 * Dataset 0 is the Net Worth line (gradient-filled, colored by trend).
 * Datasets 1-2 are Assets / Liabilities bands (liabilities plotted as a
 * negative-valued area so it reads as a band below the x-axis).
 * Remaining datasets are per-account lines, hidden by default and
 * toggleable via the custom legend (liability accounts plotted negative
 * to match the Liabilities band).
 */
function renderNetWorthChart(data) {
    const canvas = document.getElementById('netWorthChart');
    if (!canvas || !data.labels || !data.labels.length) return;

    if (netWorthChartInstance) {
        netWorthChartInstance.destroy();
        netWorthChartInstance = null;
    }

    const netWorth = data.net_worth || [];
    const assets = data.assets || [];
    const liabilities = (data.liabilities || []).map(v => -Math.abs(v));

    const latestValue = netWorth[netWorth.length - 1];
    const earliestValue = netWorth[0];
    const isPositiveTrend = latestValue >= earliestValue;
    const trendColor = isPositiveTrend ? '#7CAF7E' : '#CE6A57';

    const datasets = [{
        label: 'Net Worth',
        data: netWorth,
        borderColor: trendColor,
        backgroundColor: 'transparent',
        fill: true,
        tension: 0.35,
        pointRadius: 0,
        pointHoverRadius: 6,
        pointHoverBackgroundColor: trendColor,
        pointHoverBorderColor: '#EDE8DF',
        pointHoverBorderWidth: 2,
        pointHitRadius: 20,
        borderWidth: 2.5,
        order: 0,
    }];

    if (assets.length) {
        datasets.push({
            label: 'Assets',
            data: assets,
            borderColor: 'rgba(124, 175, 126, 0.85)',
            backgroundColor: 'rgba(124, 175, 126, 0.08)',
            borderWidth: 1.5,
            fill: 'origin',
            tension: 0.3,
            pointRadius: 0,
            pointHitRadius: 15,
            order: 2,
        });
    }

    if (liabilities.length) {
        datasets.push({
            label: 'Liabilities',
            data: liabilities,
            borderColor: 'rgba(206, 106, 87, 0.85)',
            backgroundColor: 'rgba(206, 106, 87, 0.08)',
            borderWidth: 1.5,
            fill: 'origin',
            tension: 0.3,
            pointRadius: 0,
            pointHitRadius: 15,
            order: 2,
        });
    }

    // Per-account lines (hidden by default), toggleable via legend
    const accounts = data.accounts || [];
    accounts.forEach((acct, i) => {
        const color = ACCOUNT_LINE_COLORS[i % ACCOUNT_LINE_COLORS.length];
        datasets.push({
            label: acct.name,
            data: acct.data,
            borderColor: color,
            backgroundColor: color + '11',
            borderWidth: 1.8,
            borderDash: [5, 4],
            fill: false,
            tension: 0.35,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: color,
            pointHoverBorderColor: '#EDE8DF',
            pointHoverBorderWidth: 2,
            pointHitRadius: 15,
            hidden: true,
            order: 3,
        });
    });

    let mainGradient = null;

    // Use let so the filter closure can reference the chart after construction
    let nwChart = null;

    nwChart = new Chart(canvas, {
        type: 'line',
        data: { labels: data.labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            scales: {
                x: {
                    type: 'time',
                    time: { unit: 'month', displayFormats: { month: 'MMM yyyy' } },
                    grid: {
                        color: 'rgba(151, 144, 126, 0.08)',
                        drawTicks: false,
                    },
                    ticks: {
                        color: '#97907E',
                        maxTicksLimit: 12,
                        padding: 8,
                        font: { size: 11 },
                    },
                    border: { display: false },
                },
                y: {
                    grid: {
                        color: 'rgba(151, 144, 126, 0.08)',
                        drawTicks: false,
                    },
                    ticks: {
                        color: '#97907E',
                        callback: value => formatCurrency(value),
                        padding: 8,
                        font: { size: 11 },
                        maxTicksLimit: 6,
                    },
                    border: { display: false },
                },
            },
            plugins: {
                legend: { display: false }, // We use custom legend
                tooltip: {
                    backgroundColor: 'rgba(36, 32, 25, 0.96)',
                    titleColor: '#EDE8DF',
                    bodyColor: '#EDE8DF',
                    borderColor: '#33302A',
                    borderWidth: 1,
                    cornerRadius: 2,
                    padding: 12,
                    displayColors: true,
                    boxPadding: 6,
                    titleFont: { weight: '600', size: 13 },
                    bodyFont: { size: 12 },
                    callbacks: {
                        label: ctx => {
                            let name = ctx.dataset.label;
                            const idMatch = name.match(/[\s_-]+\d{6,}/);
                            if (idMatch) name = name.substring(0, idMatch.index);
                            const val = ['Liabilities'].includes(ctx.dataset.label)
                                ? Math.abs(ctx.parsed.y)
                                : ctx.parsed.y;
                            return ' ' + name + ':  ' + formatCurrency(val);
                        },
                    },
                    filter: (item) => {
                        // Use the closure-captured nwChart to check runtime visibility
                        if (!nwChart) return true;
                        const meta = nwChart.getDatasetMeta(item.datasetIndex);
                        const isHidden = meta.hidden === null ? !!nwChart.data.datasets[item.datasetIndex].hidden : meta.hidden;
                        return !isHidden;
                    },
                },
            },
        },
        plugins: [{
            id: 'netWorthGradient',
            beforeDatasetsDraw(chart) {
                const { ctx, chartArea } = chart;
                if (!chartArea) return;

                if (!mainGradient || mainGradient._h !== chartArea.bottom - chartArea.top) {
                    mainGradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
                    if (isPositiveTrend) {
                        mainGradient.addColorStop(0, 'rgba(124, 175, 126, 0.25)');
                        mainGradient.addColorStop(0.5, 'rgba(124, 175, 126, 0.06)');
                        mainGradient.addColorStop(1, 'rgba(124, 175, 126, 0)');
                    } else {
                        mainGradient.addColorStop(0, 'rgba(206, 106, 87, 0.25)');
                        mainGradient.addColorStop(0.5, 'rgba(206, 106, 87, 0.06)');
                        mainGradient.addColorStop(1, 'rgba(206, 106, 87, 0)');
                    }
                    mainGradient._h = chartArea.bottom - chartArea.top;
                    chart.data.datasets[0].backgroundColor = mainGradient;
                }
            }
        },
        {
            id: 'crosshair',
            afterDatasetsDraw(chart) {
                const { ctx, tooltip, chartArea } = chart;
                if (!tooltip || !tooltip.opacity || !chartArea) return;

                const x = tooltip.caretX;
                ctx.save();
                ctx.beginPath();
                ctx.moveTo(x, chartArea.top);
                ctx.lineTo(x, chartArea.bottom);
                ctx.lineWidth = 1;
                ctx.strokeStyle = 'rgba(151, 144, 126, 0.3)';
                ctx.setLineDash([4, 4]);
                ctx.stroke();
                ctx.restore();
            }
        }]
    });

    netWorthChartInstance = nwChart;

    // Build custom HTML legend with toggle pills
    buildNetWorthLegend(nwChart);
    updateNetWorthHeadline(data.stats);
}

function buildNetWorthLegend(chart) {
    const container = document.getElementById('netWorthLegend');
    if (!container) return;
    container.innerHTML = '';

    chart.data.datasets.forEach((ds, i) => {
        const color = ds.borderColor;

        // Get the latest (most recent) value for this dataset. Liabilities
        // (and liability accounts) are plotted negative for the band visual —
        // show the legend value as a positive debt magnitude.
        const rawVal = ds.data[ds.data.length - 1];
        const latestVal = ds.label === 'Liabilities' ? Math.abs(rawVal) : rawVal;

        // Clean up label: remove long account IDs after name
        let displayName = ds.label;
        const idMatch = displayName.match(/[\s_-]+\d{6,}/);
        if (idMatch) {
            displayName = displayName.substring(0, idMatch.index);
        }

        const item = document.createElement('div');
        item.className = 'nw-legend-item';
        item.classList.add(ds.hidden ? 'inactive' : 'active');
        item.style.setProperty('--legend-color', color + '55');
        item.style.setProperty('--legend-bg', color + '15');

        item.innerHTML = `
            <span class="nw-legend-dot" style="background:${color};box-shadow:0 0 6px ${color}66;"></span>
            <span class="nw-legend-name">${displayName}</span>
            <span class="nw-legend-value">${formatCurrency(latestVal)}</span>
        `;

        item.addEventListener('click', () => {
            const meta = chart.getDatasetMeta(i);
            // Chart.js uses null to mean "use dataset config default"
            // If hidden is null, the dataset config's hidden:true is in effect => currently hidden
            // If hidden is true => currently hidden
            // If hidden is false => currently visible
            const isCurrentlyHidden = meta.hidden === null ? !!chart.data.datasets[i].hidden : meta.hidden;
            meta.hidden = !isCurrentlyHidden;
            chart.update();

            // Update pill visual to match new state
            if (meta.hidden) {
                item.classList.remove('active');
                item.classList.add('inactive');
            } else {
                item.classList.remove('inactive');
                item.classList.add('active');
            }
        });

        container.appendChild(item);
    });
}

function updateNetWorthHeadline(stats) {
    if (!stats) return;

    const valueEl = document.getElementById('nw-headline-value');
    if (valueEl) valueEl.textContent = formatCurrency(stats.current_net_worth || 0);

    const assetsEl = document.getElementById('nw-assets-total');
    if (assetsEl) assetsEl.textContent = formatCurrency(stats.total_assets || 0);

    const liabilitiesEl = document.getElementById('nw-liabilities-total');
    if (liabilitiesEl) liabilitiesEl.textContent = formatCurrency(stats.total_liabilities || 0);

    const setChangePill = (id, change, pct, label) => {
        const el = document.getElementById(id);
        if (!el) return;
        const sign = change >= 0 ? '+' : '-';
        el.textContent = `${label} ${sign}${formatCurrency(Math.abs(change))} (${pct >= 0 ? '+' : ''}${pct}%)`;
        el.classList.remove('positive', 'negative');
        el.classList.add(change >= 0 ? 'positive' : 'negative');
    };

    setChangePill('nw-change-30d', stats.change_30d || 0, stats.change_30d_pct || 0, '30d');
    setChangePill('nw-change-90d', stats.change_90d || 0, stats.change_90d_pct || 0, '90d');
}

function renderBiweeklyChart(data) {
    const ctx = document.getElementById('biweeklySpendingChart');
    if (!ctx || !data.labels.length) return;

    const barColors = data.spending.map(val =>
        val > data.average ? COLORS.red : COLORS.green
    );

    const datasets = [
        {
            label: 'Biweekly Spending',
            data: data.spending,
            backgroundColor: barColors,
            borderRadius: 0,
            type: 'bar',
            order: 2,
        },
        {
            label: 'Average',
            data: Array(data.labels.length).fill(data.average),
            type: 'line',
            borderColor: COLORS.yellow,
            borderDash: [6, 4],
            borderWidth: 2,
            pointRadius: 0,
            fill: false,
            order: 1,
        },
    ];

    if (data.rolling_average && data.rolling_average.length) {
        datasets.push({
            label: 'Rolling Avg (3 mo)',
            data: data.rolling_average,
            type: 'line',
            borderColor: COLORS.purple,
            borderWidth: 2,
            pointRadius: 0,
            fill: false,
            order: 0,
        });
    }

    new Chart(ctx, {
        type: 'bar',
        data: { labels: data.labels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            onHover: (event, chartElement) => {
                event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
            },
            interaction: {
                mode: 'index',
                intersect: false,
            },
            onClick: (e, elements) => {
                if (elements.length > 0) {
                    const index = elements[0].index;
                    const start = data.period_starts[index];
                    const end = data.period_ends[index];
                    if (start && end) {
                        window.location.href = `/transactions?start=${start}&end=${end}`;
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: COLORS.grid },
                    ticks: {
                        color: COLORS.text,
                        maxRotation: 45,
                        maxTicksLimit: 20,
                    },
                },
                y: {
                    grid: { color: COLORS.grid },
                    ticks: {
                        color: COLORS.text,
                        callback: value => formatCurrency(value),
                    },
                    beginAtZero: true,
                },
            },
            plugins: {
                legend: {
                    labels: { color: COLORS.text, usePointStyle: true },
                },
                tooltip: {
                    callbacks: {
                        label: ctx => ctx.dataset.label + ': ' + formatCurrency(ctx.parsed.y),
                    },
                },
            },
        },
    });
}

function renderIncomeVsExpenseChart(spendingData, incomeData) {
    const ctx = document.getElementById('incomeVsExpenseChart');
    if (!ctx) return;

    // Aggregate biweekly periods into monthly buckets
    // Each period gets assigned to the month of its start date
    const monthlyIncome = {};
    const monthlyExpenses = {};

    // Helper: period_start "YYYY-MM-DD" -> month key "YYYY-MM"
    function toMonthKey(dateStr) {
        return dateStr.substring(0, 7); // "2024-03-04" -> "2024-03"
    }

    // Accumulate spending by month
    if (spendingData.period_starts) {
        spendingData.period_starts.forEach((startDate, i) => {
            const key = toMonthKey(startDate);
            monthlyExpenses[key] = (monthlyExpenses[key] || 0) + (spendingData.spending[i] || 0);
        });
    }

    // Accumulate income by month
    if (incomeData.period_starts) {
        incomeData.period_starts.forEach((startDate, i) => {
            const key = toMonthKey(startDate);
            monthlyIncome[key] = (monthlyIncome[key] || 0) + (incomeData.income[i] || 0);
        });
    }

    // Build sorted list of all month keys
    const allMonthKeys = [...new Set([
        ...Object.keys(monthlyIncome),
        ...Object.keys(monthlyExpenses),
    ])].sort();

    if (allMonthKeys.length === 0) return;

    // Format labels: "Mar 2024", "Apr 2024", etc.
    const labels = allMonthKeys.map(key => {
        const [year, month] = key.split('-');
        const date = new Date(parseInt(year), parseInt(month) - 1, 1);
        return date.toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
    });

    const income = allMonthKeys.map(k => Math.round(monthlyIncome[k] || 0));
    const expenses = allMonthKeys.map(k => Math.round(monthlyExpenses[k] || 0));
    const net = allMonthKeys.map((_, i) => income[i] - expenses[i]);

    // Store month keys for click navigation
    const monthStarts = allMonthKeys.map(k => k + '-01');
    const monthEnds = allMonthKeys.map(k => {
        const [year, month] = k.split('-').map(Number);
        const lastDay = new Date(year, month, 0).getDate();
        return `${k}-${String(lastDay).padStart(2, '0')}`;
    });

    new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Income',
                    data: income,
                    backgroundColor: 'rgba(124, 175, 126, 0.7)',
                    borderRadius: 0,
                    order: 2,
                },
                {
                    label: 'Expenses',
                    data: expenses,
                    backgroundColor: 'rgba(206, 106, 87, 0.7)',
                    borderRadius: 0,
                    order: 2,
                },
                {
                    label: 'Net',
                    data: net,
                    type: 'line',
                    borderColor: COLORS.primary,
                    borderWidth: 2,
                    pointRadius: 4,
                    pointBackgroundColor: net.map(v => v >= 0 ? 'rgba(124, 175, 126, 1)' : 'rgba(206, 106, 87, 1)'),
                    fill: false,
                    tension: 0.3,
                    order: 1,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            onClick: (e, elements) => {
                if (elements.length > 0) {
                    const index = elements[0].index;
                    const start = monthStarts[index];
                    const end = monthEnds[index];
                    if (start && end) {
                        window.location.href = `/transactions?start=${start}&end=${end}`;
                    }
                }
            },
            onHover: (event, chartElement) => {
                event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
            },
            scales: {
                x: {
                    grid: { color: COLORS.grid },
                    ticks: {
                        color: COLORS.text,
                        maxRotation: 45,
                    },
                },
                y: {
                    grid: { color: COLORS.grid },
                    ticks: {
                        color: COLORS.text,
                        callback: value => formatCurrency(value),
                    },
                    beginAtZero: true,
                },
            },
            plugins: {
                legend: {
                    labels: { color: COLORS.text, usePointStyle: true },
                },
                tooltip: {
                    callbacks: {
                        label: ctx => ctx.dataset.label + ': ' + formatCurrency(ctx.parsed.y),
                    },
                },
            },
        },
    });
}

// Curated spending palette — Private Wire earth tones, desaturated, no neon
const SPENDING_PALETTE = [
    { bg: '#D9A03F', glow: 'rgba(217, 160, 63, 0.35)' },   // brass
    { bg: '#7CAF7E', glow: 'rgba(124, 175, 126, 0.35)' },  // ledger green
    { bg: '#CE6A57', glow: 'rgba(206, 106, 87, 0.35)' },   // red ink
    { bg: '#B8A276', glow: 'rgba(184, 162, 118, 0.35)' },  // tan
    { bg: '#8A6F3B', glow: 'rgba(138, 111, 59, 0.35)' },   // ochre
    { bg: '#5F7A61', glow: 'rgba(95, 122, 97, 0.35)' },    // forest
    { bg: '#A98467', glow: 'rgba(169, 132, 103, 0.35)' },  // clay
    { bg: '#C2B49A', glow: 'rgba(194, 180, 154, 0.35)' },  // parchment
    { bg: '#6B5A34', glow: 'rgba(107, 90, 52, 0.35)' },    // olive
    { bg: '#97907E', glow: 'rgba(151, 144, 126, 0.35)' },  // stone
    { bg: '#8F5B4A', glow: 'rgba(143, 91, 74, 0.35)' },    // umber
    { bg: '#4C5B4E', glow: 'rgba(76, 91, 78, 0.35)' },     // moss
];

let spendingChartInstance = null;

function renderSpendingChart(data, days = 30) {
    const canvas = document.getElementById('spendingChart');
    if (!canvas || !data.length) return;

    if (spendingChartInstance) {
        spendingChartInstance.destroy();
    }

    const labels = data.map(d => d.category);
    const values = data.map(d => d.amount);
    const totalAmount = values.reduce((a, b) => a + b, 0);

    const bgColors = data.map((_, i) => SPENDING_PALETTE[i % SPENDING_PALETTE.length].bg);
    const hoverColors = data.map((_, i) => {
        // Slightly brighter on hover
        const c = SPENDING_PALETTE[i % SPENDING_PALETTE.length].bg;
        return c + 'dd';
    });

    // Build the custom legend
    function buildCustomLegend() {
        const container = document.getElementById('spendingLegend');
        if (!container) return;
        container.innerHTML = '';

        data.forEach((item, i) => {
            const color = SPENDING_PALETTE[i % SPENDING_PALETTE.length].bg;
            const pct = ((item.amount / totalAmount) * 100).toFixed(1);

            const row = document.createElement('div');
            row.className = 'legend-row';
            row.style.cursor = 'pointer';
            row.addEventListener('click', () => {
                navigateToCategory(item.category, days);
            });

            row.innerHTML = `
                <div class="legend-left">
                    <span class="legend-dot" style="background:${color};box-shadow:0 0 6px ${color}88;"></span>
                    <span class="legend-label">${item.category}</span>
                </div>
                <div class="legend-right">
                    <span class="legend-amount">${formatCurrency(item.amount)}</span>
                    <span class="legend-pct">${pct}%</span>
                </div>
                <div class="legend-bar-track">
                    <div class="legend-bar-fill" style="width:${pct}%;background:${color};box-shadow:0 0 8px ${color}66;"></div>
                </div>
            `;
            container.appendChild(row);
        });
    }

    function navigateToCategory(category, days) {
        const today = new Date();
        const startDate = new Date();
        startDate.setDate(today.getDate() - days);
        const startStr = startDate.toISOString().split('T')[0];
        const endStr = today.toISOString().split('T')[0];
        if (category === "Uncategorized") {
            window.location.href = `/transactions?status=uncategorized&start=${startStr}&end=${endStr}`;
        } else {
            window.location.href = `/transactions?category=${encodeURIComponent(category)}&start=${startStr}&end=${endStr}`;
        }
    }

    spendingChartInstance = new Chart(canvas, {
        type: 'doughnut',
        data: {
            labels: labels,
            datasets: [{
                data: values,
                backgroundColor: bgColors,
                hoverBackgroundColor: hoverColors,
                borderWidth: 0,
                hoverOffset: 12,
                borderRadius: 0,
                spacing: 3,
                cutout: '72%',
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            layout: {
                padding: { top: 10, bottom: 10, left: 10, right: 10 }
            },
            animation: {
                animateRotate: true,
                animateScale: true,
                duration: 800,
                easing: 'easeOutQuart',
            },
            plugins: {
                legend: { display: false }, // We use custom legend
                tooltip: {
                    backgroundColor: 'rgba(36, 32, 25, 0.96)',
                    titleColor: '#EDE8DF',
                    bodyColor: '#EDE8DF',
                    borderColor: '#33302A',
                    borderWidth: 1,
                    cornerRadius: 2,
                    padding: 12,
                    displayColors: true,
                    boxPadding: 6,
                    titleFont: { weight: 'bold', size: 13 },
                    bodyFont: { size: 12 },
                    callbacks: {
                        title: (items) => {
                            return data[items[0].dataIndex].category;
                        },
                        label: (ctx) => {
                            const item = data[ctx.dataIndex];
                            return ` ${formatCurrency(item.amount)}  (${item.percentage}%)`;
                        }
                    }
                }
            },
            onHover: (event, chartElement) => {
                event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
            },
            onClick: (e, elements) => {
                if (elements.length > 0) {
                    const index = elements[0].index;
                    navigateToCategory(data[index].category, days);
                }
            }
        },
        plugins: [
            {
                // Inner shadow ring for depth
                id: 'innerShadow',
                beforeDraw: function (chart) {
                    const { ctx, chartArea } = chart;
                    if (!chartArea) return;
                    const cx = (chartArea.left + chartArea.right) / 2;
                    const cy = (chartArea.top + chartArea.bottom) / 2;
                    const outerR = Math.min(chartArea.right - chartArea.left, chartArea.bottom - chartArea.top) / 2;
                    const innerR = outerR * 0.72; // matches cutout

                    // Subtle inner glow
                    const grad = ctx.createRadialGradient(cx, cy, innerR - 8, cx, cy, innerR + 4);
                    grad.addColorStop(0, 'rgba(18, 17, 16, 0.6)');
                    grad.addColorStop(1, 'rgba(18, 17, 16, 0)');

                    ctx.save();
                    ctx.beginPath();
                    ctx.arc(cx, cy, innerR + 4, 0, Math.PI * 2);
                    ctx.arc(cx, cy, innerR - 8, 0, Math.PI * 2, true);
                    ctx.fillStyle = grad;
                    ctx.fill();
                    ctx.restore();
                }
            },
            {
                // Center text with total
                id: 'centerText',
                beforeDraw: function (chart) {
                    const { ctx, chartArea } = chart;
                    if (!chartArea) return;

                    const cx = (chartArea.left + chartArea.right) / 2;
                    const cy = (chartArea.top + chartArea.bottom) / 2;
                    const chartH = chartArea.bottom - chartArea.top;

                    ctx.save();
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';

                    // "Total Spending" label
                    const labelSize = Math.max(10, chartH * 0.055);
                    ctx.font = `500 ${labelSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
                    ctx.fillStyle = '#97907E';
                    ctx.fillText('Total Spending', cx, cy - chartH * 0.06);

                    // Dollar amount
                    const amountSize = Math.max(16, chartH * 0.11);
                    ctx.font = `600 ${amountSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
                    ctx.fillStyle = '#EDE8DF';
                    ctx.fillText(formatCurrency(totalAmount), cx, cy + chartH * 0.04);

                    // Days label
                    const subSize = Math.max(9, chartH * 0.04);
                    ctx.font = `400 ${subSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
                    ctx.fillStyle = '#6E6858';
                    ctx.fillText(`Past ${days} days`, cx, cy + chartH * 0.12);

                    ctx.restore();
                }
            }
        ]
    });

    buildCustomLegend();
}


// --- Category Spending Trends ---

const CATEGORY_TREND_COLORS = [
    { line: '#D9A03F', fill: 'rgba(217, 160, 63, 0.12)' },   // brass
    { line: '#7CAF7E', fill: 'rgba(124, 175, 126, 0.12)' },  // ledger green
    { line: '#CE6A57', fill: 'rgba(206, 106, 87, 0.12)' },   // red ink
    { line: '#B8A276', fill: 'rgba(184, 162, 118, 0.12)' },  // tan
    { line: '#8A6F3B', fill: 'rgba(138, 111, 59, 0.12)' },   // ochre
    { line: '#5F7A61', fill: 'rgba(95, 122, 97, 0.12)' },    // forest
    { line: '#A98467', fill: 'rgba(169, 132, 103, 0.12)' },  // clay
    { line: '#97907E', fill: 'rgba(151, 144, 126, 0.12)' },  // stone
];

let categoryTrendsChartInstance = null;

function renderCategoryTrendsChart(data, months) {
    const ctx = document.getElementById('categoryTrendsChart');
    if (!ctx || !data.labels || !data.labels.length) return;

    if (categoryTrendsChartInstance) {
        categoryTrendsChartInstance.destroy();
    }

    const categoryNames = Object.keys(data.datasets);
    if (!categoryNames.length) return;

    // Sort categories by total spending (descending) for better default ordering
    categoryNames.sort((a, b) => {
        const sumA = data.datasets[a].reduce((s, v) => s + v, 0);
        const sumB = data.datasets[b].reduce((s, v) => s + v, 0);
        return sumB - sumA;
    });

    // Format labels: "2025-03" → "Mar 2025" or "Mar" for shorter ranges
    const useShortLabels = months <= 3;
    const displayLabels = data.labels.map(ym => {
        const [y, m] = ym.split('-');
        const d = new Date(parseInt(y), parseInt(m) - 1, 1);
        return useShortLabels
            ? d.toLocaleDateString(undefined, { month: 'short' })
            : d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' });
    });

    const datasets = categoryNames.map((cat, i) => {
        const colors = CATEGORY_TREND_COLORS[i % CATEGORY_TREND_COLORS.length];
        return {
            label: cat,
            data: data.datasets[cat],
            borderColor: colors.line,
            backgroundColor: colors.fill,
            fill: false,
            tension: 0.3,
            borderWidth: 2.5,
            pointRadius: months <= 3 ? 4 : 2,
            pointHoverRadius: 6,
            pointBackgroundColor: colors.line,
            pointBorderColor: '#121110',
            pointBorderWidth: 2,
            // Hide categories beyond the top 5 by default for readability
            hidden: i >= 5,
        };
    });

    let chartRef = null;

    chartRef = new Chart(ctx, {
        type: 'line',
        data: { labels: displayLabels, datasets },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            interaction: {
                mode: 'index',
                intersect: false,
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: 'rgba(36, 32, 25, 0.96)',
                    titleColor: '#EDE8DF',
                    bodyColor: '#C9C2B4',
                    borderColor: '#33302A',
                    borderWidth: 1,
                    cornerRadius: 2,
                    padding: 12,
                    displayColors: true,
                    boxPadding: 6,
                    titleFont: { weight: '600', size: 13 },
                    bodyFont: { size: 12 },
                    callbacks: {
                        label: function(context) {
                            return ` ${context.dataset.label}: ${formatCurrency(context.parsed.y)}`;
                        },
                    },
                    filter: (item) => {
                        if (!chartRef) return true;
                        const meta = chartRef.getDatasetMeta(item.datasetIndex);
                        const isHidden = meta.hidden === null ? !!chartRef.data.datasets[item.datasetIndex].hidden : meta.hidden;
                        return !isHidden;
                    },
                },
            },
            scales: {
                x: {
                    grid: { color: COLORS.grid },
                    ticks: {
                        color: COLORS.text,
                        font: { size: 11 },
                        maxRotation: 45,
                    },
                },
                y: {
                    grid: { color: COLORS.grid },
                    ticks: {
                        color: COLORS.text,
                        callback: v => formatCurrency(v),
                    },
                    beginAtZero: true,
                },
            },
            onHover: (event, chartElement) => {
                event.native.target.style.cursor = chartElement[0] ? 'pointer' : 'default';
            },
            onClick: (event, elements) => {
                if (elements.length > 0) {
                    const idx = elements[0].index;
                    const datasetIdx = elements[0].datasetIndex;
                    const cat = categoryNames[datasetIdx];
                    const ym = data.labels[idx]; // "2025-03"
                    const [y, m] = ym.split('-');
                    const lastDay = new Date(parseInt(y), parseInt(m), 0).getDate();
                    const start = `${y}-${m}-01`;
                    const end = `${y}-${m}-${String(lastDay).padStart(2, '0')}`;
                    window.location.href = `/transactions?category=${encodeURIComponent(cat)}&start=${start}&end=${end}&status=all`;
                }
            },
        },
    });

    categoryTrendsChartInstance = chartRef;

    // Build toggle legend
    buildCategoryTrendsLegend(chartRef, categoryNames, data);
}

function buildCategoryTrendsLegend(chart, categoryNames, data) {
    const container = document.getElementById('categoryTrendsLegend');
    if (!container) return;
    container.innerHTML = '';

    categoryNames.forEach((cat, i) => {
        const colors = CATEGORY_TREND_COLORS[i % CATEGORY_TREND_COLORS.length];
        const total = data.datasets[cat].reduce((s, v) => s + v, 0);

        const item = document.createElement('div');
        item.className = 'nw-legend-item';
        item.style.setProperty('--legend-color', colors.line + '55');
        item.style.setProperty('--legend-bg', colors.line + '15');

        const isHidden = chart.data.datasets[i].hidden;
        item.classList.add(isHidden ? 'inactive' : 'active');

        item.innerHTML = `
            <span class="nw-legend-dot" style="background:${colors.line};box-shadow:0 0 6px ${colors.line}66;"></span>
            <span class="nw-legend-name">${cat}</span>
            <span class="nw-legend-value">${formatCurrency(total)}</span>
        `;

        item.addEventListener('click', () => {
            const meta = chart.getDatasetMeta(i);
            const isCurrentlyHidden = meta.hidden === null ? !!chart.data.datasets[i].hidden : meta.hidden;
            meta.hidden = !isCurrentlyHidden;
            chart.update();

            if (meta.hidden) {
                item.classList.remove('active');
                item.classList.add('inactive');
            } else {
                item.classList.remove('inactive');
                item.classList.add('active');
            }
        });

        container.appendChild(item);
    });
}
