// Account management UI for the Accounts page: per-row kebab menu with
// Edit / Merge into… / Hide / Delete, a net-worth toggle, and an Unhide
// control in the hidden-accounts section. Plain JS, no build step.

(function () {
    'use strict';

    let openMenu = null;
    let activeAccount = null; // {id, name, type, institution, ...} from row data attrs
    let mergePreviewOk = false;

    document.addEventListener('DOMContentLoaded', () => {
        initRowMenus();
        initNetWorthToggles();
        initUnhideButtons();
        initModalClosers();
        initEditModal();
        initMergeModal();
        initDeleteModal();
    });

    // --- Helpers ---

    function esc(str) {
        const div = document.createElement('div');
        div.textContent = str == null ? '' : String(str);
        return div.innerHTML;
    }

    async function patchAccount(accountId, body) {
        const resp = await fetch(`/api/accounts/${accountId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `Request failed (${resp.status})`);
        return data;
    }

    function rowData(row) {
        return {
            id: row.dataset.accountId,
            name: row.dataset.name,
            type: row.dataset.type,
            source: row.dataset.source,
            institution: row.dataset.institution || '',
            excluded: row.dataset.exclude === '1',
            plaidLinked: row.dataset.plaidLinked === '1',
            txnCount: parseInt(row.dataset.txnCount || '0', 10),
            snapshotCount: parseInt(row.dataset.snapshotCount || '0', 10),
            importCount: parseInt(row.dataset.importCount || '0', 10),
        };
    }

    function setError(el, msg) {
        if (!el) return;
        el.className = 'upload-result' + (msg ? ' error' : '');
        el.textContent = msg || '';
    }

    function openModal(id) {
        document.getElementById(id).hidden = false;
    }

    function closeModal(id) {
        document.getElementById(id).hidden = true;
    }

    function initModalClosers() {
        document.querySelectorAll('[data-close-modal]').forEach((el) => {
            el.addEventListener('click', () => closeModal(el.dataset.closeModal));
        });
    }

    // --- Kebab menus ---

    function closeOpenMenu() {
        if (openMenu) {
            openMenu.remove();
            openMenu = null;
        }
    }

    function initRowMenus() {
        document.querySelectorAll('.account-menu-btn').forEach((btn) => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const row = btn.closest('.account-row');
                if (openMenu && openMenu.dataset.accountId === row.dataset.accountId) {
                    closeOpenMenu();
                    return;
                }
                closeOpenMenu();
                showMenuForRow(row, btn);
            });
        });
        document.addEventListener('click', closeOpenMenu);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeOpenMenu();
        });
    }

    function showMenuForRow(row, btn) {
        const account = rowData(row);
        const menu = document.createElement('div');
        menu.className = 'account-menu';
        menu.dataset.accountId = account.id;
        menu.addEventListener('click', (e) => e.stopPropagation());

        const items = [
            { label: 'Edit', action: () => openEditModal(account) },
            { label: 'Merge into…', action: () => openMergeModal(account) },
            { label: 'Hide', action: () => hideAccount(account, true) },
            { label: 'Delete…', action: () => openDeleteModal(account), danger: true },
        ];
        items.forEach((item) => {
            const el = document.createElement('button');
            el.type = 'button';
            el.textContent = item.label;
            if (item.danger) el.classList.add('danger');
            el.addEventListener('click', () => {
                closeOpenMenu();
                item.action();
            });
            menu.appendChild(el);
        });

        btn.closest('.account-actions-cell').appendChild(menu);
        openMenu = menu;
    }

    // --- Hide / unhide ---

    async function hideAccount(account, hidden) {
        try {
            await patchAccount(account.id, { hidden: hidden });
            window.location.reload();
        } catch (err) {
            console.error('Hide/unhide failed:', err);
            alert(err.message);
        }
    }

    function initUnhideButtons() {
        document.querySelectorAll('.unhide-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                btn.disabled = true;
                hideAccount({ id: btn.dataset.accountId }, false);
            });
        });
    }

    // --- Net worth toggle ---

    function initNetWorthToggles() {
        document.querySelectorAll('.nw-toggle').forEach((toggle) => {
            toggle.addEventListener('change', async () => {
                toggle.disabled = true;
                try {
                    await patchAccount(toggle.dataset.accountId, {
                        exclude_from_net_worth: !toggle.checked,
                    });
                    const row = toggle.closest('.account-row');
                    if (row) row.dataset.exclude = toggle.checked ? '0' : '1';
                } catch (err) {
                    console.error('Net worth toggle failed:', err);
                    toggle.checked = !toggle.checked; // revert
                    alert(err.message);
                } finally {
                    toggle.disabled = false;
                }
            });
        });
    }

    // --- Edit modal ---

    function initEditModal() {
        const form = document.getElementById('edit-account-form');
        if (!form) return;
        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            if (!activeAccount) return;
            const saveBtn = document.getElementById('edit-account-save-btn');
            const errorEl = document.getElementById('edit-account-error');
            setError(errorEl, '');
            saveBtn.disabled = true;
            try {
                await patchAccount(activeAccount.id, {
                    name: document.getElementById('edit-account-name').value,
                    type: document.getElementById('edit-account-type').value,
                    institution: document.getElementById('edit-account-institution').value || null,
                });
                window.location.reload();
            } catch (err) {
                console.error('Edit failed:', err);
                setError(errorEl, err.message);
                saveBtn.disabled = false;
            }
        });
    }

    function openEditModal(account) {
        activeAccount = account;
        document.getElementById('edit-account-name').value = account.name;
        const typeSelect = document.getElementById('edit-account-type');
        typeSelect.value = account.type;
        if (typeSelect.value !== account.type) {
            // Unknown type (e.g. legacy) — add it so we don't silently change it.
            typeSelect.appendChild(new Option(account.type, account.type, true, true));
        }
        document.getElementById('edit-account-institution').value = account.institution;
        setError(document.getElementById('edit-account-error'), '');
        document.getElementById('edit-account-save-btn').disabled = false;
        openModal('edit-account-modal');
    }

    // --- Merge modal ---

    function initMergeModal() {
        const select = document.getElementById('merge-target-select');
        const confirmBtn = document.getElementById('merge-confirm-btn');
        if (!select) return;

        select.addEventListener('change', () => loadMergePreview());

        confirmBtn.addEventListener('click', async () => {
            if (!activeAccount || !select.value || !mergePreviewOk) return;
            const errorEl = document.getElementById('merge-account-error');
            setError(errorEl, '');
            confirmBtn.disabled = true;
            confirmBtn.textContent = 'Merging…';
            try {
                const resp = await fetch(`/api/accounts/${activeAccount.id}/merge`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target_id: parseInt(select.value, 10) }),
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.error || `Merge failed (${resp.status})`);
                window.location.reload();
            } catch (err) {
                console.error('Merge failed:', err);
                setError(errorEl, err.message);
                confirmBtn.disabled = false;
                confirmBtn.textContent = 'Merge accounts';
            }
        });
    }

    function openMergeModal(account) {
        activeAccount = account;
        mergePreviewOk = false;
        document.getElementById('merge-account-title').textContent = `Merge "${account.name}" into…`;

        const select = document.getElementById('merge-target-select');
        select.innerHTML = '';
        select.appendChild(new Option('Choose a target account…', ''));
        document.querySelectorAll('.account-row').forEach((row) => {
            if (row.dataset.accountId !== account.id) {
                select.appendChild(new Option(row.dataset.name, row.dataset.accountId));
            }
        });

        document.getElementById('merge-preview-summary').hidden = true;
        document.getElementById('merge-overlap-wrap').hidden = true;
        setError(document.getElementById('merge-account-error'), '');
        const confirmBtn = document.getElementById('merge-confirm-btn');
        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Merge accounts';
        openModal('merge-account-modal');
    }

    async function loadMergePreview() {
        const select = document.getElementById('merge-target-select');
        const summaryEl = document.getElementById('merge-preview-summary');
        const overlapWrap = document.getElementById('merge-overlap-wrap');
        const confirmBtn = document.getElementById('merge-confirm-btn');
        const errorEl = document.getElementById('merge-account-error');

        mergePreviewOk = false;
        confirmBtn.disabled = true;
        setError(errorEl, '');
        summaryEl.hidden = true;
        overlapWrap.hidden = true;
        if (!activeAccount || !select.value) return;

        summaryEl.hidden = false;
        summaryEl.textContent = 'Computing preview…';
        try {
            const resp = await fetch(`/api/accounts/${activeAccount.id}/merge-preview`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_id: parseInt(select.value, 10) }),
            });
            const data = await resp.json();
            if (!resp.ok) throw new Error(data.error || `Preview failed (${resp.status})`);

            const targetName = select.options[select.selectedIndex].text;
            summaryEl.innerHTML =
                `<strong>${data.moving}</strong> transaction(s) will move to "${esc(targetName)}", ` +
                `<strong>${data.overlaps}</strong> overlap(s) will be skipped as duplicates, and ` +
                `<strong>${data.snapshots_moving}</strong> balance snapshot(s) will move. ` +
                `"${esc(activeAccount.name)}" will then be deleted.`;

            if (data.sample_overlaps && data.sample_overlaps.length) {
                const rows = data.sample_overlaps.map((o) => `
                    <tr>
                        <td>${esc(o.date)}</td>
                        <td>${Number(o.amount).toFixed(2)}</td>
                        <td>${esc(o.desc_source)}</td>
                        <td>${esc(o.desc_target)}</td>
                    </tr>
                `).join('');
                document.getElementById('merge-overlap-table').innerHTML =
                    '<thead><tr><th>Date</th><th>Amount</th><th>Source desc</th><th>Target desc</th></tr></thead>' +
                    `<tbody>${rows}</tbody>`;
                overlapWrap.hidden = false;
            }

            mergePreviewOk = true;
            confirmBtn.disabled = false;
        } catch (err) {
            console.error('Merge preview failed:', err);
            summaryEl.hidden = true;
            setError(errorEl, err.message);
        }
    }

    // --- Delete modal ---

    function initDeleteModal() {
        const confirmBtn = document.getElementById('delete-confirm-btn');
        if (!confirmBtn) return;
        confirmBtn.addEventListener('click', async () => {
            if (!activeAccount) return;
            const errorEl = document.getElementById('delete-account-error');
            setError(errorEl, '');
            confirmBtn.disabled = true;
            confirmBtn.textContent = 'Deleting…';
            try {
                const resp = await fetch(`/api/accounts/${activeAccount.id}`, {
                    method: 'DELETE',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ confirm: true }),
                });
                const data = await resp.json();
                if (!resp.ok) throw new Error(data.error || `Delete failed (${resp.status})`);
                if (data.unlinked_item) {
                    alert(`"${data.unlinked_item.institution_name || 'Bank'}" was unlinked — this was its last account.`);
                }
                window.location.reload();
            } catch (err) {
                console.error('Delete failed:', err);
                setError(errorEl, err.message);
                confirmBtn.disabled = false;
                confirmBtn.textContent = 'Delete account';
            }
        });
    }

    function openDeleteModal(account) {
        activeAccount = account;
        document.getElementById('delete-account-title').textContent = `Delete "${account.name}"?`;
        const countsEl = document.getElementById('delete-account-counts');
        let html =
            `This will delete <strong>${account.txnCount}</strong> transaction(s), ` +
            `<strong>${account.snapshotCount}</strong> balance snapshot(s), and ` +
            `<strong>${account.importCount}</strong> import record(s).`;
        if (account.plaidLinked) {
            html += ' This account is linked to a bank via Plaid — if it is the last account on that bank, the bank will be unlinked too.';
        }
        countsEl.innerHTML = html;
        setError(document.getElementById('delete-account-error'), '');
        const confirmBtn = document.getElementById('delete-confirm-btn');
        confirmBtn.disabled = false;
        confirmBtn.textContent = 'Delete account';
        openModal('delete-account-modal');
    }
})();
