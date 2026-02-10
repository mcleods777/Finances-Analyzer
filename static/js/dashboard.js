document.addEventListener('DOMContentLoaded', async () => {
    // Set default date for manual balance form to today
    const dateInput = document.getElementById('mb-date');
    if (dateInput) {
        dateInput.value = new Date().toISOString().split('T')[0];
    }

    try {
        const [netWorthData, spendingData, incomeData, runwayData, breakdownData] = await Promise.all([
            fetch('/api/net-worth').then(r => r.json()),
            fetch('/api/biweekly-spending').then(r => r.json()),
            fetch('/api/biweekly-income').then(r => r.json()),
            fetch('/api/runway').then(r => r.json()),
            fetch('/api/spending-breakdown?days=30').then(r => r.json()),
        ]);

        renderNetWorthChart(netWorthData);
        renderIncomeVsExpenseChart(spendingData, incomeData);
        renderBiweeklyChart(spendingData);
        updateRunwayBar(runwayData);
        renderSpendingChart(breakdownData, 30);
    } catch (err) {
        console.error('Failed to load dashboard data:', err);
    }

    // Load manual balance history
    loadManualBalances();
});

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

function updateRunwayBar(data) {
    const bar = document.getElementById('runway-bar');
    if (!bar || !data.avg_biweekly_spending) return;

    const fmtCurrency = (v) => v < 0 ? `-$${Math.abs(v).toFixed(2)}` : `$${v.toFixed(2)}`;

    // Calculate what percentage of the budget has been used this period
    const totalBudget = data.avg_biweekly_spending;
    const remaining = data.budget_remaining_this_period;
    const spent = totalBudget - remaining;
    const pctRemaining = Math.max(0, Math.min(100, (remaining / totalBudget) * 100));

    // Get pending bills only
    const allBills = data.recurring_bills || [];
    const pendingBills = allBills.filter(b => b.status === 'pending');
    const pendingTotal = data.pending_bills_total || 0;
    const freeCash = data.free_cash || remaining;

    // Free cash health color
    const healthPct = remaining > 0 ? (freeCash / totalBudget) * 100 : 0;
    const freeColor = healthPct > 35 ? '#22c55e' : (healthPct > 15 ? '#f59e0b' : '#ef4444');

    // Distinct colors for each bill segment
    const billColors = [
        '#f59e0b', '#f97316', '#ef4444', '#ec4899',
        '#a855f7', '#6366f1', '#0ea5e9', '#14b8a6',
    ];

    // Build segmented bar
    bar.style.width = pctRemaining + '%';
    bar.style.background = 'none';

    // Create segments container
    let segmentsEl = bar.querySelector('.runway-segments');
    if (!segmentsEl) {
        bar.innerHTML = '';
        segmentsEl = document.createElement('div');
        segmentsEl.className = 'runway-segments';
        bar.appendChild(segmentsEl);
    }
    segmentsEl.innerHTML = '';

    if (remaining <= 0) {
        bar.style.width = '0%';
    } else if (pendingBills.length === 0) {
        // No pending bills — full green bar
        segmentsEl.innerHTML = `
            <div class="runway-segment" style="width: 100%; background: ${freeColor};">
                <div class="segment-tooltip">
                    <div class="segment-tooltip-name">Free Cash</div>
                    <div class="segment-tooltip-amount">${fmtCurrency(remaining)}</div>
                </div>
            </div>`;
    } else {
        // Free cash segment
        const freePct = Math.max(0, (freeCash / remaining) * 100);

        if (freePct > 0) {
            segmentsEl.innerHTML += `
                <div class="runway-segment" style="width: ${freePct}%; background: ${freeColor};">
                    <div class="segment-tooltip">
                        <div class="segment-tooltip-name">Free Cash</div>
                        <div class="segment-tooltip-amount">${fmtCurrency(freeCash)}</div>
                    </div>
                </div>`;
        }

        // Individual bill segments — sorted by due date
        const sortedBills = [...pendingBills].sort((a, b) => {
            return new Date(a.due_date) - new Date(b.due_date);
        });

        sortedBills.forEach((bill, i) => {
            const billPct = Math.max(0.5, (bill.amount / remaining) * 100); // min 0.5% so tiny bills are visible
            const color = billColors[i % billColors.length];
            const dueDate = new Date(bill.due_date + 'T00:00:00');
            const dateStr = dueDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

            segmentsEl.innerHTML += `
                <div class="runway-segment" style="width: ${billPct}%; background: ${color};">
                    <div class="segment-tooltip">
                        <div class="segment-tooltip-name">${escapeHtml(bill.name)}</div>
                        <div class="segment-tooltip-amount">${fmtCurrency(bill.amount)}</div>
                        <div class="segment-tooltip-date">Due ${dateStr}</div>
                    </div>
                </div>`;
        });
    }

    // Update bar legend — show free cash + count of pending
    const barContainer = bar.parentElement;
    let legendEl = barContainer.parentElement.querySelector('.runway-bar-legend');
    if (pendingBills.length > 0) {
        if (!legendEl) {
            legendEl = document.createElement('div');
            legendEl.className = 'runway-bar-legend';
            barContainer.parentElement.insertBefore(legendEl, barContainer.nextSibling);
        }
        legendEl.innerHTML = `
            <span class="runway-bar-legend-item">
                <span class="runway-bar-legend-dot" style="background: ${freeColor};"></span>
                Free: ${fmtCurrency(freeCash)}
            </span>
            <span class="runway-bar-legend-item">
                <span class="runway-bar-legend-dot" style="background: #f59e0b;"></span>
                ${pendingBills.length} pending bill${pendingBills.length !== 1 ? 's' : ''}: ${fmtCurrency(pendingTotal)}
            </span>
        `;
    } else if (legendEl) {
        legendEl.remove();
    }

    // Populate Pending Bills Cards
    const container = document.getElementById('pending-bills-container');
    const list = document.getElementById('pending-bills-list');
    const totalEl = document.getElementById('pending-bills-total');

    if (container && list) {
        if (allBills.length > 0) {
            container.style.display = 'block';

            const pendingCount = pendingBills.length;
            if (totalEl) {
                totalEl.textContent = pendingTotal > 0
                    ? `${pendingCount} pending \u2022 $${pendingTotal.toFixed(2)} committed`
                    : 'All paid \u2714';
                totalEl.style.color = pendingTotal > 0 ? '#fbbf24' : '#4ade80';
            }

            list.innerHTML = allBills.map((b, idx) => {
                const isPaid = b.status === 'paid';
                const statusClass = isPaid ? 'paid' : 'pending';
                const dueDate = new Date(b.due_date + 'T00:00:00');
                const dateStr = dueDate.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });

                // Match color to bar segment for pending bills
                let dotColor = '#4ade80'; // green for paid
                if (!isPaid) {
                    const pendingIdx = pendingBills.findIndex(pb => pb.name === b.name);
                    dotColor = billColors[pendingIdx % billColors.length];
                }

                return `<div class="bill-card ${statusClass}">
                    <div class="bill-card-name">
                        <span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${dotColor};margin-right:6px;vertical-align:middle;"></span>
                        ${escapeHtml(b.name)}
                    </div>
                    <div class="bill-card-row">
                        <span class="bill-card-amount ${statusClass}">$${b.amount.toFixed(2)}</span>
                        <span class="bill-card-date">Due ${dateStr}</span>
                    </div>
                    <span class="bill-card-status ${statusClass}">
                        ${isPaid ? '\u2714 Paid' + (b.paid_date ? ' ' + new Date(b.paid_date + 'T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) : '') : '\u25CB Pending'}
                    </span>
                </div>`;
            }).join('');
        } else {
            container.style.display = 'none';
        }
    }

    // Store runway data globally so recurring bills table can use it
    window._runwayData = data;
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
