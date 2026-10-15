/* AIWEB → Python API → DSH. No model secrets or chat history are stored in browser storage. */
async function workspaceRequest(path, options = {}) {
    const response = await fetch(path, {
        ...options,
        headers: options.body instanceof FormData
            ? options.headers
            : { 'Content-Type': 'application/json', ...options.headers }
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
        const detail = result.detail || result.error || `请求失败（${response.status}）`;
        const error = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
        error.status = response.status;
        throw error;
    }
    return result;
}

// A small DOM-only Markdown renderer. Source HTML and links stay inert text.
function renderAssistantMarkdown(target, value) {
    target.replaceChildren();
    target.style.whiteSpace = 'normal';
    const lines = String(value || '').replace(/\r\n?/g, '\n').split('\n');
    function inline(parent, text, allowBold = true) {
        const pattern = allowBold ? /`([^`\n]+)`|\*\*([^*\n]+)\*\*/g : /`([^`\n]+)`/g;
        let offset = 0;
        for (const match of text.matchAll(pattern)) {
            parent.append(document.createTextNode(text.slice(offset, match.index)));
            const element = document.createElement(match[1] !== undefined ? 'code' : 'strong');
            if (match[1] !== undefined) {
                element.textContent = match[1];
                Object.assign(element.style, { fontFamily: 'Consolas, monospace', fontSize: '.92em', padding: '1px 5px', borderRadius: '4px', background: '#ffffff10' });
            } else inline(element, match[2], false);
            parent.append(element);
            offset = match.index + match[0].length;
        }
        parent.append(document.createTextNode(text.slice(offset)));
    }
    const fence = line => line.match(/^\s*(`{3,}|~{3,})([\w#+.-]*)\s*$/);
    const heading = line => line.match(/^\s{0,3}(#{1,6})\s+(.+)$/);
    const listItem = line => line.match(/^\s*(?:([-+*])|\d+[.)])\s+(.+)$/);
    let index = 0;
    while (index < lines.length) {
        if (!lines[index].trim()) { index += 1; continue; }
        const openFence = fence(lines[index]);
        if (openFence) {
            const codeLines = [];
            index += 1;
            while (index < lines.length) {
                const close = lines[index].trim();
                if (close.length >= openFence[1].length && [...close].every(char => char === openFence[1][0])) { index += 1; break; }
                codeLines.push(lines[index++]);
            }
            const pre = document.createElement('pre');
            Object.assign(pre.style, { padding: '12px', margin: '10px 0', borderRadius: '8px', background: '#080910', overflowX: 'auto', maxWidth: '100%', whiteSpace: 'pre', fontSize: '12px', lineHeight: '1.7' });
            const code = document.createElement('code');
            code.textContent = codeLines.join('\n');
            if (openFence[2]) code.dataset.language = openFence[2];
            pre.append(code);
            target.append(pre);
            continue;
        }
        const title = heading(lines[index]);
        if (title) {
            const element = document.createElement(`h${Math.max(2, title[1].length)}`);
            Object.assign(element.style, { fontSize: title[1].length <= 2 ? '1.1em' : '1em', margin: '14px 0 8px', lineHeight: '1.65', fontWeight: '600' });
            inline(element, title[2]);
            target.append(element);
            index += 1;
            continue;
        }
        const firstItem = listItem(lines[index]);
        if (firstItem) {
            const unordered = !!firstItem[1];
            const list = document.createElement(unordered ? 'ul' : 'ol');
            Object.assign(list.style, { margin: '8px 0', paddingLeft: '22px' });
            while (index < lines.length) {
                const item = listItem(lines[index]);
                if (!item || !!item[1] !== unordered) break;
                const li = document.createElement('li');
                li.style.margin = '4px 0';
                inline(li, item[2]);
                list.append(li);
                index += 1;
            }
            target.append(list);
            continue;
        }
        const paragraphLines = [lines[index++]];
        while (index < lines.length && lines[index].trim() && !fence(lines[index]) && !heading(lines[index]) && !listItem(lines[index])) paragraphLines.push(lines[index++]);
        const paragraph = document.createElement('p');
        Object.assign(paragraph.style, { margin: '8px 0', whiteSpace: 'pre-wrap' });
        inline(paragraph, paragraphLines.join('\n'));
        target.append(paragraph);
    }
    if (target.firstElementChild) target.firstElementChild.style.marginTop = '0';
    if (target.lastElementChild) target.lastElementChild.style.marginBottom = '0';
}

function initWorkspace() {
    const $ = id => document.getElementById(id);
    const state = {
        project: null, projects: [], builds: [], selectedBuild: null,
        pendingFiles: [], busy: false, task: null, source: null, lastSeq: 0,
        settings: null, activeStage: null, stageGroup: null, hiddenStages: 0, directory: '', file: '',
        checkingTask: false, connectionFailures: 0, replayAssistant: []
    };
    const messagesEl = $('chatMessages');
    const inputEl = $('chatInput');
    const dialog = $('settingsDialog');
    let toastTimer;

    function toast(text) {
        $('workspaceToast').textContent = text;
        $('workspaceToast').hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { $('workspaceToast').hidden = true; }, 6500);
    }
    function report(error) { toast(error.message || String(error)); }
    function scroll() { requestAnimationFrame(() => { messagesEl.scrollTop = messagesEl.scrollHeight; }); }
    function setChatLayout(hasMessages) {
        messagesEl.parentElement.classList.toggle('has-messages', hasMessages);
        $('chatSection').classList.toggle('has-messages', hasMessages);
        const empty = $('chatEmpty');
        if (empty) empty.hidden = hasMessages;
    }
    function appendMessage(role, content, attachments = []) {
        setChatLayout(true);
        const wrapper = document.createElement('div');
        wrapper.className = `message ${role === 'user' ? 'message-user' : 'message-ai'}`;
        if (role === 'error') wrapper.classList.add('message-error');
        const bubble = document.createElement('div');
        bubble.className = 'message-bubble';
        const text = document.createElement('div');
        text.className = 'message-content';
        if (role === 'assistant' || role === 'ai') renderAssistantMarkdown(text, content);
        else text.textContent = content || '';
        bubble.append(text);
        if (attachments.length) {
            const list = document.createElement('div');
            list.className = 'attach-list';
            attachments.forEach(file => {
                const item = document.createElement('div');
                item.className = 'attach-item attach-file';
                item.textContent = `📎 ${typeof file === 'string' ? file : (file.name || file.filename || '附件')}`;
                list.append(item);
            });
            bubble.append(list);
        }
        if (role === 'user') {
            const avatar = document.createElement('div');
            avatar.className = 'message-avatar';
            avatar.innerHTML = '<i class="fas fa-user"></i>';
            wrapper.append(avatar);
        }
        wrapper.append(bubble);
        messagesEl.append(wrapper);
        scroll();
        return wrapper;
    }
    function setBusy(busy) {
        state.busy = busy;
        $('sendBtn').classList.toggle('is-running', busy);
        $('sendBtn').setAttribute('aria-label', busy ? '停止当前任务' : '发送');
        $('sendBtn').title = busy ? '停止当前任务' : '发送';
        $('sendBtn').innerHTML = busy
            ? '<span class="stop-square"></span>'
            : '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3.4 20.6L21 12L3.4 3.4L3 10L17 12L3 14L3.4 20.6Z" fill="currentColor"/></svg>';
        $('addBtn').disabled = busy;
        inputEl.disabled = busy;
    }
    function closeStream() {
        if (state.source) state.source.close();
        state.source = null;
    }
    function clearTaskDisplay() {
        state.activeStage = null;
        state.stageGroup = null;
        state.hiddenStages = 0;
    }
    function taskStage(stage, label, detail) {
        setChatLayout(true);
        if (!state.stageGroup) {
            state.stageGroup = document.createElement('div');
            state.stageGroup.className = 'stage-group live-task-stages';
            const summary = document.createElement('p');
            summary.className = 'file-empty stage-history-summary';
            summary.hidden = true;
            state.stageGroup.append(summary);
            messagesEl.append(state.stageGroup);
        }
        const failed = ['failed', 'blocked', 'cancelled', 'error'].includes(stage) || stage.startsWith('tool-failed-');
        const terminal = failed || stage === 'completed';
        if (!state.activeStage || state.activeStage.dataset.stage !== stage) {
            if (state.activeStage && !state.activeStage.classList.contains('stage-error')) {
                state.activeStage.classList.add('stage-done');
            }
            const col = document.createElement('div');
            col.className = `stage-col stage-starting ${terminal ? (failed ? 'stage-error' : 'stage-done') : ''}`;
            col.dataset.stage = stage;
            col.innerHTML = '<div class="ai-status-header"><div class="stg-icon"><div class="stg-orbit"><div class="stg-orbit-nucleus"></div><div class="stg-orbit-electron stg-orbit-electron--1"></div><div class="stg-orbit-electron stg-orbit-electron--2"></div><div class="stg-orbit-electron stg-orbit-electron--3"></div></div><span class="stg-icon-done">✓</span></div><span class="ai-status-label"></span></div><ul class="ai-status-info live-status-info"></ul>';
            state.stageGroup.append(col);
            state.activeStage = col;
            // The backend retains every event; keep long tasks small in the chat DOM.
            const ordinaryStages = Array.from(state.stageGroup.children).filter(child =>
                child.classList.contains('stage-col') && !child.classList.contains('stage-error'));
            while (ordinaryStages.length > 10) {
                ordinaryStages.shift().remove();
                state.hiddenStages += 1;
            }
            if (state.hiddenStages) {
                const summary = state.stageGroup.querySelector('.stage-history-summary');
                summary.hidden = false;
                summary.textContent = `已收起 ${state.hiddenStages} 条较早状态，显示最近 10 条普通状态；错误会保留。`;
            }
        }
        state.activeStage.querySelector('.ai-status-label').textContent = label || stage;
        if (detail) {
            const list = state.activeStage.querySelector('ul');
            if (!list.lastElementChild || list.lastElementChild.textContent !== detail) {
                const li = document.createElement('li');
                li.textContent = detail;
                list.append(li);
                // A repeated stage can also have many tool output fragments.
                const details = Array.from(list.children).filter(child => !child.dataset.summary);
                let hiddenDetails = Number(list.dataset.hiddenDetails || 0);
                while (details.length > 5) {
                    details.shift().remove();
                    hiddenDetails += 1;
                }
                if (hiddenDetails) {
                    list.dataset.hiddenDetails = String(hiddenDetails);
                    let summary = list.querySelector('[data-summary]');
                    if (!summary) {
                        summary = document.createElement('li');
                        summary.dataset.summary = 'true';
                        list.prepend(summary);
                    }
                    summary.textContent = `已收起 ${hiddenDetails} 条较早详情`;
                }
            }
        }
        scroll();
    }

    function refreshConnection() {
        const configured = state.settings && state.settings.has_api_key;
        $('connectionStatus').textContent = configured ? `DSH · ${state.settings.model}` : '请先配置 DeepSeek API Key';
        $('connectionStatus').className = `connection-status ${configured ? 'connected' : 'needs-setup'}`;
        $('openSettingsBtn').textContent = configured ? '模型与环境设置' : '配置 DeepSeek';
    }
    async function refreshEnvironment(force = false) {
        $('environmentStatus').textContent = '正在检测…';
        try {
            const env = await workspaceRequest(`/api/environment${force ? '?refresh=true' : ''}`);
            const rows = [
                ['DSH 运行时', !!env.dsh?.runtime_ready, env.dsh?.detail || (env.dsh?.version ? `版本 ${env.dsh.version}` : '未检测到运行时')],
                ['Unity Editor', !!env.unity?.installed, env.unity?.detail || (env.unity?.installed ? '已安装' : '需要安装 Unity Editor')],
                ['Unity Editor 连接', !!env.unity?.connected, env.unity?.connected ? '已连接制作环境' : '尚未连接，请启动 Unity 工程并连接 CLI'],
                ['Unity 许可证', env.unity?.licensed === true, env.unity?.licensed === true ? '已激活' : env.unity?.licensed === false ? '尚未激活，需要先在 Unity Hub 中激活许可证' : '尚未确认许可证状态'],
                ['Unity 网页构建模块', !!env.unity?.web_module, env.unity?.web_module ? '已安装' : '需要 Web 构建模块才能在网页试玩'],
                ['Unity 技能库', !!env.skills?.installed, `${env.skills?.count || 0} 个技能${env.skills?.names?.length ? `：${env.skills.names.join('、')}` : ''}`]
            ];
            $('environmentStatus').replaceChildren();
            rows.forEach(([name, ready, detail]) => {
                const row = document.createElement('div');
                row.className = `environment-row ${ready ? 'environment-ready' : 'environment-pending'}`;
                const title = document.createElement('strong');
                title.textContent = `${ready ? '✓' : '○'} ${name}`;
                const description = document.createElement('span');
                description.textContent = detail;
                row.append(title, description);
                $('environmentStatus').append(row);
            });
            (env.warnings || []).forEach(warning => {
                const p = document.createElement('p');
                p.className = 'environment-warning';
                p.textContent = warning;
                $('environmentStatus').append(p);
            });
        } catch (error) { $('environmentStatus').textContent = `检测失败：${error.message}`; }
    }
    async function openSettings() {
        if (!dialog.open) dialog.showModal();
        $('settingsFeedback').textContent = '';
        $('apiKeyInput').value = '';
        try {
            state.settings = await workspaceRequest('/api/settings');
            $('modelInput').value = state.settings.model || 'deepseek-v4-flash';
            $('baseUrlInput').value = state.settings.base_url || 'https://api.deepseek.com';
            $('apiKeyState').textContent = state.settings.has_api_key ? '已设置' : '未设置';
            $('apiKeyInput').placeholder = state.settings.has_api_key ? '留空保留已有密钥' : '填写 DeepSeek API Key';
            refreshConnection();
        } catch (error) { $('settingsFeedback').textContent = error.message; }
        refreshEnvironment();
    }
    $('openSettingsBtn').addEventListener('click', openSettings);
    $('closeSettingsBtn').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => { $('apiKeyInput').value = ''; });
    dialog.addEventListener('click', event => { if (event.target === dialog) { const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close(); } });
    $('refreshEnvironmentBtn').addEventListener('click', () => refreshEnvironment(true));
    $('settingsForm').addEventListener('submit', async event => {
        event.preventDefault();
        if (state.busy) { $('settingsFeedback').textContent = '请先停止或等待当前任务结束，再修改模型配置。'; return; }
        const body = { model: $('modelInput').value.trim(), base_url: $('baseUrlInput').value.trim() };
        const key = $('apiKeyInput').value.trim();
        if (key) body.api_key = key;
        $('saveSettingsBtn').disabled = true;
        $('settingsFeedback').textContent = '正在保存…';
        try {
            state.settings = await workspaceRequest('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
            $('apiKeyInput').value = '';
            $('apiKeyState').textContent = state.settings.has_api_key ? '已设置' : '未设置';
            $('settingsFeedback').textContent = '已保存，可关闭设置开始对话。';
            refreshConnection();
            refreshEnvironment();
        } catch (error) { $('settingsFeedback').textContent = error.message; }
        finally { $('saveSettingsBtn').disabled = false; }
    });

    function renderPendingFiles() {
        let list = $('attachHint');
        if (!list) {
            list = document.createElement('div');
            list.id = 'attachHint';
            list.className = 'pending-attachments';
            document.querySelector('.chat-input-area').append(list);
        }
        list.replaceChildren();
        state.pendingFiles.forEach((file, index) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = `📎 ${file.name} ×`;
            button.title = '移除附件';
            button.addEventListener('click', () => { state.pendingFiles.splice(index, 1); renderPendingFiles(); });
            list.append(button);
        });
        list.hidden = !state.pendingFiles.length;
    }
    $('addBtn').addEventListener('click', () => { if (!state.busy) $('fileInput').click(); });
    $('fileInput').addEventListener('change', () => {
        state.pendingFiles.push(...Array.from($('fileInput').files || []));
        $('fileInput').value = '';
        renderPendingFiles();
    });
    inputEl.addEventListener('input', () => {
        inputEl.style.height = 'auto';
        inputEl.style.height = `${inputEl.scrollHeight + inputEl.offsetHeight - inputEl.clientHeight}px`;
    });
    inputEl.addEventListener('keydown', event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !state.busy) {
            event.preventDefault();
            sendMessage();
        }
    });
    $('sendBtn').addEventListener('click', () => { if (state.busy) cancelTask(); else sendMessage(); });

    async function sendMessage() {
        const text = inputEl.value.trim();
        if (state.busy || (!text && !state.pendingFiles.length)) return;
        const files = [...state.pendingFiles];
        setBusy(true);
        try {
            state.settings = await workspaceRequest('/api/settings');
            refreshConnection();
            if (!state.settings.has_api_key) { setBusy(false); await openSettings(); toast('先在设置中保存 DeepSeek API Key。'); return; }
            if (!state.project) {
                const project = await workspaceRequest('/api/projects', { method: 'POST', body: JSON.stringify({ title: text.slice(0, 48) || files[0]?.name || '新游戏' }) });
                state.project = project.project || project;
                updateProjectAddress();
            }
            const form = new FormData();
            form.append('text', text);
            files.forEach(file => form.append('files', file));
            const task = await workspaceRequest(`/api/projects/${encodeURIComponent(state.project.id)}/messages`, { method: 'POST', body: form });
            appendMessage('user', text, files);
            inputEl.value = '';
            inputEl.style.height = 'auto';
            state.pendingFiles = [];
            renderPendingFiles();
            clearTaskDisplay();
            state.replayAssistant = [];
            taskStage('queued', '等待制作');
            watchTask(task.task_id);
            refreshProjects().catch(() => {});
        } catch (error) {
            setBusy(false);
            appendMessage('error', `请求未成功：${error.message}`);
            if (error.status === 409 && /key|密钥|配置/i.test(error.message)) openSettings();
        }
    }
    async function cancelTask() {
        if (!state.task) return;
        try {
            await workspaceRequest(`/api/tasks/${encodeURIComponent(state.task)}/cancel`, { method: 'POST' });
            taskStage('cancelling', '正在停止', '等待当前操作退出。');
        } catch (error) { report(error); }
    }
    function watchTask(id) {
        closeStream();
        state.task = id;
        state.lastSeq = 0;
        state.connectionFailures = 0;
        setBusy(true);
        const source = new EventSource(`/api/tasks/${encodeURIComponent(id)}/events?after=0`);
        state.source = source;
        source.onmessage = event => {
            if (state.source !== source) return;
            let item;
            try { item = JSON.parse(event.data); } catch { return; }
            if (item.seq && item.seq <= state.lastSeq) return;
            state.lastSeq = Math.max(state.lastSeq, item.seq || 0);
            state.connectionFailures = 0;
            const data = item.data || {};
            if (item.type === 'status') taskStage(data.stage || 'running', data.label || '执行中', data.detail);
            if (item.type === 'tool') {
                const failed = data.phase === 'failed';
                const label = failed ? '工具失败' : ['end', 'completed'].includes(data.phase) ? '已执行' : '正在执行';
                taskStage(`tool-${failed ? 'failed-' : ''}${data.name}`, `${label} · ${data.name || '工具'}`, data.summary);
            }
            if (item.type === 'assistant' && data.text) {
                const replay = state.replayAssistant.indexOf(data.text);
                if (replay >= 0) state.replayAssistant.splice(replay, 1);
                else appendMessage('assistant', data.text);
            }
            if (item.type === 'error') {
                taskStage('error', '执行遇到问题', data.message);
                appendMessage('error', data.message || '任务执行失败，请查看环境设置。');
            }
            if (item.type === 'completed') finishTask(data, id);
        };
        source.onerror = async () => {
            if (state.source !== source || state.checkingTask) return;
            state.connectionFailures += 1;
            taskStage('reconnecting', '连接暂时中断', '正在重新连接，任务可能仍在后台运行。');
            state.checkingTask = true;
            try {
                const result = await workspaceRequest(`/api/tasks/${encodeURIComponent(id)}`);
                const task = result.task || result;
                if (['completed', 'failed', 'blocked', 'cancelled'].includes(task.status)) {
                    await finishTask({ status: task.status, message: task.error || task.message, build: task.build }, id);
                } else if (state.connectionFailures >= 6) {
                    closeStream();
                    setBusy(false);
                    taskStage('blocked', '执行连接已断开', '后台任务可能仍在运行。请从历史记录重新打开作品以恢复连接。');
                }
            } catch (error) {
                if (state.connectionFailures >= 3) {
                    closeStream();
                    setBusy(false);
                    taskStage('blocked', '无法连接后台', '请确认后端服务仍在运行，再从历史记录重新打开作品。');
                }
            } finally { state.checkingTask = false; }
        };
    }
    async function finishTask(data, id) {
        if (state.task !== id || !state.busy) return;
        closeStream();
        const status = data.status || 'completed';
        const labels = { completed: '本轮对话结束', failed: '本次任务失败', blocked: '需要处理环境问题', cancelled: '任务已停止' };
        taskStage(status, labels[status] || '本次任务结束', data.message);
        setBusy(false);
        if (data.build) setBuild(data.build);
        try {
            if (state.project) {
                const result = await workspaceRequest(`/api/projects/${encodeURIComponent(state.project.id)}`);
                state.builds = result.builds || [];
                renderBuilds();
                if (!data.build && (result.project?.latest_build || state.builds[0])) setBuild(result.project?.latest_build || state.builds[0]);
                await loadFiles(state.directory);
            }
            await refreshProjects();
        } catch (error) { report(error); }
        inputEl.focus();
    }

    function updateProjectAddress() {
        const url = new URL(location.href);
        if (state.project) url.searchParams.set('project', state.project.id);
        else url.searchParams.delete('project');
        history.replaceState({}, '', url);
        document.title = state.project ? `${state.project.title} — FOFU AI` : 'FOFU AI - AI创作平台';
    }
    function resetProject() {
        if (state.busy) { toast('当前任务仍在执行，请先停止或等待完成。'); return; }
        closeStream();
        state.project = null;
        state.task = null;
        state.builds = [];
        state.directory = '';
        state.file = '';
        clearTaskDisplay();
        messagesEl.innerHTML = '<div class="chat-empty" id="chatEmpty"><div class="chat-empty-brand"><img src="image/logo.png" alt="FOFU" class="chat-empty-logo-img"><span class="chat-empty-name">FOFU</span></div></div>';
        setChatLayout(false);
        inputEl.value = '';
        inputEl.style.height = 'auto';
        state.pendingFiles = [];
        renderPendingFiles();
        setBuild(null);
        renderBuilds();
        $('projectFiles').textContent = '创建作品后查看文件';
        $('codeContent').textContent = '这里展示智能体实际制作的 Unity 工程文件。请从左侧选择文件。';
        document.querySelector('.code-filename').textContent = '请选择工程文件';
        updateProjectAddress();
        inputEl.focus();
    }
    async function loadProject(id) {
        if (state.busy) { toast('当前任务仍在执行，请先停止或等待完成。'); return; }
        const result = await workspaceRequest(`/api/projects/${encodeURIComponent(id)}`);
        closeStream();
        state.project = result.project;
        state.builds = result.builds || [];
        state.directory = '';
        state.file = '';
        clearTaskDisplay();
        messagesEl.replaceChildren();
        setChatLayout(true);
        (result.messages || []).forEach(message => appendMessage(message.role, message.content, message.attachments || []));
        if (!(result.messages || []).length) {
            const hint = document.createElement('p'); hint.className = 'file-empty'; hint.textContent = '描述你想制作的游戏，开始这个作品。'; messagesEl.append(hint);
        }
        renderBuilds();
        setBuild(result.project.latest_build || state.builds[0] || null);
        await loadFiles();
        const tasks = result.tasks || [];
        const running = tasks.find(task => ['queued', 'running', 'cancelling'].includes(task.status));
        if (running) {
            const messages = result.messages || [];
            const lastUser = messages.map(message => message.role).lastIndexOf('user');
            state.replayAssistant = messages.slice(lastUser + 1).filter(message => ['assistant', 'ai'].includes(message.role)).map(message => message.content);
            watchTask(running.id);
        }
        else if (tasks.length) {
            const latest = tasks[0];
            if (['failed', 'blocked', 'cancelled'].includes(latest.status)) taskStage(latest.status, latest.status === 'cancelled' ? '上次任务已停止' : '上次任务未完成', latest.error || latest.message);
        }
        updateProjectAddress();
        $('historyModal').classList.remove('active');
        scroll();
    }
    function setBuild(build) {
        state.selectedBuild = build || null;
        let preview = null;
        if (build?.preview_url) {
            try {
                const url = new URL(build.preview_url, location.href);
                if (['http:', 'https:'].includes(url.protocol) && url.origin === location.origin) preview = url.href;
            } catch { /* Invalid preview is never mounted. */ }
        }
        document.querySelectorAll('.game-iframe, .pc-iframe').forEach(frame => {
            frame.setAttribute('sandbox', 'allow-scripts allow-pointer-lock allow-forms allow-downloads');
            frame.hidden = !preview;
            if (frame.src !== (preview || 'about:blank')) frame.src = preview || 'about:blank';
            const empty = frame.parentElement.querySelector('.preview-empty');
            if (empty) empty.hidden = !!preview;
        });
        $('previewUrlLabel').textContent = preview ? new URL(preview).pathname : '等待游戏构建';
        if (build?.id) $('buildSelect').value = build.id;
    }
    function renderBuilds() {
        const select = $('buildSelect');
        select.replaceChildren();
        state.builds.forEach((build, index) => {
            const option = document.createElement('option');
            option.value = build.id;
            option.textContent = `${index === 0 ? '最新 · ' : ''}${formatTime(build.created_at)}`;
            select.append(option);
        });
        select.hidden = !state.builds.length;
        if (state.selectedBuild?.id) select.value = state.selectedBuild.id;
    }
    $('buildSelect').addEventListener('change', () => { const build = state.builds.find(item => item.id === $('buildSelect').value); if (build) setBuild(build); });
    document.querySelector('.pc-bar-action').addEventListener('click', () => {
        const frame = document.querySelector('.pc-iframe');
        if (frame && !frame.hidden) frame.src = frame.src;
    });
    async function loadFiles(path = '') {
        if (!state.project) return;
        const projectId = state.project.id;
        const result = await workspaceRequest(`/api/projects/${encodeURIComponent(projectId)}/files?path=${encodeURIComponent(path)}`);
        if (state.project?.id !== projectId) return;
        state.directory = result.path || '';
        const list = $('projectFiles');
        list.replaceChildren();
        if (state.directory) {
            const up = document.createElement('button');
            up.type = 'button'; up.className = 'code-tree-item code-tree-folder'; up.textContent = '↑ 上一级';
            up.addEventListener('click', () => loadFiles(state.directory.split('/').slice(0, -1).join('/')).catch(report));
            list.append(up);
        }
        (result.files || []).forEach(file => {
            const folder = ['dir', 'directory', 'folder'].includes(file.type);
            const button = document.createElement('button');
            button.type = 'button';
            button.className = `code-tree-item ${folder ? 'code-tree-folder' : 'code-tree-file'}${file.path === state.file ? ' active' : ''}`;
            button.title = file.path;
            const icon = document.createElement('i'); icon.className = `fas ${folder ? 'fa-folder' : 'fa-file-code'}`;
            const label = document.createElement('span'); label.textContent = file.name;
            button.append(icon, label);
            button.addEventListener('click', () => (folder ? loadFiles(file.path) : readFile(file.path)).catch(report));
            list.append(button);
        });
        if (!(result.files || []).length) {
            const empty = document.createElement('div'); empty.className = 'file-empty'; empty.textContent = '暂无文件'; list.append(empty);
        }
    }
    async function readFile(path) {
        const projectId = state.project?.id;
        if (!projectId) return;
        const result = await workspaceRequest(`/api/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}`);
        if (state.project?.id !== projectId) return;
        state.file = result.path || path;
        $('codeContent').textContent = result.content;
        document.querySelector('.code-filename').textContent = state.file.split('/').pop();
        document.querySelector('.code-title').textContent = `${state.file} — FOFU Studio`;
        document.querySelectorAll('#projectFiles .code-tree-item').forEach(button => button.classList.toggle('active', button.title === state.file));
    }
    document.querySelector('[data-view="code"].preview-panel-tab').addEventListener('click', () => loadFiles(state.directory).catch(report));

    function formatTime(value) {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
    }
    async function refreshProjects() {
        const result = await workspaceRequest('/api/projects');
        state.projects = result.projects || [];
        renderProjects();
    }
    function renderProjects() {
        const query = $('historySearch').value.trim().toLowerCase();
        const projects = state.projects.filter(project => (project.title || '').toLowerCase().includes(query));
        $('historyList').replaceChildren();
        $('historyCount').textContent = projects.length;
        $('historyEmpty').style.display = projects.length ? 'none' : 'flex';
        $('historyEmpty').querySelector('p').textContent = query ? '没有找到相关作品' : '暂无作品记录';
        projects.forEach(project => {
            const item = document.createElement('div'); item.className = 'history-item'; item.tabIndex = 0; item.setAttribute('role', 'button');
            const info = document.createElement('div'); info.className = 'history-item-info';
            const title = document.createElement('div'); title.className = 'history-item-title'; title.textContent = project.title || '新游戏';
            const meta = document.createElement('div'); meta.className = 'history-item-meta'; meta.textContent = formatTime(project.updated_at || project.created_at);
            info.append(title, meta);
            const remove = document.createElement('button'); remove.className = 'history-delete'; remove.type = 'button'; remove.title = '删除作品'; remove.setAttribute('aria-label', '删除作品'); remove.innerHTML = '<i class="fas fa-trash"></i>';
            remove.addEventListener('click', async event => {
                event.stopPropagation();
                if (state.busy) { toast('请先停止或等待当前任务完成。'); return; }
                if (!confirm(`移除“${project.title}”的作品与聊天记录？工程文件将保留在本机。`)) return;
                try {
                    await workspaceRequest(`/api/projects/${encodeURIComponent(project.id)}`, { method: 'DELETE' });
                    if (state.project?.id === project.id) resetProject();
                    await refreshProjects();
                } catch (error) { report(error); }
            });
            item.append(info, remove);
            item.addEventListener('click', () => loadProject(project.id).catch(report));
            item.addEventListener('keydown', event => { if (event.target === item && ['Enter', ' '].includes(event.key)) { event.preventDefault(); loadProject(project.id).catch(report); } });
            $('historyList').append(item);
        });
    }
    async function openHistory(focusSearch) {
        $('historyModal').classList.add('active');
        try { await refreshProjects(); } catch (error) {
            $('historyEmpty').style.display = 'flex';
            $('historyEmpty').querySelector('p').textContent = `无法读取作品：${error.message}`;
        }
        try {
            const legacy = JSON.parse(localStorage.getItem('fofu_ai_history_v1') || '[]');
            $('historyNotice').hidden = !legacy.length;
            if (legacy.length) $('historyNotice').textContent = `检测到 ${legacy.length} 条旧版演示记录。它们尚未迁移至后台，不计入当前作品。`;
        } catch { $('historyNotice').hidden = true; }
        if (focusSearch) $('historySearch').focus();
        else $('historyModalClose').focus();
    }
    $('historySearch').addEventListener('input', renderProjects);
    $('historyModalClose').addEventListener('click', () => $('historyModal').classList.remove('active'));
    $('historyModal').addEventListener('click', event => { if (event.target === $('historyModal')) $('historyModal').classList.remove('active'); });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') $('historyModal').classList.remove('active'); });
    $('clearHistoryBtn').addEventListener('click', async () => {
        if (state.busy) { toast('请先停止或等待当前任务完成。'); return; }
        if (!state.projects.length || !confirm('清空全部作品与聊天记录？工程文件将保留在本机，记录无法恢复。')) return;
        try {
            await workspaceRequest('/api/projects', { method: 'DELETE' });
            resetProject(); await refreshProjects();
        } catch (error) { report(error); }
    });
    async function publishProject() {
        if (state.busy) { toast('请先等待当前任务结束，再发布作品。'); return; }
        if (!state.project || !state.selectedBuild) { toast('请先完成一次 Unity 网页构建，再发布作品。'); return; }
        try {
            const result = await workspaceRequest(`/api/projects/${encodeURIComponent(state.project.id)}/publish`, { method: 'POST', body: JSON.stringify({ build_id: state.selectedBuild.id }) });
            const url = new URL(result.url, location.href);
            if (!['http:', 'https:'].includes(url.protocol)) throw new Error('发布地址无效');
            const text = `发布地址：${url.href}`;
            try { await navigator.clipboard.writeText(url.href); toast(`已复制${text}。本机地址只能在相应网络中访问。`); }
            catch { toast(text); }
        } catch (error) { report(error); }
    }
    document.querySelectorAll('.sidebar-icon').forEach(button => {
        button.addEventListener('click', () => {
            const action = button.dataset.action;
            document.querySelectorAll('.sidebar-icon').forEach(item => item.classList.toggle('active', item === button));
            if (action === 'settings') openSettings();
            if (action === 'search' || action === 'history') openHistory(action === 'search');
            if (action === 'new-chat') resetProject();
            if (action === 'publish') publishProject();
            if (action === 'back') { if (state.project) resetProject(); else toast('当前已在创作首页。'); }
        });
    });
    window.addEventListener('beforeunload', closeStream);
    (async () => {
        try {
            await workspaceRequest('/api/health');
            state.settings = await workspaceRequest('/api/settings');
            refreshConnection();
            const id = new URL(location.href).searchParams.get('project');
            if (id) await loadProject(id);
        } catch (error) {
            $('connectionStatus').textContent = '后台未连接';
            $('connectionStatus').className = 'connection-status needs-setup';
            toast(`无法连接后台：${error.message}。请通过 Python 后端提供的地址打开页面。`);
        }
    })();
}
