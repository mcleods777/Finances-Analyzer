document.addEventListener('DOMContentLoaded', async () => {
    // Set default date for manual balance form to today
    const dateInput = document.getElementById('mb-date');
    if (dateInput) {
        dateInput.value = new Date().toISOString().split('T')[0];
    }

    try {
        const [netWorthData, spendingData, incomeData, runwayData, breakdownData, monthlyRunwayData, categoryTrendsData] = await Promise.all([
            fetch('/api/net-worth').then(r => r.json()),
            fetch('/api/biweekly-spending').then(r => r.json()),
            fetch('/api/biweekly-income').then(r => r.json()),
            fetch('/api/runway').then(r => r.json()),
            fetch('/api/spending-breakdown?days=30').then(r => r.json()),
            fetch('/api/monthly-runway').then(r => r.json()),
            fetch('/api/category-trends').then(r => r.json()),
        ]);

        renderNetWorthChart(netWorthData);
        renderIncomeVsExpenseChart(spendingData, incomeData);
        renderBiweeklyChart(spendingData);
        updateRunwayBar(runwayData);
        renderSpendingChart(breakdownData, 30);
        updateMonthlyRunwayBars(monthlyRunwayData);
        renderCategoryTrendsChart(categoryTrendsData, 12);
    } catch (err) {
        console.error('Failed to load dashboard data:', err);
    }

    // Load manual balance history
    loadManualBalances();
});

async function updateNetWorthRange(range) {
    // Update active button state
    document.querySelectorAll('#nw-range-controls .chart-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.range === range);
    });

    try {
        const data = await fetch(`/api/net-worth?range=${range}`).then(r => r.json());
        renderNetWorthChart(data);
    } catch (err) {
        console.error('Failed to update net worth chart:', err);
    }
}

async function updateSpendingChart(days) {
    // Update active button state
    document.querySelectorAll('.chart-controls .chart-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.textContent.includes(days)) btn.classList.add('active');
    });

    try {
        const data = await fetch(`/api/spending-breakdown?days=${days}`).then(r => r.json());
        renderSpendingChart(data, days);
    } catch (err) {
        console.error('Failed to update spending chart:', err);
    }
}

async function updateCategoryTrends(months) {
    // Update active button state
    document.querySelectorAll('.category-trends-controls .chart-btn').forEach(btn => {
        btn.classList.remove('active');
        if (btn.dataset.months === String(months)) btn.classList.add('active');
    });

    try {
        const data = await fetch(`/api/category-trends?months=${months}`).then(r => r.json());
        renderCategoryTrendsChart(data, months);
    } catch (err) {
        console.error('Failed to update category trends:', err);
    }
}

function updateRunwayBar(data) {
    // Store runway data globally so recurring bills table can use it
    window._runwayData = data;
}

// --- Monthly Half Runway ---

const BILL_COLORS = [
    '#f59e0b', '#f97316', '#ef4444', '#ec4899',
    '#a855f7', '#6366f1', '#0ea5e9', '#14b8a6',
];

const fmtCurrency = (v) => v < 0 ? `-$${Math.abs(v).toFixed(2)}` : `$${v.toFixed(2)}`;

function updateMonthlyRunwayBars(data) {
    if (!data || !data.halves) return;

    window._monthlyRunwayData = data;

    const halves = data.halves;

    halves.forEach((half, i) => {
        const idx = i + 1;
        const card = document.getElementById(`monthly-half-${idx}`);
        const labelEl = document.getElementById(`half${idx}-label`);
        const daysEl = document.getElementById(`half${idx}-days`);
        const budgetInput = document.getElementById(`half${idx}-budget`);
        const statsEl = document.getElementById(`half${idx}-stats`);
        const bar = document.getElementById(`half${idx}-bar`);
        const legendEl = document.getElementById(`half${idx}-legend`);

        if (!card) return;

        // Current half highlight
        if (half.is_current) {
            card.classList.add('is-current');
        }

        // Label
        labelEl.innerHTML = half.label + (half.is_current ? '<span class="current-tag">Current</span>' : '');

        // Days
        if (half.is_current) {
            daysEl.textContent = `${half.days_remaining} days left`;
        } else {
            daysEl.textContent = `${half.days_remaining} days`;
        }

        // Budget input
        budgetInput.value = half.budget.toFixed(2);

        // Stats
        const freeCashClass = half.free_cash >= 0 ? 'free-cash' : 'free-cash negative';
        statsEl.innerHTML = `
            <span class="half-stat spent">Spent<strong>${fmtCurrency(half.spent_so_far)}</strong></span>
            <span class="half-stat">Committed<strong>${fmtCurrency(half.committed)}</strong></span>
            <span class="half-stat ${freeCashClass}">Free<strong>${fmtCurrency(half.free_cash)}</strong></span>
        `;

        // Render segmented bar
        renderHalfBar(bar, legendEl, half);
    });

    // Populate pending bills cards across both halves
    populatePendingBillCards(halves);

    // Init simulator
    initSimulator(data);

    // Populate temp expenses list
    renderTempExpenses(data.temporary_expenses || []);
}

function renderHalfBar(bar, legendEl, half, delta) {
    const budget = half.budget;
    if (budget <= 0) {
        bar.style.width = '0%';
        return;
    }

    const adjustedFreeCash = delta !== undefined ? half.free_cash + delta : half.free_cash;
    const totalUsed = half.spent_so_far + half.committed;
    const adjustedTotalUsed = delta !== undefined ? totalUsed - delta : totalUsed;
    const pctUsed = Math.min(100, Math.max(0, (adjustedTotalUsed / budget) * 100));

    bar.style.width = '100%';
    bar.style.background = 'none';

    let segmentsEl = bar.querySelector('.runway-segments');
    if (!segmentsEl) {
        bar.innerHTML = '';
        segmentsEl = document.createElement('div');
        segmentsEl.className = 'runway-segments';
        bar.appendChild(segmentsEl);
    }
    segmentsEl.innerHTML = '';

    // Calculate segment sizes as percent of budget
    const spentPct = Math.max(0, (half.spent_so_far / budget) * 100);

    const pendingBills = (half.pending_bills || []).filter(b => b.status === 'pending');
    const tempExpenses = half.temporary_expenses || [];

    // Free cash color
    const freeVal = adjustedFreeCash;
    const freePct = Math.max(0, (freeVal / budget) * 100);
    const freeColor = freeVal > budget * 0.35 ? '#22c55e' : (freeVal > budget * 0.15 ? '#eab308' : '#ef4444');

    // Helper: inline label if segment is wide enough
    const inlineLabel = (text, pct) => pct > 12 ? `<span class="segment-inline-label">${text}</span>` : '';

    // Spent segment
    if (spentPct > 0) {
        const spentLabel = inlineLabel(fmtCurrency(half.spent_so_far), spentPct);
        segmentsEl.innerHTML += `
            <div class="runway-segment" style="width: ${Math.min(spentPct, 100)}%; background: linear-gradient(180deg, #64748b 0%, #475569 100%);">
                ${spentLabel}
                <div class="segment-tooltip">
                    <div class="segment-tooltip-name">Already Spent</div>
                    <div class="segment-tooltip-amount">${fmtCurrency(half.spent_so_far)}</div>
                </div>
            </div>`;
    }

    // Pending bills — alternating amber shades for visual grouping
    if (pendingBills.length > 0) {
        pendingBills.forEach((bill, bi) => {
            const billPct = Math.max(0.5, (bill.amount / budget) * 100);
            const shade = bi % 2 === 0
                ? 'linear-gradient(180deg, #fbbf24 0%, #f59e0b 100%)'
                : 'linear-gradient(180deg, #f59e0b 0%, #d97706 100%)';
            const dueDate = new Date(bill.due_date + 'T00:00:00');
            const dateStr = dueDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
            const billLabel = inlineLabel(escapeHtml(bill.name), billPct);

            segmentsEl.innerHTML += `
                <div class="runway-segment" style="width: ${billPct}%; background: ${shade}; border-right: 1px solid rgba(0,0,0,0.15);">
                    ${billLabel}
                    <div class="segment-tooltip">
                        <div class="segment-tooltip-name">${escapeHtml(bill.name)}</div>
                        <div class="segment-tooltip-amount">${fmtCurrency(bill.amount)}</div>
                        <div class="segment-tooltip-date">Due ${dateStr}</div>
                    </div>
                </div>`;
        });
    }

    // Temp expenses
    tempExpenses.forEach(te => {
        const tePct = Math.max(0.5, (te.amount / budget) * 100);
        const teLabel = inlineLabel(escapeHtml(te.name), tePct);
        segmentsEl.innerHTML += `
            <div class="runway-segment" style="width: ${tePct}%; background: linear-gradient(180deg, #c084fc 0%, #a855f7 100%); border-right: 1px solid rgba(0,0,0,0.15);">
                ${teLabel}
                <div class="segment-tooltip">
                    <div class="segment-tooltip-name">${escapeHtml(te.name)}</div>
                    <div class="segment-tooltip-amount">${fmtCurrency(te.amount)}</div>
                    <div class="segment-tooltip-date">Temporary</div>
                </div>
            </div>`;
    });

    // Free cash
    if (freePct > 0) {
        const freeGrad = freeVal > budget * 0.35
            ? 'linear-gradient(180deg, #4ade80 0%, #22c55e 100%)'
            : (freeVal > budget * 0.15
                ? 'linear-gradient(180deg, #fde047 0%, #eab308 100%)'
                : 'linear-gradient(180deg, #f87171 0%, #ef4444 100%)');
        const freeLabel = inlineLabel(fmtCurrency(freeVal), Math.min(freePct, 100 - spentPct));
        segmentsEl.innerHTML += `
            <div class="runway-segment" style="width: ${Math.min(freePct, 100 - spentPct)}%; background: ${freeGrad};">
                ${freeLabel}
                <div class="segment-tooltip">
                    <div class="segment-tooltip-name">Free Cash</div>
                    <div class="segment-tooltip-amount">${fmtCurrency(freeVal)}</div>
                </div>
            </div>`;
    }

    // Percentage badge next to bar
    const pctEl = bar.closest('.monthly-half-card')?.querySelector('.runway-pct-badge');
    if (pctEl) {
        const usedPct = Math.round(pctUsed);
        pctEl.textContent = `${usedPct}%`;
        pctEl.title = `${usedPct}% of budget used (spent + committed)`;
        pctEl.className = 'runway-pct-badge ' + (
            usedPct > 85 ? 'pct-danger' : (usedPct > 65 ? 'pct-warn' : 'pct-good')
        );
    }

    // Legend — clear descriptions
    legendEl.innerHTML = '';
    const items = [];
    if (spentPct > 0) {
        items.push(`<span class="runway-bar-legend-item"><span class="runway-bar-legend-dot" style="background:#64748b;"></span>Spent ${fmtCurrency(half.spent_so_far)}</span>`);
    }
    if (pendingBills.length > 0) {
        items.push(`<span class="runway-bar-legend-item"><span class="runway-bar-legend-dot" style="background:#f59e0b;"></span>${pendingBills.length} upcoming bill${pendingBills.length !== 1 ? 's' : ''} ${fmtCurrency(half.pending_total)}</span>`);
    }
    if (tempExpenses.length > 0) {
        const tempSum = tempExpenses.reduce((s, t) => s + t.amount, 0);
        items.push(`<span class="runway-bar-legend-item"><span class="runway-bar-legend-dot" style="background:#a855f7;"></span>Temporary ${fmtCurrency(tempSum)}</span>`);
    }
    items.push(`<span class="runway-bar-legend-item"><span class="runway-bar-legend-dot" style="background:${freeColor};"></span>Free ${fmtCurrency(freeVal)}</span>`);
    legendEl.innerHTML = items.join('');
}

function populatePendingBillCards(halves) {
    const container = document.getElementById('pending-bills-container');
    const list = document.getElementById('pending-bills-list');
    const totalEl = document.getElementById('pending-bills-total');

    if (!container || !list) return;

    // Merge all bills from both halves
    const allBills = [];
    halves.forEach(h => {
        (h.pending_bills || []).forEach(b => allBills.push(b));
    });

    const pendingBills = allBills.filter(b => b.status === 'pending');
    const paidBills = allBills.filter(b => b.status === 'paid');
    const pendingTotal = pendingBills.reduce((s, b) => s + b.amount, 0);

    // Hide entire section if no pending bills
    if (pendingBills.length === 0) {
        container.style.display = 'none';
        return;
    }

    container.style.display = 'block';

    if (totalEl) {
        totalEl.textContent = `${pendingBills.length} pending \u2022 $${pendingTotal.toFixed(2)} committed`;
        totalEl.style.color = '#fbbf24';
    }

    const pendingCardHtml = (b) => {
        const dueDate = new Date(b.due_date + 'T00:00:00');
        const dateStr = dueDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        const searchTerm = encodeURIComponent(b.search_keyword || b.name);

        return `<a href="/transactions?search=${searchTerm}&status=all" class="bill-card-link">
            <div class="bill-card pending">
                <div class="bill-card-name">
                    <span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#f59e0b;margin-right:6px;vertical-align:middle;"></span>
                    ${escapeHtml(b.name)}
                </div>
                <div class="bill-card-row">
                    <span class="bill-card-amount pending">$${b.amount.toFixed(2)}</span>
                    <span class="bill-card-date">Due ${dateStr}</span>
                </div>
                <span class="bill-card-status pending">\u25CB Pending</span>
            </div>
        </a>`;
    };

    // Split pending cards into the two half-month groups, mirroring the
    // Budget Runway halves (labels like "Jul 1-15" come from the server).
    let html = '';
    halves.forEach((h, hi) => {
        const groupPending = (h.pending_bills || []).filter(b => b.status === 'pending');
        const groupCommitted = groupPending.reduce((s, b) => s + b.amount, 0);

        html += `<div class="bill-group-header${hi > 0 ? ' bill-group-divider' : ''}">
            <span class="bill-group-label">${escapeHtml(h.label || '')}</span>
            <span class="bill-group-total">${groupPending.length} pending \u2022 $${groupCommitted.toFixed(2)} committed</span>
        </div>`;

        if (groupPending.length === 0) {
            html += `<div class="bill-group-empty">No pending bills</div>`;
        } else {
            html += groupPending.map(pendingCardHtml).join('');
        }
    });

    // Add a "Show paid" toggle if there are paid bills
    if (paidBills.length > 0) {
        html += `<div id="paid-bills-toggle" style="grid-column: 1 / -1; text-align: center; padding: 6px 0;">
            <a href="#" onclick="togglePaidBills(event)" style="color: #64748b; font-size: 0.78rem; text-decoration: none;">
                Show ${paidBills.length} paid bill${paidBills.length !== 1 ? 's' : ''} &darr;
            </a>
        </div>`;

        html += paidBills.map((b) => {
            const dueDate = new Date(b.due_date + 'T00:00:00');
            const dateStr = dueDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
            const searchTerm = encodeURIComponent(b.search_keyword || b.name);

            return `<a href="/transactions?search=${searchTerm}&status=all" class="bill-card-link" style="display: none;">
                <div class="bill-card paid paid-bill-card">
                    <div class="bill-card-name">
                        <span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:#4ade80;margin-right:6px;vertical-align:middle;"></span>
                        ${escapeHtml(b.name)}
                    </div>
                    <div class="bill-card-row">
                        <span class="bill-card-amount paid">$${b.amount.toFixed(2)}</span>
                        <span class="bill-card-date">Due ${dateStr}</span>
                    </div>
                    <span class="bill-card-status paid">
                        \u2714 Paid${b.paid_date ? ' ' + new Date(b.paid_date + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : ''}
                    </span>
                </div>
            </a>`;
        }).join('');
    }

    list.innerHTML = html;
}

function togglePaidBills(event) {
    event.preventDefault();
    // Paid cards are wrapped in <a class="bill-card-link"> — find those wrappers
    const paidLinks = document.querySelectorAll('.bill-card-link:has(.paid-bill-card)');
    const toggle = document.getElementById('paid-bills-toggle');
    const isHidden = paidLinks[0] && paidLinks[0].style.display === 'none';

    paidLinks.forEach(link => {
        link.style.display = isHidden ? '' : 'none';
    });

    if (toggle) {
        const count = paidLinks.length;
        toggle.innerHTML = isHidden
            ? `<a href="#" onclick="togglePaidBills(event)" style="color: #64748b; font-size: 0.78rem; text-decoration: none;">Hide paid bills &uarr;</a>`
            : `<a href="#" onclick="togglePaidBills(event)" style="color: #64748b; font-size: 0.78rem; text-decoration: none;">Show ${count} paid bill${count !== 1 ? 's' : ''} &darr;</a>`;
    }
}

// --- Budget Simulator ---

function toggleSimulator() {
    const content = document.getElementById('simulator-content');
    const toggle = document.getElementById('simulator-toggle');
    if (content.style.display === 'none') {
        content.style.display = 'block';
        toggle.textContent = '\u2212';
    } else {
        content.style.display = 'none';
        toggle.textContent = '+';
    }
}

function initSimulator(data) {
    const grid = document.getElementById('simulator-sliders');
    if (!grid) return;

    const cats = data.category_averages || [];
    if (cats.length === 0) {
        grid.innerHTML = '<p style="color:#64748b; font-size:0.82rem;">No category data available for simulation.</p>';
        return;
    }

    window._simulatorOriginals = {};

    grid.innerHTML = cats.map(cat => {
        const avg = cat.avg_per_half;
        const maxVal = Math.round(avg * 3);
        window._simulatorOriginals[cat.category] = avg;

        return `<div class="slider-row">
            <span class="slider-label" title="${escapeHtml(cat.category)}">${escapeHtml(cat.category)}</span>
            <input type="range" class="slider-input" min="0" max="${maxVal}" step="1" value="${Math.round(avg)}"
                   data-category="${escapeAttr(cat.category)}" data-original="${avg}"
                   oninput="onSimulatorSliderChange()">
            <span class="slider-value" data-slider-value="${escapeAttr(cat.category)}">${fmtCurrency(avg)}</span>
            <span class="slider-diff neutral" data-slider-diff="${escapeAttr(cat.category)}">$0</span>
        </div>`;
    }).join('');
}

function onSimulatorSliderChange() {
    const sliders = document.querySelectorAll('#simulator-sliders .slider-input');
    let totalDelta = 0;

    sliders.forEach(slider => {
        const cat = slider.dataset.category;
        const original = parseFloat(slider.dataset.original);
        const current = parseFloat(slider.value);
        const diff = current - original;
        totalDelta += diff;

        // Update value display
        const valEl = document.querySelector(`[data-slider-value="${CSS.escape(cat)}"]`);
        if (valEl) valEl.textContent = fmtCurrency(current);

        // Update diff display
        const diffEl = document.querySelector(`[data-slider-diff="${CSS.escape(cat)}"]`);
        if (diffEl) {
            const absDiff = Math.abs(diff);
            if (diff > 0.5) {
                diffEl.textContent = `+$${absDiff.toFixed(0)}`;
                diffEl.className = 'slider-diff negative';
            } else if (diff < -0.5) {
                diffEl.textContent = `-$${absDiff.toFixed(0)}`;
                diffEl.className = 'slider-diff positive';
            } else {
                diffEl.textContent = '$0';
                diffEl.className = 'slider-diff neutral';
            }
        }
    });

    // Update delta display
    const deltaEl = document.getElementById('simulator-delta');
    const deltaAmountEl = document.getElementById('sim-delta-amount');
    if (deltaEl && deltaAmountEl) {
        if (Math.abs(totalDelta) > 0.5) {
            deltaEl.classList.add('active');
            const sign = totalDelta > 0 ? '+' : '-';
            deltaAmountEl.textContent = `${sign}$${Math.abs(totalDelta).toFixed(0)}`;
            deltaAmountEl.className = totalDelta > 0 ? 'delta-amount delta-negative' : 'delta-amount delta-positive';
        } else {
            deltaEl.classList.remove('active');
        }
    }

    // Update bars with delta (negative delta = spending less = more free cash)
    updateBarsWithDelta(-totalDelta);
}

function updateBarsWithDelta(delta) {
    const data = window._monthlyRunwayData;
    if (!data || !data.halves) return;

    data.halves.forEach((half, i) => {
        const idx = i + 1;
        const bar = document.getElementById(`half${idx}-bar`);
        const legendEl = document.getElementById(`half${idx}-legend`);
        const statsEl = document.getElementById(`half${idx}-stats`);

        if (!bar) return;

        const adjustedFreeCash = half.free_cash + delta;
        const freeCashClass = adjustedFreeCash >= 0 ? 'free-cash' : 'free-cash negative';

        statsEl.innerHTML = `
            <span class="half-stat spent">Spent<strong>${fmtCurrency(half.spent_so_far)}</strong></span>
            <span class="half-stat">Committed<strong>${fmtCurrency(half.committed)}</strong></span>
            <span class="half-stat ${freeCashClass}">Free<strong>${fmtCurrency(adjustedFreeCash)}</strong></span>
        `;

        renderHalfBar(bar, legendEl, half, delta);
    });
}

function resetSimulator() {
    const sliders = document.querySelectorAll('#simulator-sliders .slider-input');
    sliders.forEach(slider => {
        slider.value = Math.round(parseFloat(slider.dataset.original));
    });
    onSimulatorSliderChange();
}

// --- Budget Override Save ---

async function saveBudgetOverride(halfNum) {
    const input = document.getElementById(`half${halfNum}-budget`);
    const value = parseFloat(input.value);

    if (isNaN(value) || value < 0) {
        alert('Please enter a valid budget amount.');
        return;
    }

    const body = {};
    if (halfNum === 1) body.first_half = value;
    if (halfNum === 2) body.second_half = value;

    try {
        const resp = await fetch('/api/budget-overrides', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Save failed: ' + (result.message || 'Unknown error'));
        }
    } catch (err) {
        alert('Save failed: ' + err.message);
    }
}

// --- Temporary Expenses ---

async function submitTempExpense(event) {
    event.preventDefault();

    const name = document.getElementById('temp-name').value.trim();
    const amount = parseFloat(document.getElementById('temp-amount').value);
    const half = parseInt(document.getElementById('temp-half').value, 10);

    if (!name || isNaN(amount) || amount <= 0) {
        alert('Please fill in name and amount.');
        return;
    }

    try {
        const resp = await fetch('/api/temporary-expenses', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, amount, half }),
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Save failed: ' + (result.message || 'Unknown error'));
        }
    } catch (err) {
        alert('Save failed: ' + err.message);
    }
}

async function deleteTempExpense(name) {
    if (!confirm(`Remove temporary expense "${name}"?`)) return;

    try {
        const resp = await fetch('/api/temporary-expenses', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        const result = await resp.json();
        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Delete failed: ' + (result.message || 'Unknown error'));
        }
    } catch (err) {
        alert('Delete failed: ' + err.message);
    }
}

function renderTempExpenses(expenses) {
    const list = document.getElementById('temp-expense-list');
    if (!list) return;

    if (!expenses || expenses.length === 0) {
        list.innerHTML = '<span style="color:#64748b; font-size:0.8rem;">No temporary expenses configured.</span>';
        return;
    }

    list.innerHTML = expenses.map(te => {
        const halfLabel = te.half === 1 ? '1st half' : '2nd half';
        return `<div class="temp-expense-card">
            <span class="temp-expense-name">${escapeHtml(te.name)}</span>
            <span class="temp-expense-amount">${fmtCurrency(te.amount)}</span>
            <span class="temp-expense-half">${halfLabel}</span>
            <button class="temp-expense-delete" onclick="deleteTempExpense('${escapeAttr(te.name)}')" title="Remove">&times;</button>
        </div>`;
    }).join('');
}

async function refreshData() {
    const btn = document.getElementById('refresh-btn');
    btn.disabled = true;
    btn.textContent = 'Refreshing...';

    try {
        const resp = await fetch('/api/refresh', { method: 'POST' });
        const result = await resp.json();

        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Refresh failed: ' + (result.message || 'Unknown error'));
            btn.disabled = false;
            btn.textContent = 'Refresh Data';
        }
    } catch (err) {
        alert('Refresh failed: ' + err.message);
        btn.disabled = false;
        btn.textContent = 'Refresh Data';
    }
}

// --- Manual Balance Functions ---

function toggleManualSection() {
    const content = document.getElementById('manual-balance-content');
    const toggle = document.getElementById('manual-toggle');
    if (content.style.display === 'none') {
        content.style.display = 'block';
        toggle.textContent = '\u2212';  // minus sign
    } else {
        content.style.display = 'none';
        toggle.textContent = '+';
    }
}

async function submitManualBalance(event) {
    event.preventDefault();

    const account = document.getElementById('mb-account').value.trim();
    const date = document.getElementById('mb-date').value;
    const balance = parseFloat(document.getElementById('mb-balance').value);

    if (!account || !date || isNaN(balance)) {
        alert('Please fill in all fields.');
        return;
    }

    // Check if this is a new account (not in the known list)
    const isNewAccount = typeof KNOWN_MANUAL_ACCOUNTS !== 'undefined'
        && !KNOWN_MANUAL_ACCOUNTS.includes(account);

    if (isNewAccount) {
        // Show the 24-month history modal instead of saving just one entry
        openHistoryModal(account, date, balance);
        return;
    }

    // Existing account — just save the single entry
    const btn = document.getElementById('mb-submit-btn');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const resp = await fetch('/api/manual-balance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account, date, balance }),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Save failed: ' + (result.message || 'Unknown error'));
            btn.disabled = false;
            btn.textContent = 'Save';
        }
    } catch (err) {
        alert('Save failed: ' + err.message);
        btn.disabled = false;
        btn.textContent = 'Save';
    }
}

// --- 24-Month History Modal ---

function openHistoryModal(accountName, currentDate, currentBalance) {
    const modal = document.getElementById('history-modal');
    const nameSpan = document.getElementById('modal-account-name');
    const grid = document.getElementById('history-grid');
    const status = document.getElementById('modal-status');

    nameSpan.textContent = accountName;
    status.textContent = '';

    // Generate 24 months going back from today
    const today = new Date();
    const months = [];
    for (let i = 0; i < 24; i++) {
        const d = new Date(today.getFullYear(), today.getMonth() - i, 1);
        // Use last day of month as the snapshot date
        const lastDay = new Date(d.getFullYear(), d.getMonth() + 1, 0);
        const dateStr = lastDay.toISOString().split('T')[0];
        const label = lastDay.toLocaleDateString('en-US', { year: 'numeric', month: 'short' });
        months.push({ dateStr, label });
    }

    // Reverse so oldest is first
    months.reverse();

    grid.innerHTML = months.map((m, i) => {
        // Pre-fill the most recent month with the balance they entered
        const isCurrentMonth = (i === months.length - 1);
        const prefill = isCurrentMonth ? currentBalance : '';

        return `<div class="history-row">
            <label>${m.label}</label>
            <input type="number" step="0.01" min="0"
                   data-date="${m.dateStr}"
                   placeholder="Balance"
                   value="${prefill}"
                   class="history-input">
        </div>`;
    }).join('');

    modal.style.display = 'flex';

    // Focus the first empty input
    const firstEmpty = grid.querySelector('input:not([value])') || grid.querySelector('input');
    if (firstEmpty) firstEmpty.focus();
}

function closeHistoryModal() {
    document.getElementById('history-modal').style.display = 'none';
}

async function saveHistoryEntries() {
    const accountName = document.getElementById('modal-account-name').textContent;
    const inputs = document.querySelectorAll('#history-grid .history-input');
    const btn = document.getElementById('modal-save-btn');
    const status = document.getElementById('modal-status');

    // Collect non-empty entries
    const entries = [];
    inputs.forEach(input => {
        const val = input.value.trim();
        if (val !== '') {
            entries.push({
                account: accountName,
                date: input.dataset.date,
                balance: parseFloat(val),
            });
        }
    });

    if (entries.length === 0) {
        status.textContent = 'Enter at least one balance.';
        status.className = 'bulk-status error';
        return;
    }

    btn.disabled = true;
    btn.textContent = `Saving ${entries.length} entries...`;
    status.textContent = '';

    try {
        const resp = await fetch('/api/manual-balance/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entries }),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            status.textContent = `Saved ${result.saved} entries!`;
            status.className = 'bulk-status success';
            setTimeout(() => location.reload(), 800);
        } else {
            status.textContent = 'Save failed: ' + (result.message || 'Unknown error');
            status.className = 'bulk-status error';
            btn.disabled = false;
            btn.textContent = 'Save All';
        }
    } catch (err) {
        status.textContent = 'Save failed: ' + err.message;
        status.className = 'bulk-status error';
        btn.disabled = false;
        btn.textContent = 'Save All';
    }
}

async function loadManualBalances() {
    const tbody = document.getElementById('manual-history-body');
    if (!tbody) return;

    try {
        const entries = await fetch('/api/manual-balances').then(r => r.json());

        renderQuickUpdatePanel(entries);

        if (!entries.length) {
            tbody.innerHTML = '<tr><td colspan="4" class="empty-msg">No manual entries yet</td></tr>';
            return;
        }

        tbody.innerHTML = entries.map(entry => {
            const bal = parseFloat(entry.balance);
            const formatted = bal < 0
                ? '-$' + Math.abs(bal).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
                : '$' + bal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

            return `<tr>
                <td>${escapeHtml(entry.account)}</td>
                <td>${escapeHtml(entry.date)}</td>
                <td>${formatted}</td>
                <td>
                    <button class="delete-btn" onclick="deleteManualBalance('${escapeAttr(entry.account)}', '${escapeAttr(entry.date)}')">
                        &times;
                    </button>
                </td>
            </tr>`;
        }).join('');
    } catch (err) {
        console.error('Failed to load manual balances:', err);
        tbody.innerHTML = '<tr><td colspan="4" class="empty-msg">Failed to load history</td></tr>';
    }
}

const STALE_THRESHOLD_DAYS = 30;

function daysSince(dateStr) {
    const then = new Date(dateStr + 'T00:00:00');
    const now = new Date();
    now.setHours(0, 0, 0, 0);
    return Math.round((now - then) / 86400000);
}

function renderQuickUpdatePanel(entries) {
    const section = document.getElementById('mb-quick-update-section');
    const grid = document.getElementById('mb-quick-grid');
    if (!section || !grid) return;

    if (!entries || !entries.length) {
        section.style.display = 'none';
        grid.innerHTML = '';
        return;
    }

    // entries are sorted date DESC — first occurrence per account is the latest
    const latestByAccount = new Map();
    entries.forEach(entry => {
        if (!latestByAccount.has(entry.account)) {
            latestByAccount.set(entry.account, entry);
        }
    });

    const accounts = Array.from(latestByAccount.values()).sort((a, b) => a.account.localeCompare(b.account));

    section.style.display = 'block';
    grid.innerHTML = accounts.map(entry => {
        const age = daysSince(entry.date);
        const isStale = age > STALE_THRESHOLD_DAYS;
        const staleClass = isStale ? 'stale' : '';
        const ageLabel = age <= 0 ? 'Today' : `${age} day${age === 1 ? '' : 's'} old`;
        const bal = parseFloat(entry.balance);
        const formatted = bal < 0
            ? '-$' + Math.abs(bal).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
            : '$' + bal.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });

        return `<div class="mb-account-card">
            <div class="mb-account-card-top">
                <span class="mb-account-name" title="${escapeAttr(entry.account)}">${escapeHtml(entry.account)}</span>
                <span class="mb-staleness ${staleClass}">${ageLabel}</span>
            </div>
            <div class="mb-account-card-balance">${formatted}<span class="mb-account-date">as of ${escapeHtml(entry.date)}</span></div>
            <div class="mb-account-card-form">
                <input type="number" step="0.01" class="mb-quick-input" placeholder="New balance"
                       data-account="${escapeAttr(entry.account)}">
                <button onclick="quickUpdateBalance('${escapeAttr(entry.account)}', this)">Update</button>
            </div>
        </div>`;
    }).join('');
}

async function quickUpdateBalance(account, btn) {
    const grid = document.getElementById('mb-quick-grid');
    const input = grid ? grid.querySelector(`.mb-quick-input[data-account="${CSS.escape(account)}"]`) : null;
    if (!input) return;

    const balance = parseFloat(input.value);
    if (isNaN(balance)) {
        alert('Enter a balance first.');
        return;
    }

    const date = new Date().toISOString().split('T')[0];

    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const resp = await fetch('/api/manual-balance', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account, date, balance }),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Save failed: ' + (result.message || 'Unknown error'));
            btn.disabled = false;
            btn.textContent = 'Update';
        }
    } catch (err) {
        alert('Save failed: ' + err.message);
        btn.disabled = false;
        btn.textContent = 'Update';
    }
}

async function deleteManualBalance(account, date) {
    if (!confirm(`Delete ${account} entry for ${date}?`)) return;

    try {
        const resp = await fetch('/api/manual-balance', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account, date }),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Delete failed: ' + (result.message || 'Unknown error'));
        }
    } catch (err) {
        alert('Delete failed: ' + err.message);
    }
}

// --- Bulk Import Functions ---

function toggleBulkImport() {
    const content = document.getElementById('bulk-import-content');
    const toggle = document.getElementById('bulk-toggle');
    if (content.style.display === 'none') {
        content.style.display = 'block';
        toggle.textContent = '\u2212';
    } else {
        content.style.display = 'none';
        toggle.textContent = '+';
    }
}

async function submitBulkImport() {
    const textarea = document.getElementById('bulk-textarea');
    const btn = document.getElementById('bulk-submit-btn');
    const status = document.getElementById('bulk-status');
    const raw = textarea.value.trim();

    if (!raw) {
        status.textContent = 'Nothing to import.';
        status.className = 'bulk-status error';
        return;
    }

    // Parse lines: "Account Name, YYYY-MM-DD, Balance"
    const lines = raw.split('\n').filter(l => l.trim());
    const entries = [];
    const parseErrors = [];

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        if (!line) continue;

        // Split by comma, but account name might not have commas
        const parts = line.split(',');
        if (parts.length < 3) {
            parseErrors.push(`Line ${i + 1}: expected 3 comma-separated values`);
            continue;
        }

        // Last part is balance, second-to-last is date, everything before is account name
        const balance = parseFloat(parts[parts.length - 1].trim().replace(/[$,]/g, ''));
        const date = parts[parts.length - 2].trim();
        const account = parts.slice(0, parts.length - 2).join(',').trim();

        if (!account) {
            parseErrors.push(`Line ${i + 1}: missing account name`);
            continue;
        }
        if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
            parseErrors.push(`Line ${i + 1}: date must be YYYY-MM-DD (got "${date}")`);
            continue;
        }
        if (isNaN(balance)) {
            parseErrors.push(`Line ${i + 1}: invalid balance`);
            continue;
        }

        entries.push({ account, date, balance });
    }

    if (parseErrors.length > 0 && entries.length === 0) {
        status.textContent = 'All lines had errors: ' + parseErrors.join('; ');
        status.className = 'bulk-status error';
        return;
    }

    btn.disabled = true;
    btn.textContent = `Importing ${entries.length} entries...`;
    status.textContent = '';

    try {
        const resp = await fetch('/api/manual-balance/bulk', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entries }),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            let msg = `Imported ${result.saved} entries.`;
            if (parseErrors.length > 0) {
                msg += ` ${parseErrors.length} lines skipped.`;
            }
            if (result.errors && result.errors.length > 0) {
                msg += ` Server skipped ${result.errors.length}.`;
            }
            status.textContent = msg;
            status.className = 'bulk-status success';
            // Reload after short delay so user sees the message
            setTimeout(() => location.reload(), 1000);
        } else {
            status.textContent = 'Import failed: ' + (result.message || 'Unknown error');
            status.className = 'bulk-status error';
            btn.disabled = false;
            btn.textContent = 'Import All';
        }
    } catch (err) {
        status.textContent = 'Import failed: ' + err.message;
        status.className = 'bulk-status error';
        btn.disabled = false;
        btn.textContent = 'Import All';
    }
}

// --- Recurring Bills Management ---

function toggleRecurringBills() {
    const content = document.getElementById('recurring-bills-content');
    const toggle = document.getElementById('recurring-toggle');
    if (content.style.display === 'none') {
        content.style.display = 'block';
        toggle.textContent = '\u2212';
        loadRecurringBills();
    } else {
        content.style.display = 'none';
        toggle.textContent = '+';
    }
}

async function loadRecurringBills() {
    const tbody = document.getElementById('recurring-bills-body');
    if (!tbody) return;

    try {
        const bills = await fetch('/api/recurring-bills').then(r => r.json());
        const runwayData = window._runwayData || {};
        const billStatuses = runwayData.recurring_bills || [];

        if (!bills.length) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">No recurring bills configured</td></tr>';
            return;
        }

        tbody.innerHTML = bills.map(bill => {
            // Find status for this bill from runway data
            const statusInfo = billStatuses.find(s => s.name === bill.name);
            let statusHtml = '<span class="status-badge no-period">N/A</span>';
            if (statusInfo) {
                if (statusInfo.status === 'paid') {
                    statusHtml = '<span class="status-badge paid">Paid</span>';
                } else {
                    statusHtml = '<span class="status-badge pending">Pending</span>';
                }
            }

            const keywords = bill.match_criteria.join(', ');

            return `<tr>
                <td style="font-weight: 500;">${escapeHtml(bill.name)}</td>
                <td>$${bill.amount.toFixed(2)}</td>
                <td>${bill.day_of_month}</td>
                <td><span class="match-keywords" title="${escapeAttr(keywords)}">${escapeHtml(keywords) || '—'}</span></td>
                <td>${statusHtml}</td>
                <td>
                    <button class="delete-btn" onclick="deleteRecurringBill('${escapeAttr(bill.name)}')" title="Remove bill">
                        &times;
                    </button>
                </td>
            </tr>`;
        }).join('');
    } catch (err) {
        console.error('Failed to load recurring bills:', err);
        tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">Failed to load bills</td></tr>';
    }
}

async function submitRecurringBill(event) {
    event.preventDefault();

    const name = document.getElementById('rb-name').value.trim();
    const amount = parseFloat(document.getElementById('rb-amount').value);
    const day = parseInt(document.getElementById('rb-day').value, 10);
    const matchRaw = document.getElementById('rb-match').value.trim();

    if (!name || isNaN(amount) || isNaN(day)) {
        alert('Please fill in name, amount, and day of month.');
        return;
    }

    // Parse comma-separated match criteria
    const matchCriteria = matchRaw
        ? matchRaw.split(',').map(s => s.trim().toLowerCase()).filter(Boolean)
        : [name.toLowerCase()];

    const btn = document.getElementById('rb-submit-btn');
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const resp = await fetch('/api/recurring-bills', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                amount,
                day_of_month: day,
                match_criteria: matchCriteria
            }),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            // Clear form
            document.getElementById('rb-name').value = '';
            document.getElementById('rb-amount').value = '';
            document.getElementById('rb-day').value = '';
            document.getElementById('rb-match').value = '';
            btn.disabled = false;
            btn.textContent = 'Add Bill';

            // Reload page to refresh runway calculations
            location.reload();
        } else {
            alert('Save failed: ' + (result.message || 'Unknown error'));
            btn.disabled = false;
            btn.textContent = 'Add Bill';
        }
    } catch (err) {
        alert('Save failed: ' + err.message);
        btn.disabled = false;
        btn.textContent = 'Add Bill';
    }
}

async function deleteRecurringBill(name) {
    if (!confirm(`Delete recurring bill "${name}"?`)) return;

    try {
        const resp = await fetch('/api/recurring-bills', {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        const result = await resp.json();

        if (result.status === 'ok') {
            location.reload();
        } else {
            alert('Delete failed: ' + (result.message || 'Unknown error'));
        }
    } catch (err) {
        alert('Delete failed: ' + err.message);
    }
}

// --- Helpers ---

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function escapeAttr(str) {
    return str.replace(/'/g, "\\'").replace(/"/g, '&quot;');
}
