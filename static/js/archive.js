// The Archive — dossier (profile entries) + the permanent insight log.
// Talks to /api/profile and /api/insights (finance/blueprints/desk.py).

const logState = { page: 1, source: '', q: '', pageSize: 50, total: 0 };

document.addEventListener('DOMContentLoaded', () => {
    loadDossier();
    loadInsights();

    document.querySelectorAll('.dossier-add-btn').forEach(btn =>
        btn.addEventListener('click', () => startAdd(btn.dataset.section)));

    const search = document.getElementById('log-search');
    let debounce;
    search.addEventListener('input', () => {
        clearTimeout(debounce);
        debounce = setTimeout(() => {
            logState.q = search.value.trim();
            logState.page = 1;
            loadInsights();
        }, 250);
    });

    document.querySelectorAll('#source-tabs button').forEach(btn =>
        btn.addEventListener('click', () => {
            document.querySelectorAll('#source-tabs button').forEach(b =>
                b.classList.toggle('active', b === btn));
            logState.source = btn.dataset.source;
            logState.page = 1;
            loadInsights();
        }));

    document.getElementById('log-prev').addEventListener('click', (e) => {
        e.preventDefault();
        if (logState.page > 1) { logState.page--; loadInsights(); }
    });
    document.getElementById('log-next').addEventListener('click', (e) => {
        e.preventDefault();
        if (logState.page * logState.pageSize < logState.total) { logState.page++; loadInsights(); }
    });
});

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// --- Dossier ---

async function loadDossier() {
    try {
        const sections = await fetch('/api/profile').then(r => r.json());
        Object.keys(sections).forEach(section => {
            const container = document.querySelector(
                `.dossier-section[data-section="${section}"] .dossier-rows`);
            if (!container) return;
            container.innerHTML = '';
            const entries = sections[section];
            if (!entries.length) {
                container.innerHTML = '<div class="dossier-empty microtype">Nothing on file</div>';
                return;
            }
            entries.forEach(entry => container.appendChild(dossierRow(entry)));
        });
    } catch (err) {
        console.error('Failed to load dossier:', err);
    }
}

function dossierRow(entry) {
    const row = document.createElement('div');
    row.className = 'dossier-row';

    const text = document.createElement('span');
    text.className = 'dossier-text';
    text.textContent = entry.text;
    text.title = 'Click to edit';
    text.addEventListener('click', () => startEdit(row, text, entry));

    row.appendChild(text);

    if (entry.source === 'ai') {
        const mark = document.createElement('span');
        mark.className = 'ai-mark';
        mark.textContent = 'AI';
        mark.title = 'Added by the CFO from a conversation';
        row.appendChild(mark);
    }

    const del = document.createElement('button');
    del.className = 'dossier-delete';
    del.innerHTML = '&times;';
    del.title = 'Remove from dossier';
    del.addEventListener('click', async () => {
        const resp = await fetch(`/api/profile/${entry.id}`, { method: 'DELETE' });
        if (!resp.ok) return;
        showUndoChip(row, entry);
    });
    row.appendChild(del);
    return row;
}

// Delete = soft-deactivate; the row becomes a brief undo chip.
function showUndoChip(row, entry) {
    row.innerHTML = '';
    const chip = document.createElement('span');
    chip.className = 'microtype dossier-undo-chip';
    const snippet = entry.text.length > 40 ? entry.text.slice(0, 40) + '…' : entry.text;
    chip.innerHTML = `Removed: ${escapeHtml(snippet)} `;
    const undo = document.createElement('a');
    undo.href = '#';
    undo.textContent = 'undo';
    undo.addEventListener('click', async (e) => {
        e.preventDefault();
        await fetch(`/api/profile/${entry.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ active: true }),
        });
        loadDossier();
    });
    chip.appendChild(undo);
    row.appendChild(chip);
    setTimeout(() => { if (row.contains(chip)) loadDossier(); }, 8000);
}

function startEdit(row, textEl, entry) {
    if (row.querySelector('input')) return;
    const input = document.createElement('input');
    input.value = entry.text;
    textEl.replaceWith(input);
    input.focus();

    let done = false;
    const finish = async (save) => {
        if (done) return;
        done = true;
        const newText = input.value.trim();
        if (save && newText && newText !== entry.text) {
            const resp = await fetch(`/api/profile/${entry.id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text: newText }),
            });
            if (resp.ok) entry.text = newText;
        }
        textEl.textContent = entry.text;
        input.replaceWith(textEl);
    };
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') finish(true);
        if (e.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', () => finish(true));
}

function startAdd(section) {
    const container = document.querySelector(
        `.dossier-section[data-section="${section}"] .dossier-rows`);
    if (!container || container.querySelector('.dossier-add-row')) return;

    const empty = container.querySelector('.dossier-empty');
    if (empty) empty.remove();

    const row = document.createElement('div');
    row.className = 'dossier-row dossier-add-row';
    const input = document.createElement('input');
    input.placeholder = 'New entry…';
    row.appendChild(input);
    container.prepend(row);
    input.focus();

    let done = false;
    const finish = async (save) => {
        if (done) return;
        done = true;
        const text = input.value.trim();
        if (save && text) {
            await fetch('/api/profile', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ section, text }),
            });
        }
        loadDossier();
    };
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') finish(true);
        if (e.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', () => finish(true));
}

// --- Insight log ---

function datelineFor(createdAt) {
    const d = new Date(String(createdAt).replace(' ', 'T'));
    if (isNaN(d)) return String(createdAt);
    return d.toLocaleDateString(undefined, {
        weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
    });
}

async function loadInsights() {
    const container = document.getElementById('log-entries');
    try {
        const params = new URLSearchParams();
        if (logState.source) params.set('source', logState.source);
        if (logState.q) params.set('q', logState.q);
        if (logState.page > 1) params.set('page', String(logState.page));
        const data = await fetch('/api/insights?' + params.toString()).then(r => r.json());
        logState.total = data.total || 0;
        logState.pageSize = data.page_size || 50;

        const countEl = document.getElementById('log-count');
        countEl.textContent = `${logState.total} insight${logState.total === 1 ? '' : 's'} on record`;

        const insights = data.insights || [];
        if (!insights.length) {
            container.innerHTML = '<div class="log-empty">Nothing in the archive yet. Briefings and Desk conversations file their findings here.</div>';
            renderPagination();
            return;
        }

        container.innerHTML = '';
        let lastDate = null;
        insights.forEach(insight => {
            const day = String(insight.created_at).slice(0, 10);
            if (day !== lastDate) {
                lastDate = day;
                const dl = document.createElement('div');
                dl.className = 'log-dateline microtype';
                dl.textContent = datelineFor(insight.created_at);
                container.appendChild(dl);
            }
            container.appendChild(logEntry(insight));
        });
        renderPagination();
    } catch (err) {
        console.error('Failed to load insights:', err);
        container.innerHTML = '<div class="log-empty">Failed to load the archive.</div>';
    }
}

function logEntry(insight) {
    const row = document.createElement('div');
    row.className = 'log-entry';

    const source = document.createElement('span');
    source.className = `log-source microtype ${insight.source === 'chat' ? 'chat' : ''}`;
    source.textContent = insight.source === 'chat' ? 'Desk' : 'Briefing';
    row.appendChild(source);

    const text = document.createElement('span');
    text.className = 'log-text';
    // Stored insight text may carry the advisor's <data>…</data> injection-
    // defense wrappers — strip them for display.
    text.textContent = String(insight.text).replace(/<\/?data>/g, '');
    row.appendChild(text);

    if (insight.source === 'chat' && insight.conversation_id) {
        const link = document.createElement('a');
        link.className = 'log-conv-link microtype';
        link.href = `/desk#conversation-${insight.conversation_id}`;
        link.textContent = 'conversation →';
        row.appendChild(link);
    }

    const del = document.createElement('button');
    del.className = 'log-delete';
    del.innerHTML = '&times;';
    del.title = 'Delete this insight';
    del.addEventListener('click', async () => {
        if (!confirm('Delete this insight from the archive?')) return;
        const resp = await fetch(`/api/insights/${insight.id}`, { method: 'DELETE' });
        if (resp.ok) loadInsights();
    });
    row.appendChild(del);
    return row;
}

function renderPagination() {
    const wrap = document.getElementById('log-pagination');
    const pages = Math.max(1, Math.ceil(logState.total / logState.pageSize));
    if (pages <= 1) {
        wrap.style.display = 'none';
        return;
    }
    wrap.style.display = '';
    document.getElementById('log-page-info').textContent = `Page ${logState.page} of ${pages}`;
    document.getElementById('log-prev').classList.toggle('disabled', logState.page <= 1);
    document.getElementById('log-next').classList.toggle('disabled', logState.page >= pages);
}
