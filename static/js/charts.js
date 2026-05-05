// Chart color palette
const COLORS = {
    primary: '#3b82f6',
    primaryFill: 'rgba(59, 130, 246, 0.15)',
    green: 'rgba(74, 222, 128, 0.7)',
    red: 'rgba(248, 113, 113, 0.7)',
    yellow: '#f59e0b',
    purple: '#8b5cf6',
    grid: 'rgba(148, 163, 184, 0.1)',
    text: '#94a3b8',
};

// Account line colors for net worth breakdown
const ACCOUNT_COLORS = [
    '#22d3ee', '#a78bfa', '#fb923c', '#f472b6', '#34d399', '#fbbf24',
];

function formatCurrency(value) {
    if (value < 0) {
        return '-$' + Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
    }
    return '$' + value.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

// Polished account line colors
const ACCOUNT_LINE_COLORS = [
    '#22d3ee', '#a78bfa', '#fb923c', '#f472b6', '#34d399', '#fbbf24',
    '#38bdf8', '#e879f9', '#4ade80', '#f87171',
];

function renderNetWorthChart(data) {
    const canvas = document.getElementById('netWorthChart');
    if (!canvas || !data.labels.length) return;

    let mainGradient = null;

    const latestValue = data.datasets[0].data[data.datasets[0].data.length - 1];
    const earliestValue = data.datasets[0].data[0];
    const isPositiveTrend = latestValue >= earliestValue;
    const trendColor = isPositiveTrend ? '#22c55e' : '#ef4444';

    const datasets = [{
        label: data.datasets[0].label,
        data: data.datasets[0].data,
        borderColor: trendColor,
        backgroundColor: 'transparent',
        fill: true,
        tension: 0.35,
        pointRadius: 0,
        pointHoverRadius: 6,
        pointHoverBackgroundColor: trendColor,
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
        pointHitRadius: 20,
        borderWidth: 2.5,
    }];

    // Per-account lines (hidden by default)
    for (let i = 1; i < data.datasets.length; i++) {
        const color = ACCOUNT_LINE_COLORS[(i - 1) % ACCOUNT_LINE_COLORS.length];
        datasets.push({
            label: data.datasets[i].label,
            data: data.datasets[i].data,
            borderColor: color,
            backgroundColor: color + '11',
            borderWidth: 1.8,
            borderDash: [5, 4],
            fill: false,
            tension: 0.35,
            pointRadius: 0,
            pointHoverRadius: 5,
            pointHoverBackgroundColor: color,
            pointHoverBorderColor: '#fff',
            pointHoverBorderWidth: 2,
            pointHitRadius: 15,
            hidden: true,
        });
    }

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
                        color: 'rgba(148, 163, 184, 0.06)',
                        drawTicks: false,
                    },
                    ticks: {
                        color: '#64748b',
                        maxTicksLimit: 12,
                        padding: 8,
                        font: { size: 11 },
                    },
                    border: { display: false },
                },
                y: {
                    grid: {
                        color: 'rgba(148, 163, 184, 0.06)',
                        drawTicks: false,
                    },
                    ticks: {
                        color: '#64748b',
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
                    backgroundColor: 'rgba(15, 23, 42, 0.95)',
                    titleColor: '#f1f5f9',
                    bodyColor: '#e2e8f0',
                    borderColor: '#475569',
                    borderWidth: 1,
                    cornerRadius: 8,
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
                            return ' ' + name + ':  ' + formatCurrency(ctx.parsed.y);
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
                        mainGradient.addColorStop(0, 'rgba(34, 197, 94, 0.25)');
                        mainGradient.addColorStop(0.5, 'rgba(34, 197, 94, 0.06)');
                        mainGradient.addColorStop(1, 'rgba(34, 197, 94, 0)');
                    } else {
                        mainGradient.addColorStop(0, 'rgba(239, 68, 68, 0.25)');
                        mainGradient.addColorStop(0.5, 'rgba(239, 68, 68, 0.06)');
                        mainGradient.addColorStop(1, 'rgba(239, 68, 68, 0)');
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
                ctx.strokeStyle = 'rgba(148, 163, 184, 0.25)';
                ctx.setLineDash([4, 4]);
                ctx.stroke();
                ctx.restore();
            }
        }]
    });

    // Build custom HTML legend with toggle pills
    buildNetWorthLegend(nwChart, data);
}

function buildNetWorthLegend(chart, data) {
    const container = document.getElementById('netWorthLegend');
    if (!container) return;
    container.innerHTML = '';

    data.datasets.forEach((ds, i) => {
        const isMain = i === 0;
        const color = isMain
            ? chart.data.datasets[0].borderColor
            : ACCOUNT_LINE_COLORS[(i - 1) % ACCOUNT_LINE_COLORS.length];

        // Get the latest (most recent) value for this dataset
        const latestVal = ds.data[ds.data.length - 1];

        // Clean up label: remove long account IDs after name
        let displayName = ds.label;
        // Truncate at first occurrence of a long number sequence
        const idMatch = displayName.match(/[\s_-]+\d{6,}/);
        if (idMatch) {
            displayName = displayName.substring(0, idMatch.index);
        }

        const item = document.createElement('div');
        item.className = 'nw-legend-item';
        item.classList.add(chart.data.datasets[i].hidden ? 'inactive' : 'active');
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
            borderRadius: 4,
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
                    backgroundColor: 'rgba(74, 222, 128, 0.7)',
                    borderRadius: 4,
                    order: 2,
                },
                {
                    label: 'Expenses',
                    data: expenses,
                    backgroundColor: 'rgba(248, 113, 113, 0.7)',
                    borderRadius: 4,
                    order: 2,
                },
                {
                    label: 'Net',
                    data: net,
                    type: 'line',
                    borderColor: COLORS.primary,
                    borderWidth: 2,
                    pointRadius: 4,
                    pointBackgroundColor: net.map(v => v >= 0 ? 'rgba(74, 222, 128, 1)' : 'rgba(248, 113, 113, 1)'),
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

// Curated spending palette — vibrant but harmonious on dark backgrounds
const SPENDING_PALETTE = [
    { bg: '#6366f1', glow: 'rgba(99, 102, 241, 0.35)' },   // indigo
    { bg: '#f43f5e', glow: 'rgba(244, 63, 94, 0.35)' },    // rose
    { bg: '#22d3ee', glow: 'rgba(34, 211, 238, 0.35)' },    // cyan
    { bg: '#f97316', glow: 'rgba(249, 115, 22, 0.35)' },    // orange
    { bg: '#a78bfa', glow: 'rgba(167, 139, 250, 0.35)' },   // violet
    { bg: '#34d399', glow: 'rgba(52, 211, 153, 0.35)' },    // emerald
    { bg: '#fb7185', glow: 'rgba(251, 113, 133, 0.35)' },   // pink
    { bg: '#fbbf24', glow: 'rgba(251, 191, 36, 0.35)' },    // amber
    { bg: '#38bdf8', glow: 'rgba(56, 189, 248, 0.35)' },    // sky
    { bg: '#e879f9', glow: 'rgba(232, 121, 249, 0.35)' },   // fuchsia
    { bg: '#4ade80', glow: 'rgba(74, 222, 128, 0.35)' },    // green
    { bg: '#818cf8', glow: 'rgba(129, 140, 248, 0.35)' },   // indigo-light
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
                borderRadius: 4,
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
                    backgroundColor: 'rgba(15, 23, 42, 0.95)',
                    titleColor: '#f1f5f9',
                    bodyColor: '#e2e8f0',
                    borderColor: '#475569',
                    borderWidth: 1,
                    cornerRadius: 8,
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
                    grad.addColorStop(0, 'rgba(15, 23, 42, 0.6)');
                    grad.addColorStop(1, 'rgba(15, 23, 42, 0)');

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
                    ctx.fillStyle = '#64748b';
                    ctx.fillText('Total Spending', cx, cy - chartH * 0.06);

                    // Dollar amount
                    const amountSize = Math.max(16, chartH * 0.11);
                    ctx.font = `700 ${amountSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
                    ctx.fillStyle = '#f1f5f9';
                    ctx.fillText(formatCurrency(totalAmount), cx, cy + chartH * 0.04);

                    // Days label
                    const subSize = Math.max(9, chartH * 0.04);
                    ctx.font = `400 ${subSize}px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`;
                    ctx.fillStyle = '#475569';
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
    { line: '#4ade80', fill: 'rgba(74, 222, 128, 0.12)' },   // green
    { line: '#38bdf8', fill: 'rgba(56, 189, 248, 0.12)' },   // sky
    { line: '#f59e0b', fill: 'rgba(245, 158, 11, 0.12)' },   // amber
    { line: '#a78bfa', fill: 'rgba(167, 139, 250, 0.12)' },  // violet
    { line: '#fb923c', fill: 'rgba(251, 146, 60, 0.12)' },   // orange
    { line: '#f472b6', fill: 'rgba(244, 114, 182, 0.12)' },  // pink
    { line: '#22d3ee', fill: 'rgba(34, 211, 238, 0.12)' },   // cyan
    { line: '#e879f9', fill: 'rgba(232, 121, 249, 0.12)' },  // fuchsia
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
            pointBorderColor: '#1e293b',
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
                    backgroundColor: 'rgba(15, 23, 42, 0.95)',
                    titleColor: '#f1f5f9',
                    bodyColor: '#cbd5e1',
                    borderColor: '#334155',
                    borderWidth: 1,
                    cornerRadius: 8,
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
