// 保留原有星空、预览切换和面板拖拽。业务数据来自 Python 后端。
document.addEventListener('DOMContentLoaded', () => {
    initStarfield();
    initPreviewPanel();
    initWorkspace();
});

function initStarfield() {
    const canvas = document.getElementById('starfield-bg');
    if (!canvas) return;
    
    const ctx = canvas.getContext('2d');
    
    function resizeCanvas() {
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
    }
    
    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);
    
    const stars = [];
    const starCount = 200;
    
    // 创建星星
    for (let i = 0; i < starCount; i++) {
        stars.push({
            x: Math.random() * canvas.width,
            y: Math.random() * canvas.height,
            radius: Math.random() * 2,
            vx: (Math.random() - 0.5) * 0.5,
            vy: (Math.random() - 0.5) * 0.5,
            opacity: Math.random() * 0.6 + 0.3
        });
    }
    
    function animate() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        
        stars.forEach(star => {
            ctx.fillStyle = `rgba(255, 255, 255, ${star.opacity})`;
            ctx.beginPath();
            ctx.arc(star.x, star.y, star.radius, 0, Math.PI * 2);
            ctx.fill();
            
            star.x += star.vx;
            star.y += star.vy;
            
            if (star.x < 0 || star.x > canvas.width) star.vx *= -1;
            if (star.y < 0 || star.y > canvas.height) star.vy *= -1;
            
            // 闪烁效果
            star.opacity += (Math.random() - 0.5) * 0.03;
            star.opacity = Math.max(0.2, Math.min(0.9, star.opacity));
        });
        
        requestAnimationFrame(animate);
    }
    
    animate();
}

// 初始化预览面板（右侧 APP/PC/代码 切换 + 收起/展开）
function initPreviewPanel() {
    const panel = document.getElementById('previewPanel');
    if (!panel) return;

    const tabs = panel.querySelectorAll('.preview-panel-tab');
    const views = panel.querySelectorAll('.preview-view');
    const toggle = document.getElementById('previewToggle');

    // 标签切换
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const viewName = tab.dataset.view;
            tabs.forEach(t => {
                const active = t === tab;
                t.classList.toggle('active', active);
                t.setAttribute('aria-selected', active ? 'true' : 'false');
            });
            views.forEach(v => {
                v.classList.toggle('active', v.dataset.view === viewName);
            });
        });
    });

    // 收起 / 展开（与拖拽宽度协调：收起清空行内宽度，展开恢复上次宽度）
    let resizedWidth = null;
    if (toggle) {
        toggle.addEventListener('click', () => {
            const collapsing = !panel.classList.contains('collapsed');
            if (collapsing) {
                panel.classList.add('collapsed');
                panel.style.width = ''; // 让 collapsed 的 width:0 生效
            } else {
                panel.classList.remove('collapsed');
                panel.style.width = (resizedWidth || 600) + 'px';
            }
            toggle.setAttribute(
                'aria-label',
                collapsing ? '展开预览面板' : '收起预览面板'
            );
        });
    }

    // 左缘把手：自由左右延展面板宽度
    const handle = document.getElementById('previewResizeHandle');
    if (handle) {
        let resizeState = null;
        const MIN_W = 320;
        const maxWidth = () => Math.max(MIN_W, window.innerWidth - 320);

        const applyWidth = (clientX) => {
            if (!resizeState) return;
            // 把手在面板左缘：向左拖 = 面板左缘向左拉 = 变宽；向右拖 = 变窄
            let w = resizeState.startW - (clientX - resizeState.startX);
            w = Math.max(MIN_W, Math.min(maxWidth(), Math.round(w)));
            panel.style.width = w + 'px';
            resizedWidth = w;
        };
        const startResize = (clientX, ev) => {
            if (panel.classList.contains('collapsed')) return;
            if (ev && ev.cancelable) ev.preventDefault();
            resizeState = { startX: clientX, startW: panel.offsetWidth };
            handle.classList.add('dragging');
            document.body.classList.add('resizing-panel');
        };
        const onMove = (clientX) => {
            if (!resizeState) return;
            applyWidth(clientX);
        };
        const endResize = () => {
            if (!resizeState) return;
            resizeState = null;
            handle.classList.remove('dragging');
            document.body.classList.remove('resizing-panel');
        };

        handle.addEventListener('mousedown', (e) => {
            startResize(e.clientX, e);
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
        const onMouseMove = (e) => onMove(e.clientX);
        const onMouseUp = () => {
            endResize();
            document.removeEventListener('mousemove', onMouseMove);
            document.removeEventListener('mouseup', onMouseUp);
        };

        handle.addEventListener('touchstart', (e) => {
            startResize(e.touches[0].clientX, e);
        }, { passive: false });
        handle.addEventListener('touchmove', (e) => {
            onMove(e.touches[0].clientX);
        }, { passive: false });
        handle.addEventListener('touchend', endResize);
        handle.addEventListener('touchcancel', endResize);
    }

    // 复制代码
    const copyBtn = document.getElementById('codeCopyBtn');
    const codeEl = document.getElementById('codeContent');
    if (copyBtn && codeEl) {
        copyBtn.addEventListener('click', async () => {
            const text = codeEl.textContent || '';
            try {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    await navigator.clipboard.writeText(text);
                } else {
                    // 旧浏览器回退
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                }
                copyBtn.classList.add('copied');
                copyBtn.innerHTML = '<i class="fas fa-check"></i>';
                setTimeout(() => {
                    copyBtn.classList.remove('copied');
                    copyBtn.innerHTML = '<i class="fas fa-copy"></i>';
                }, 1500);
            } catch (e) {
                console.error('复制失败:', e);
            }
        });
    }

    // 收起按钮跟随面板左缘：面板宽度变化（拖拽/收起/展开/初始显示）时实时同步位置
    const mainEl = panel.parentElement;
    if (toggle && mainEl) {
        const syncTogglePos = () => {
            const mainRect = mainEl.getBoundingClientRect();
            const panelRect = panel.getBoundingClientRect();
            const bw = toggle.offsetWidth || 26;
            toggle.style.left = (panelRect.left - mainRect.left - bw) + 'px';
        };
        if (typeof ResizeObserver !== 'undefined') {
            const ro = new ResizeObserver(syncTogglePos);
            ro.observe(panel);
        }
        // 面板宽度过渡动画结束后精确同步一次（避免停在动画中间值）
        panel.addEventListener('transitionend', (e) => {
            if (e.propertyName === 'width') syncTogglePos();
        });
        syncTogglePos();
    }

}
