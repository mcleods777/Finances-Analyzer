// The Desk — conversational CFO. Talks to the Wave 1 endpoints in
// finance/blueprints/desk.py (/api/chat, /api/conversations, /api/profile).

const CHAT_TIMEOUT_MS = 180000;
const MODEL_COSTS = {
    'claude-haiku-4-5': '~1¢ / question',
    'claude-sonnet-5': '~5¢ / question',
    'claude-opus-4-8': '~20¢ / question',
};
const DEFAULT_MODEL = 'claude-sonnet-5';
const DEFAULT_INTEL = 'standard';

const deskState = {
    conversationId: null,
    model: DEFAULT_MODEL,
    intelligence: DEFAULT_INTEL,
    sending: false,
};

document.addEventListener('DOMContentLoaded', () => {
    setPicker(DEFAULT_MODEL, DEFAULT_INTEL);
    initComposer();
    document.getElementById('conv-new').addEventListener('click', newConversation);

    const match = (location.hash || '').match(/^#conversation-(\d+)$/);
    loadConversations().then(() => {
        if (match) openConversation(parseInt(match[1], 10));
    });
});

// --- Helpers (same conventions as dashboard.js) ---

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// Typeset numbers inside serif prose in Plex Sans tabular (the "data voice").
function wrapProseFigures(escapedText) {
    return escapedText.replace(
        /(\$[\d,]+(?:\.\d+)?|\d[\d,]*(?:\.\d+)?%?)/g,
        '<span class="figure">$1</span>'
    );
}

function convDate(iso) {
    if (!iso) return '';
    const d = new Date(iso.replace(' ', 'T'));
    if (isNaN(d)) return '';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// --- Picker ---

function setPicker(model, intelligence) {
    deskState.model = model;
    deskState.intelligence = intelligence;
    document.querySelectorAll('#model-control button').forEach(b =>
        b.classList.toggle('active', b.dataset.model === model));
    document.querySelectorAll('#intel-control button').forEach(b =>
        b.classList.toggle('active', b.dataset.intel === intelligence));
    document.getElementById('cost-hint').textContent = 'est. ' + (MODEL_COSTS[model] || '');
}

document.addEventListener('click', (e) => {
    const modelBtn = e.target.closest('#model-control button');
    if (modelBtn) setPicker(modelBtn.dataset.model, deskState.intelligence);
    const intelBtn = e.target.closest('#intel-control button');
    if (intelBtn) setPicker(deskState.model, intelBtn.dataset.intel);
});

// --- Conversation rail ---

async function loadConversations() {
    const list = document.getElementById('conv-list');
    try {
        const data = await fetch('/api/conversations').then(r => r.json());
        const conversations = data.conversations || [];
        if (!conversations.length) {
            list.innerHTML = '<div class="conv-empty microtype">No conversations yet</div>';
            return;
        }
        list.innerHTML = '';
        conversations.forEach(c => list.appendChild(convRow(c)));
        markActiveRow();
    } catch (err) {
        console.error('Failed to load conversations:', err);
        list.innerHTML = '<div class="conv-empty microtype">Failed to load</div>';
    }
}

function convRow(c) {
    const row = document.createElement('div');
    row.className = 'conv-row';
    row.dataset.id = c.id;

    const title = document.createElement('span');
    title.className = 'conv-title';
    title.textContent = c.title;
    title.title = 'Double-click to rename';

    const date = document.createElement('span');
    date.className = 'conv-date microtype';
    date.textContent = convDate(c.updated_at || c.created_at);

    const del = document.createElement('button');
    del.className = 'conv-delete';
    del.innerHTML = '&times;';
    del.title = 'Delete conversation';

    row.appendChild(title);
    row.appendChild(date);
    row.appendChild(del);

    row.addEventListener('click', (e) => {
        if (e.target === del || title.querySelector('input')) return;
        openConversation(c.id);
    });
    title.addEventListener('dblclick', (e) => {
        e.stopPropagation();
        startRename(title, c);
    });
    del.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm(`Delete conversation "${c.title}"?`)) return;
        await fetch(`/api/conversations/${c.id}`, { method: 'DELETE' });
        if (deskState.conversationId === c.id) newConversation();
        loadConversations();
    });
    return row;
}

function startRename(titleEl, c) {
    const input = document.createElement('input');
    input.value = c.title;
    titleEl.textContent = '';
    titleEl.appendChild(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (save) => {
        if (done) return;
        done = true;
        const newTitle = input.value.trim();
        if (save && newTitle && newTitle !== c.title) {
            await fetch(`/api/conversations/${c.id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: newTitle }),
            });
            c.title = newTitle;
        }
        titleEl.textContent = c.title;
    };
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') finish(true);
        if (e.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', () => finish(true));
    input.addEventListener('click', (e) => e.stopPropagation());
}

function markActiveRow() {
    document.querySelectorAll('.conv-row').forEach(r =>
        r.classList.toggle('active', parseInt(r.dataset.id, 10) === deskState.conversationId));
}

// --- Opening / new conversation ---

function clearMessages() {
    const pane = document.getElementById('desk-messages');
    pane.innerHTML = '';
    return pane;
}

function newConversation() {
    deskState.conversationId = null;
    history.replaceState(null, '', '/desk');
    const pane = clearMessages();
    pane.innerHTML = '<div class="desk-hint">Ask the CFO anything about your money — spending, bills, runway, what changed. Answers come from your live ledger.</div>';
    setPicker(DEFAULT_MODEL, DEFAULT_INTEL);
    document.getElementById('composer-input').focus();
    markActiveRow();
}

async function openConversation(id) {
    try {
        const resp = await fetch(`/api/conversations/${id}`);
        if (!resp.ok) return;
        const data = await resp.json();
        deskState.conversationId = id;
        history.replaceState(null, '', `/desk#conversation-${id}`);
        setPicker(data.model || DEFAULT_MODEL, data.intelligence || DEFAULT_INTEL);
        const pane = clearMessages();
        (data.messages || []).forEach(m => appendTurn(m.role, m.display_text));
        pane.scrollTop = pane.scrollHeight;
        markActiveRow();
    } catch (err) {
        console.error('Failed to open conversation:', err);
    }
}

// --- Rendering turns ---

function appendTurn(role, text, extras) {
    const pane = document.getElementById('desk-messages');
    const hint = pane.querySelector('.desk-hint');
    if (hint) hint.remove();

    const turn = document.createElement('div');
    turn.className = `desk-turn ${role}`;

    const body = document.createElement('div');
    body.className = 'turn-body';
    if (role === 'assistant') {
        // Replies may echo the advisor's <data>…</data> injection-defense
        // wrappers around merchant/category names — strip them for display.
        const clean = String(text).replace(/<\/?data>/g, '');
        body.innerHTML = wrapProseFigures(escapeHtml(clean));
    } else {
        body.textContent = text;
    }
    turn.appendChild(body);

    if (extras && extras.toolActivity && extras.toolActivity.length) {
        const tools = document.createElement('div');
        tools.className = 'turn-tools microtype';
        const names = extras.toolActivity.map(t => escapeHtml(t.tool)).join(' · ');
        tools.innerHTML = `Consulted: ${names}`;
        tools.title = extras.toolActivity
            .map(t => t.tool + (t.summary ? ` — ${t.summary}` : '')).join('\n');
        turn.appendChild(tools);
    }

    const chips = chipRows(extras);
    if (chips) turn.appendChild(chips);

    pane.appendChild(turn);
    pane.scrollTop = pane.scrollHeight;
    return turn;
}

function chipRows(extras) {
    if (!extras) return null;
    const saved = extras.insightsSaved || [];
    const profile = extras.profileChanges || [];
    if (!saved.length && !profile.length) return null;

    const wrap = document.createElement('div');
    wrap.className = 'turn-chips';

    saved.forEach(() => {
        const chip = document.createElement('div');
        chip.className = 'desk-chip';
        chip.innerHTML = 'Filed to the Archive <a href="/archive" class="chip-undo">view &rarr;</a>';
        wrap.appendChild(chip);
    });

    profile.forEach(change => {
        const chip = document.createElement('div');
        chip.className = 'desk-chip';
        const verb = change.action === 'removed' ? 'Removed from dossier'
            : change.action === 'updated' ? 'Updated in dossier' : 'Added to dossier';
        const snippet = change.text.length > 60 ? change.text.slice(0, 60) + '…' : change.text;
        chip.innerHTML = `${verb}: ${escapeHtml(snippet)}`;
        if (change.action !== 'removed') {
            const undo = document.createElement('a');
            undo.className = 'chip-undo';
            undo.href = '#';
            undo.textContent = 'undo';
            undo.addEventListener('click', async (e) => {
                e.preventDefault();
                const resp = await fetch(`/api/profile/${change.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ active: false }),
                });
                if (resp.ok) chip.innerHTML = 'Removed from dossier';
            });
            chip.appendChild(document.createTextNode(' '));
            chip.appendChild(undo);
        }
        wrap.appendChild(chip);
    });
    return wrap;
}

// --- Composer / send ---

function initComposer() {
    const input = document.getElementById('composer-input');
    const send = document.getElementById('composer-send');

    input.addEventListener('input', () => {
        input.style.height = 'auto';
        input.style.height = Math.min(input.scrollHeight, 180) + 'px';
    });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    });
    send.addEventListener('click', () => sendMessage());
}

function showThinking() {
    const pane = document.getElementById('desk-messages');
    const el = document.createElement('div');
    el.className = 'desk-thinking microtype';
    el.id = 'desk-thinking';
    el.textContent = 'The CFO is considering…';
    pane.appendChild(el);
    pane.scrollTop = pane.scrollHeight;
}

function hideThinking() {
    const el = document.getElementById('desk-thinking');
    if (el) el.remove();
}

function showError(message, retryText, options) {
    const pane = document.getElementById('desk-messages');
    const el = document.createElement('div');
    el.className = 'desk-error';
    if (options && options.hint) {
        el.innerHTML = escapeHtml(message);
    } else {
        el.innerHTML = escapeHtml(message) + ' ';
        const retry = document.createElement('a');
        retry.href = '#';
        retry.textContent = 'Retry';
        retry.addEventListener('click', (e) => {
            e.preventDefault();
            el.remove();
            sendMessage(retryText);
        });
        el.appendChild(retry);
    }
    pane.appendChild(el);
    pane.scrollTop = pane.scrollHeight;
}

async function sendMessage(retryText) {
    if (deskState.sending) return;
    const input = document.getElementById('composer-input');
    const sendBtn = document.getElementById('composer-send');
    const message = retryText !== undefined ? retryText : input.value.trim();
    if (!message) return;

    deskState.sending = true;
    sendBtn.disabled = true;
    input.disabled = true;

    if (retryText === undefined) {
        appendTurn('user', message);
        input.value = '';
        input.style.height = 'auto';
    }
    showThinking();

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS);

    try {
        const resp = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            signal: controller.signal,
            body: JSON.stringify({
                conversation_id: deskState.conversationId,
                message,
                model: deskState.model,
                intelligence: deskState.intelligence,
            }),
        });
        const data = await resp.json();
        hideThinking();

        if (!resp.ok) {
            if (data.error === 'advisor_not_configured' || data.error === 'no_data_loaded') {
                showError(data.message || 'The Desk is not configured.', message, { hint: true });
            } else {
                showError(data.message || 'The Desk is unavailable.', message);
            }
            return;
        }

        const isNew = deskState.conversationId === null;
        deskState.conversationId = data.conversation_id;
        history.replaceState(null, '', `/desk#conversation-${data.conversation_id}`);
        appendTurn('assistant', data.reply, {
            toolActivity: data.tool_activity,
            insightsSaved: data.insights_saved,
            profileChanges: data.profile_changes,
        });
        if (isNew) loadConversations(); else markActiveRow();
    } catch (err) {
        hideThinking();
        if (err.name === 'AbortError') {
            showError('The Desk timed out.', message);
        } else {
            console.error('Chat failed:', err);
            showError('The Desk is unavailable.', message);
        }
    } finally {
        clearTimeout(timer);
        deskState.sending = false;
        sendBtn.disabled = false;
        input.disabled = false;
        input.focus();
    }
}
