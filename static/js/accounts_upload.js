// CSV upload UI for the Accounts page (Wave 2a).
// Plain JS, no build step: per-account drag-and-drop upload, an "Add
// account from CSV" modal (preview -> mapping -> create+import), and an
// expandable recent-imports list per account.

document.addEventListener('DOMContentLoaded', () => {
    initExistingAccountUploads();
    initAddAccountModal();
});

// --- Upload to an existing account ---

function initExistingAccountUploads() {
    document.querySelectorAll('.upload-dropzone[data-account-id]').forEach((zone) => {
        const accountId = zone.dataset.accountId;
        const input = zone.querySelector('.upload-file-input');

        input.addEventListener('change', () => {
            if (input.files && input.files[0]) {
                uploadToAccount(accountId, input.files[0]);
                input.value = '';
            }
        });

        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            zone.classList.add('upload-dragover');
        });
        zone.addEventListener('dragleave', () => zone.classList.remove('upload-dragover'));
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('upload-dragover');
            const file = e.dataTransfer.files && e.dataTransfer.files[0];
            if (file) uploadToAccount(accountId, file);
        });
    });

    document.querySelectorAll('.upload-imports-toggle').forEach((btn) => {
        btn.addEventListener('click', () => toggleImportsList(btn.dataset.accountId));
    });
}

async function uploadToAccount(accountId, file) {
    const resultEl = document.querySelector(`.upload-result[data-account-id="${accountId}"]`);
    setResultLoading(resultEl, `Uploading ${file.name}...`);

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch(`/api/accounts/${accountId}/upload`, {
            method: 'POST',
            body: formData,
        });
        const data = await resp.json();

        if (!resp.ok) {
            setResultError(resultEl, data.error || 'Upload failed', data.errors);
            return;
        }

        setResultOk(resultEl, `Imported ${data.imported}, ${data.duplicates} duplicates skipped.`, data.errors);

        const listEl = document.querySelector(`.upload-imports-list[data-account-id="${accountId}"]`);
        if (listEl && !listEl.hidden) {
            loadImportsList(accountId);
        }
    } catch (err) {
        console.error('Upload failed:', err);
        setResultError(resultEl, 'Upload failed: ' + err.message);
    }
}

async function toggleImportsList(accountId) {
    const listEl = document.querySelector(`.upload-imports-list[data-account-id="${accountId}"]`);
    if (!listEl) return;
    if (!listEl.hidden) {
        listEl.hidden = true;
        return;
    }
    listEl.hidden = false;
    await loadImportsList(accountId);
}

async function loadImportsList(accountId) {
    const listEl = document.querySelector(`.upload-imports-list[data-account-id="${accountId}"]`);
    if (!listEl) return;
    listEl.textContent = 'Loading...';
    try {
        const imports = await fetch(`/api/accounts/${accountId}/imports`).then((r) => r.json());
        if (!imports.length) {
            listEl.textContent = 'No imports yet.';
            return;
        }
        const rows = imports.map((imp) => `
            <tr>
                <td>${escapeHtml(imp.imported_at || '')}</td>
                <td>${escapeHtml(imp.filename || '')}</td>
                <td>${imp.row_count}</td>
                <td>${imp.duplicate_count}</td>
            </tr>
        `).join('');
        listEl.innerHTML = `
            <table class="data-table">
                <thead><tr><th>When</th><th>File</th><th>Imported</th><th>Dupes</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        `;
    } catch (err) {
        console.error('Failed to load imports:', err);
        listEl.textContent = 'Failed to load imports.';
    }
}

function setResultLoading(el, msg) {
    if (!el) return;
    el.className = 'upload-result';
    el.textContent = msg;
}

function setResultOk(el, msg, errors) {
    if (!el) return;
    el.className = 'upload-result ok';
    el.innerHTML = escapeHtml(msg);
    appendErrorList(el, errors);
}

function setResultError(el, msg, errors) {
    if (!el) return;
    el.className = 'upload-result error';
    el.innerHTML = escapeHtml(msg);
    appendErrorList(el, errors);
}

function appendErrorList(el, errors) {
    if (!errors || !errors.length) return;
    const ul = document.createElement('ul');
    errors.forEach((e) => {
        const li = document.createElement('li');
        li.textContent = e;
        ul.appendChild(li);
    });
    el.appendChild(ul);
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

// --- Add account from CSV modal ---

let addAccountSelectedFile = null;
let addAccountPreview = null;

function initAddAccountModal() {
    const openBtn = document.getElementById('add-account-btn');
    const modal = document.getElementById('add-account-modal');
    const closeBtn = document.getElementById('add-account-modal-close');
    const backdrop = document.getElementById('add-account-modal-backdrop');
    const backBtn = document.getElementById('add-account-back-btn');
    const dropzone = document.getElementById('add-account-dropzone');
    const fileInput = document.getElementById('add-account-file-input');
    const form = document.getElementById('add-account-form');

    if (!openBtn || !modal) return;

    openBtn.addEventListener('click', () => openAddAccountModal());
    closeBtn.addEventListener('click', () => closeAddAccountModal());
    backdrop.addEventListener('click', () => closeAddAccountModal());
    backBtn.addEventListener('click', () => showAddAccountStep('file'));

    fileInput.addEventListener('change', () => {
        if (fileInput.files && fileInput.files[0]) {
            handleAddAccountFile(fileInput.files[0]);
            fileInput.value = '';
        }
    });

    dropzone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropzone.classList.add('upload-dragover');
    });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('upload-dragover'));
    dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropzone.classList.remove('upload-dragover');
        const file = e.dataTransfer.files && e.dataTransfer.files[0];
        if (file) handleAddAccountFile(file);
    });

    form.addEventListener('submit', (e) => {
        e.preventDefault();
        submitAddAccountForm();
    });
}

function openAddAccountModal() {
    addAccountSelectedFile = null;
    addAccountPreview = null;
    document.getElementById('add-account-form').reset();
    document.getElementById('add-account-preview-error').textContent = '';
    document.getElementById('add-account-preview-error').className = 'upload-result';
    document.getElementById('add-account-submit-error').textContent = '';
    document.getElementById('add-account-submit-error').className = 'upload-result';
    showAddAccountStep('file');
    document.getElementById('add-account-modal').hidden = false;
}

function closeAddAccountModal() {
    document.getElementById('add-account-modal').hidden = true;
}

function showAddAccountStep(step) {
    document.getElementById('add-account-step-file').hidden = step !== 'file';
    document.getElementById('add-account-step-mapping').hidden = step !== 'mapping';
}

async function handleAddAccountFile(file) {
    addAccountSelectedFile = file;
    const errorEl = document.getElementById('add-account-preview-error');
    setResultLoading(errorEl, `Reading ${file.name}...`);

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch('/api/accounts/preview', { method: 'POST', body: formData });
        const data = await resp.json();
        if (!resp.ok) {
            setResultError(errorEl, data.error || 'Could not read file');
            return;
        }
        addAccountPreview = data;
        errorEl.textContent = '';
        errorEl.className = 'upload-result';
        populateAddAccountMapping(data, file.name);
        showAddAccountStep('mapping');
    } catch (err) {
        console.error('Preview failed:', err);
        setResultError(errorEl, 'Could not read file: ' + err.message);
    }
}

function populateAddAccountMapping(preview, filename) {
    const columns = preview.columns || [];
    const detected = preview.detected_mapping || {};

    fillColumnSelect('add-account-date-col', columns, detected.date, false);
    fillColumnSelect('add-account-description-col', columns, detected.description, false);
    fillColumnSelect('add-account-amount-col', columns, detected.amount, true);
    fillColumnSelect('add-account-debit-col', columns, detected.debit, true);
    fillColumnSelect('add-account-credit-col', columns, detected.credit, true);
    fillColumnSelect('add-account-balance-col', columns, detected.balance, true);

    const nameInput = document.getElementById('add-account-name');
    if (!nameInput.value) {
        nameInput.value = filename.replace(/\.csv$/i, '');
    }

    const dateFormatInput = document.getElementById('add-account-date-format');
    dateFormatInput.value = preview.detected_date_format || '';

    // Preview table: header + up to 5 sample rows
    const table = document.getElementById('add-account-preview-table');
    const rows = preview.rows || [];
    const headerHtml = `<thead><tr>${columns.map((c) => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>`;
    const bodyHtml = `<tbody>${rows.map((row) => `
        <tr>${columns.map((c) => `<td>${escapeHtml(row[c])}</td>`).join('')}</tr>
    `).join('')}</tbody>`;
    table.innerHTML = headerHtml + bodyHtml;
}

function fillColumnSelect(selectId, columns, selected, optional) {
    const select = document.getElementById(selectId);
    const placeholder = select.querySelector('option[value=""]');
    select.innerHTML = '';
    if (optional) {
        select.appendChild(placeholder || new Option(select.dataset.placeholder || '(none)', ''));
    }
    columns.forEach((col) => {
        const opt = new Option(col, col);
        if (col === selected) opt.selected = true;
        select.appendChild(opt);
    });
}

async function submitAddAccountForm() {
    const submitBtn = document.getElementById('add-account-submit-btn');
    const errorEl = document.getElementById('add-account-submit-error');
    errorEl.textContent = '';
    errorEl.className = 'upload-result';

    if (!addAccountSelectedFile) {
        setResultError(errorEl, 'No file selected');
        return;
    }

    const form = document.getElementById('add-account-form');
    const formData = new FormData(form);
    formData.append('file', addAccountSelectedFile);

    submitBtn.disabled = true;
    submitBtn.textContent = 'Importing...';

    try {
        const resp = await fetch('/api/accounts', { method: 'POST', body: formData });
        const data = await resp.json();

        if (!resp.ok) {
            setResultError(errorEl, data.error || 'Could not create account', data.errors);
            return;
        }

        setResultOk(errorEl, `Created "${data.name}": imported ${data.imported}, ${data.duplicates} duplicates skipped.`);
        // Reload so the new account + its balance show up in the table/grid.
        setTimeout(() => window.location.reload(), 800);
    } catch (err) {
        console.error('Create account failed:', err);
        setResultError(errorEl, 'Could not create account: ' + err.message);
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Create account & import';
    }
}
