/**
 * PaperReader — Graduate Student Academic Paper Reading Tool
 * Features: Multi-color highlighting, annotations, notes panel, TOC,
 * search, dark mode, font size, language toggle, export, localStorage persistence
 */
(function () {
  'use strict';

  const COLORS = {
    yellow: { label: '核心重点', en: 'Key Finding',   bg: '#fff59d', border: '#f9a825', text: '#333' },
    blue:   { label: '研究方法', en: 'Methodology',   bg: '#b3e5fc', border: '#0288d1', text: '#01579b' },
    pink:   { label: '重要引用', en: 'Key Quote',     bg: '#f8bbd0', border: '#e91e63', text: '#880e4f' },
    green:  { label: '我的理解', en: 'My Notes',      bg: '#c8e6c9', border: '#43a047', text: '#1b5e20' },
  };

  let paperId = '';
  let data = null; // { highlights: [], settings: {} }
  let activeColor = 'yellow';
  let pendingRange = null;
  let editingHlId = null;

  // ─── Data persistence ───────────────────────────────────────────────────────

  function loadData() {
    try {
      const raw = localStorage.getItem('pr2_' + paperId);
      if (raw) return JSON.parse(raw);
    } catch (e) {}
    return { highlights: [], settings: { dark: false, fontSize: 16, lang: 'both', focus: false } };
  }

  function saveData() {
    try { localStorage.setItem('pr2_' + paperId, JSON.stringify(data)); } catch (e) {}
  }

  // ─── Init ────────────────────────────────────────────────────────────────────

  function init() {
    paperId = document.body.getAttribute('data-paper-id') || 'paper';
    data = loadData();

    injectReaderUI();
    buildTOC();
    applySettings();
    restoreHighlights();
    bindEvents();
    updateProgress();
    calcStats();
  }

  // ─── UI Construction ─────────────────────────────────────────────────────────

  function injectReaderUI() {
    // Progress bar
    const prog = document.createElement('div');
    prog.id = 'rd-progress';
    document.body.prepend(prog);

    // Top control bar
    const bar = document.createElement('div');
    bar.id = 'rd-bar';
    bar.innerHTML = `
      <div class="rd-bar-left">
        <span id="rd-stats" title="字数 / 预计阅读时间"></span>
      </div>
      <div class="rd-bar-center">
        <div class="rd-lang-group" title="语言显示">
          <button class="rd-lang-btn active" data-lang="both">双语</button>
          <button class="rd-lang-btn" data-lang="zh">仅中文</button>
          <button class="rd-lang-btn" data-lang="en">仅英文</button>
        </div>
      </div>
      <div class="rd-bar-right">
        <button id="rd-toc-btn" title="目录 (T)">☰ 目录</button>
        <button id="rd-search-btn" title="搜索 (Ctrl+F)">🔍</button>
        <button id="rd-notes-btn" title="笔记本 (N)">📋 笔记</button>
        <button id="rd-export-btn" title="导出笔记">⬇ 导出</button>
        <span class="rd-sep"></span>
        <button id="rd-font-dec" title="缩小字号">A-</button>
        <button id="rd-font-inc" title="放大字号">A+</button>
        <button id="rd-dark-btn" title="深色模式 (D)">🌙</button>
        <button id="rd-focus-btn" title="专注模式 (F)">⬛</button>
      </div>
    `;
    document.body.insertBefore(bar, document.body.children[1]);

    // TOC sidebar
    const toc = document.createElement('div');
    toc.id = 'rd-toc';
    toc.innerHTML = `
      <div class="rd-panel-header">
        <span>📑 目录</span>
        <button class="rd-panel-close" data-panel="toc">✕</button>
      </div>
      <div id="rd-toc-list"></div>
    `;
    document.body.appendChild(toc);

    // Notes sidebar
    const notes = document.createElement('div');
    notes.id = 'rd-notes';
    notes.innerHTML = `
      <div class="rd-panel-header">
        <span>📋 我的笔记</span>
        <button class="rd-panel-close" data-panel="notes">✕</button>
      </div>
      <div id="rd-notes-empty" style="padding:1.2rem;color:#aaa;font-size:.85rem;">还没有笔记。<br>选中文字后点击颜色即可高亮，长按高亮文字可添加批注。</div>
      <div id="rd-notes-list"></div>
    `;
    document.body.appendChild(notes);

    // Search panel
    const search = document.createElement('div');
    search.id = 'rd-search-panel';
    search.innerHTML = `
      <input id="rd-search-input" placeholder="搜索关键词…" autocomplete="off">
      <span id="rd-search-count"></span>
      <button id="rd-search-prev">↑</button>
      <button id="rd-search-next">↓</button>
      <button id="rd-search-close">✕</button>
    `;
    document.body.appendChild(search);

    // Highlight color picker popup
    const popup = document.createElement('div');
    popup.id = 'rd-popup';
    popup.innerHTML = `
      <div class="rd-popup-label">高亮颜色</div>
      <div class="rd-color-row">
        ${Object.entries(COLORS).map(([k, v]) => `
          <button class="rd-color-btn" data-color="${k}" title="${v.label} · ${v.en}" style="background:${v.bg};border-color:${v.border}">
            <span>${v.label}</span>
          </button>
        `).join('')}
      </div>
      <button id="rd-popup-cancel">取消</button>
    `;
    document.body.appendChild(popup);

    // Note editor modal
    const noteModal = document.createElement('div');
    noteModal.id = 'rd-note-modal';
    noteModal.innerHTML = `
      <div id="rd-note-modal-inner">
        <div class="rd-modal-header">
          <span>✏️ 添加批注</span>
          <button id="rd-note-modal-close">✕</button>
        </div>
        <div id="rd-note-excerpt"></div>
        <textarea id="rd-note-input" placeholder="在这里写下你的理解、疑问或联想…" rows="5"></textarea>
        <div class="rd-modal-footer">
          <button id="rd-note-delete" class="rd-btn-danger">🗑 删除高亮</button>
          <button id="rd-note-save" class="rd-btn-primary">保存批注</button>
        </div>
      </div>
    `;
    document.body.appendChild(noteModal);

    // Overlay
    const overlay = document.createElement('div');
    overlay.id = 'rd-overlay';
    document.body.appendChild(overlay);
  }

  // ─── TOC ─────────────────────────────────────────────────────────────────────

  function buildTOC() {
    const sections = document.querySelectorAll('.section-title, .abstract-title');
    const list = document.getElementById('rd-toc-list');
    if (!list) return;
    sections.forEach((el, i) => {
      if (!el.id) el.id = 'section-' + i;
      const a = document.createElement('a');
      a.href = '#' + el.id;
      a.textContent = el.textContent.replace(/·.*/, '').trim();
      a.className = el.classList.contains('abstract-title') ? 'toc-abstract' : 'toc-section';
      a.addEventListener('click', (e) => {
        e.preventDefault();
        el.scrollIntoView({ behavior: 'smooth', block: 'start' });
        closePanels();
      });
      list.appendChild(a);
    });
  }

  // ─── Settings ────────────────────────────────────────────────────────────────

  function applySettings() {
    const s = data.settings;
    if (s.dark) enableDark();
    setFontSize(s.fontSize || 16, false);
    setLang(s.lang || 'both', false);
    if (s.focus) enableFocus();
    // Sync lang buttons
    document.querySelectorAll('.rd-lang-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.lang === (s.lang || 'both'));
    });
    // Sync dark button
    updateDarkBtn();
  }

  function enableDark() {
    document.body.classList.add('rd-dark');
    data.settings.dark = true;
  }
  function disableDark() {
    document.body.classList.remove('rd-dark');
    data.settings.dark = false;
  }
  function toggleDark() {
    data.settings.dark ? disableDark() : enableDark();
    updateDarkBtn();
    saveData();
  }
  function updateDarkBtn() {
    const btn = document.getElementById('rd-dark-btn');
    if (btn) btn.textContent = data.settings.dark ? '☀️' : '🌙';
  }

  function setFontSize(size, save = true) {
    size = Math.max(12, Math.min(22, size));
    data.settings.fontSize = size;
    document.querySelectorAll('.lang-col').forEach(el => el.style.fontSize = size + 'px');
    if (save) saveData();
  }

  function setLang(lang, save = true) {
    data.settings.lang = lang;
    const cols = document.querySelectorAll('.bilingual-cols');
    cols.forEach(col => {
      col.setAttribute('data-lang', lang);
    });
    document.querySelectorAll('.lang-col.en').forEach(el => {
      el.style.display = (lang === 'zh') ? 'none' : '';
    });
    document.querySelectorAll('.lang-col.zh').forEach(el => {
      el.style.display = (lang === 'en') ? 'none' : '';
    });
    // Update buttons
    document.querySelectorAll('.rd-lang-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.lang === lang);
    });
    if (save) saveData();
  }

  function enableFocus() {
    document.body.classList.add('rd-focus');
    data.settings.focus = true;
    const btn = document.getElementById('rd-focus-btn');
    if (btn) { btn.textContent = '⬜'; btn.title = '退出专注模式 (F)'; }
  }
  function disableFocus() {
    document.body.classList.remove('rd-focus');
    data.settings.focus = false;
    const btn = document.getElementById('rd-focus-btn');
    if (btn) { btn.textContent = '⬛'; btn.title = '专注模式 (F)'; }
  }
  function toggleFocus() {
    data.settings.focus ? disableFocus() : enableFocus();
    saveData();
  }

  // ─── Progress & Stats ────────────────────────────────────────────────────────

  function updateProgress() {
    const bar = document.getElementById('rd-progress');
    if (!bar) return;
    const scrollTop = window.scrollY;
    const docH = document.documentElement.scrollHeight - window.innerHeight;
    bar.style.width = (docH > 0 ? (scrollTop / docH) * 100 : 0) + '%';
  }

  function calcStats() {
    const el = document.getElementById('rd-stats');
    if (!el) return;
    const text = document.querySelector('.container')?.innerText || '';
    const words = text.split(/\s+/).filter(Boolean).length;
    const mins = Math.ceil(words / 250);
    el.textContent = `约 ${words.toLocaleString()} 词 · 预计 ${mins} 分钟`;
  }

  // ─── Highlighting ─────────────────────────────────────────────────────────────

  function showPopup(x, y) {
    const popup = document.getElementById('rd-popup');
    if (!popup) return;
    popup.style.display = 'block';
    // Position near selection
    const pw = 280, ph = 90;
    let left = x - pw / 2;
    let top = y - ph - 12;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    top = Math.max(60, top);
    popup.style.left = left + 'px';
    popup.style.top = (top + window.scrollY) + 'px';
    // Highlight active color button
    document.querySelectorAll('.rd-color-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.color === activeColor);
    });
  }

  function hidePopup() {
    const popup = document.getElementById('rd-popup');
    if (popup) popup.style.display = 'none';
    pendingRange = null;
  }

  function applyHighlight(color) {
    if (!pendingRange) return;
    const range = pendingRange;
    const text = range.toString().trim();
    if (!text || text.length < 2) { hidePopup(); return; }

    const id = 'hl_' + Date.now() + '_' + Math.random().toString(36).slice(2, 7);
    const mark = document.createElement('mark');
    mark.className = 'rd-hl rd-hl-' + color;
    mark.dataset.hlId = id;

    try {
      range.surroundContents(mark);
    } catch (e) {
      // Range crosses element boundaries — extract and wrap
      try {
        const frag = range.extractContents();
        mark.appendChild(frag);
        range.insertNode(mark);
      } catch (e2) {
        hidePopup();
        return;
      }
    }

    // Store
    const hl = { id, color, text, note: '', timestamp: Date.now() };
    data.highlights.push(hl);
    saveData();
    updateNotesPanel();
    hidePopup();
    window.getSelection()?.removeAllRanges();

    // Bind click to open note editor
    mark.addEventListener('click', () => openNoteEditor(id));
  }

  function removeHighlight(id) {
    const mark = document.querySelector(`mark[data-hl-id="${id}"]`);
    if (mark) {
      const parent = mark.parentNode;
      while (mark.firstChild) parent.insertBefore(mark.firstChild, mark);
      parent.removeChild(mark);
      parent.normalize();
    }
    data.highlights = data.highlights.filter(h => h.id !== id);
    saveData();
    updateNotesPanel();
  }

  function restoreHighlights() {
    if (!data.highlights.length) return;
    data.highlights.forEach(hl => {
      restoreSingleHighlight(hl);
    });
    updateNotesPanel();
  }

  function restoreSingleHighlight(hl) {
    // Find the text node containing hl.text using TreeWalker
    const root = document.querySelector('.container');
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      // Skip already inside a mark
      if (node.parentElement.classList.contains('rd-hl')) continue;
      const idx = node.textContent.indexOf(hl.text);
      if (idx === -1) continue;
      // Found it — wrap
      const range = document.createRange();
      range.setStart(node, idx);
      range.setEnd(node, idx + hl.text.length);
      const mark = document.createElement('mark');
      mark.className = 'rd-hl rd-hl-' + hl.color;
      mark.dataset.hlId = hl.id;
      try {
        range.surroundContents(mark);
        mark.addEventListener('click', () => openNoteEditor(hl.id));
        return; // done
      } catch (e) {
        // Skip if DOM structure is complex
      }
    }
  }

  // ─── Note Editor ─────────────────────────────────────────────────────────────

  function openNoteEditor(id) {
    const hl = data.highlights.find(h => h.id === id);
    if (!hl) return;
    editingHlId = id;
    const modal = document.getElementById('rd-note-modal');
    const excerpt = document.getElementById('rd-note-excerpt');
    const input = document.getElementById('rd-note-input');
    if (!modal || !excerpt || !input) return;
    excerpt.innerHTML = `<mark class="rd-hl rd-hl-${hl.color}" style="display:inline">${hl.text.length > 120 ? hl.text.slice(0, 120) + '…' : hl.text}</mark>`;
    input.value = hl.note || '';
    modal.style.display = 'flex';
    document.getElementById('rd-overlay').style.display = 'block';
    input.focus();
  }

  function saveNote() {
    if (!editingHlId) return;
    const input = document.getElementById('rd-note-input');
    const hl = data.highlights.find(h => h.id === editingHlId);
    if (hl && input) {
      hl.note = input.value.trim();
      saveData();
      updateNotesPanel();
    }
    closeNoteEditor();
  }

  function closeNoteEditor() {
    const modal = document.getElementById('rd-note-modal');
    if (modal) modal.style.display = 'none';
    document.getElementById('rd-overlay').style.display = 'none';
    editingHlId = null;
  }

  // ─── Notes Panel ─────────────────────────────────────────────────────────────

  function updateNotesPanel() {
    const list = document.getElementById('rd-notes-list');
    const empty = document.getElementById('rd-notes-empty');
    if (!list) return;
    list.innerHTML = '';
    if (data.highlights.length === 0) {
      if (empty) empty.style.display = 'block';
      return;
    }
    if (empty) empty.style.display = 'none';

    // Sort by DOM position
    const sorted = [...data.highlights].sort((a, b) => {
      const ma = document.querySelector(`mark[data-hl-id="${a.id}"]`);
      const mb = document.querySelector(`mark[data-hl-id="${b.id}"]`);
      if (!ma || !mb) return 0;
      const pos = ma.compareDocumentPosition(mb);
      return pos & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });

    sorted.forEach(hl => {
      const c = COLORS[hl.color] || COLORS.yellow;
      const item = document.createElement('div');
      item.className = 'rd-note-item';
      item.innerHTML = `
        <div class="rd-note-color-tag" style="background:${c.bg};border-left:3px solid ${c.border}">
          <span class="rd-note-label">${c.label}</span>
          <span class="rd-note-text">"${hl.text.length > 80 ? hl.text.slice(0, 80) + '…' : hl.text}"</span>
        </div>
        ${hl.note ? `<div class="rd-note-annotation">✏️ ${hl.note}</div>` : ''}
        <div class="rd-note-actions">
          <button class="rd-note-goto" data-id="${hl.id}">↗ 跳转</button>
          <button class="rd-note-edit" data-id="${hl.id}">✏️ 批注</button>
          <button class="rd-note-del" data-id="${hl.id}">🗑</button>
        </div>
      `;
      list.appendChild(item);
    });

    // Bind note item actions
    list.querySelectorAll('.rd-note-goto').forEach(btn => {
      btn.addEventListener('click', () => {
        const mark = document.querySelector(`mark[data-hl-id="${btn.dataset.id}"]`);
        if (mark) { mark.scrollIntoView({ behavior: 'smooth', block: 'center' }); flashMark(mark); }
      });
    });
    list.querySelectorAll('.rd-note-edit').forEach(btn => {
      btn.addEventListener('click', () => openNoteEditor(btn.dataset.id));
    });
    list.querySelectorAll('.rd-note-del').forEach(btn => {
      btn.addEventListener('click', () => { if (confirm('删除此高亮？')) removeHighlight(btn.dataset.id); });
    });
  }

  function flashMark(el) {
    el.classList.add('rd-hl-flash');
    setTimeout(() => el.classList.remove('rd-hl-flash'), 1200);
  }

  // ─── Export ───────────────────────────────────────────────────────────────────

  function exportNotes() {
    if (data.highlights.length === 0) { alert('还没有笔记哦！先选中文字高亮吧。'); return; }
    const title = document.querySelector('.paper-hero h1')?.textContent || paperId;
    const lines = ['# ' + title, '', '## 我的阅读笔记', '', `*导出时间：${new Date().toLocaleString('zh-CN')}*`, ''];

    const colorOrder = ['yellow', 'blue', 'pink', 'green'];
    colorOrder.forEach(color => {
      const hls = data.highlights.filter(h => h.color === color);
      if (hls.length === 0) return;
      const c = COLORS[color];
      lines.push(`### ${c.label} (${c.en})`);
      hls.forEach(hl => {
        lines.push(`> ${hl.text}`);
        if (hl.note) lines.push(`> ✏️ **批注：** ${hl.note}`);
        lines.push('');
      });
    });

    const md = lines.join('\n');
    // Copy to clipboard
    navigator.clipboard.writeText(md).then(() => {
      alert(`✅ 已复制到剪贴板！\n共 ${data.highlights.length} 条笔记。\n\n你也可以粘贴到任何 Markdown 编辑器中。`);
    }).catch(() => {
      // Fallback: download file
      const blob = new Blob([md], { type: 'text/markdown' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = paperId + '_notes.md';
      a.click();
    });
  }

  // ─── Search ───────────────────────────────────────────────────────────────────

  let searchMatches = [];
  let searchIdx = -1;
  let originalHTML = null;

  function openSearch() {
    const panel = document.getElementById('rd-search-panel');
    if (panel) { panel.style.display = 'flex'; document.getElementById('rd-search-input')?.focus(); }
  }
  function closeSearch() {
    const panel = document.getElementById('rd-search-panel');
    if (panel) panel.style.display = 'none';
    clearSearch();
  }
  function clearSearch() {
    // Remove search highlights by restoring original... actually use a different approach
    document.querySelectorAll('.rd-search-hl').forEach(el => {
      const t = document.createTextNode(el.textContent);
      el.parentNode.replaceChild(t, el);
    });
    document.querySelectorAll('.lang-col p, .lang-col li, .lang-col h4').forEach(el => el.normalize());
    searchMatches = [];
    searchIdx = -1;
    const cnt = document.getElementById('rd-search-count');
    if (cnt) cnt.textContent = '';
  }

  function doSearch(q) {
    clearSearch();
    if (!q || q.length < 2) return;
    const root = document.querySelector('.container');
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    const matches = [];
    while ((node = walker.nextNode())) {
      if (node.parentElement.classList.contains('rd-hl') || node.parentElement.classList.contains('rd-search-hl')) continue;
      let text = node.textContent;
      let startIdx = 0;
      const lc = text.toLowerCase();
      const qlc = q.toLowerCase();
      let idx;
      while ((idx = lc.indexOf(qlc, startIdx)) !== -1) {
        matches.push({ node, start: idx, end: idx + q.length });
        startIdx = idx + q.length;
      }
    }

    // Wrap matches (in reverse to preserve offsets)
    const uniqueNodes = [...new Set(matches.map(m => m.node))].reverse();
    uniqueNodes.forEach(node => {
      const nodeMatches = matches.filter(m => m.node === node).reverse();
      nodeMatches.forEach(m => {
        const range = document.createRange();
        range.setStart(node, m.start);
        range.setEnd(node, m.end);
        const span = document.createElement('span');
        span.className = 'rd-search-hl';
        try { range.surroundContents(span); } catch (e) {}
      });
    });

    searchMatches = [...document.querySelectorAll('.rd-search-hl')];
    const cnt = document.getElementById('rd-search-count');
    if (cnt) cnt.textContent = searchMatches.length > 0 ? `${searchMatches.length} 处` : '未找到';
    if (searchMatches.length > 0) { searchIdx = 0; scrollToMatch(0); }
  }

  function scrollToMatch(i) {
    searchMatches.forEach((el, j) => el.classList.toggle('rd-search-current', j === i));
    if (searchMatches[i]) searchMatches[i].scrollIntoView({ behavior: 'smooth', block: 'center' });
    const cnt = document.getElementById('rd-search-count');
    if (cnt && searchMatches.length > 0) cnt.textContent = `${i + 1} / ${searchMatches.length}`;
  }

  // ─── Panel management ────────────────────────────────────────────────────────

  function toggleTOC() {
    const t = document.getElementById('rd-toc');
    const n = document.getElementById('rd-notes');
    if (n) n.classList.remove('open');
    if (t) t.classList.toggle('open');
  }
  function toggleNotes() {
    const t = document.getElementById('rd-toc');
    const n = document.getElementById('rd-notes');
    if (t) t.classList.remove('open');
    if (n) { n.classList.toggle('open'); if (n.classList.contains('open')) updateNotesPanel(); }
  }
  function closePanels() {
    document.getElementById('rd-toc')?.classList.remove('open');
    document.getElementById('rd-notes')?.classList.remove('open');
  }

  // ─── Event Binding ───────────────────────────────────────────────────────────

  function bindEvents() {
    // Scroll progress
    window.addEventListener('scroll', updateProgress, { passive: true });

    // Text selection in reading area
    document.addEventListener('mouseup', (e) => {
      if (e.target.closest('#rd-popup') || e.target.closest('#rd-bar') ||
          e.target.closest('#rd-toc') || e.target.closest('#rd-notes') ||
          e.target.closest('#rd-note-modal')) return;
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) {
        // Don't hide immediately if clicking inside popup
        setTimeout(() => {
          const sel2 = window.getSelection();
          if (!sel2 || sel2.isCollapsed) hidePopup();
        }, 150);
        return;
      }
      const range = sel.getRangeAt(0);
      // Only highlight inside .lang-col
      if (!range.commonAncestorContainer.parentElement?.closest('.lang-col') &&
          !range.commonAncestorContainer?.closest?.('.lang-col')) return;
      pendingRange = range.cloneRange();
      const rect = range.getBoundingClientRect();
      showPopup(rect.left + rect.width / 2, rect.top + window.scrollY);
    });

    // Color picker buttons
    document.querySelectorAll('.rd-color-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        activeColor = btn.dataset.color;
        applyHighlight(activeColor);
      });
    });
    document.getElementById('rd-popup-cancel')?.addEventListener('click', hidePopup);

    // Toolbar buttons
    document.getElementById('rd-toc-btn')?.addEventListener('click', toggleTOC);
    document.getElementById('rd-notes-btn')?.addEventListener('click', toggleNotes);
    document.getElementById('rd-search-btn')?.addEventListener('click', openSearch);
    document.getElementById('rd-export-btn')?.addEventListener('click', exportNotes);
    document.getElementById('rd-dark-btn')?.addEventListener('click', toggleDark);
    document.getElementById('rd-focus-btn')?.addEventListener('click', toggleFocus);
    document.getElementById('rd-font-dec')?.addEventListener('click', () => setFontSize(data.settings.fontSize - 1));
    document.getElementById('rd-font-inc')?.addEventListener('click', () => setFontSize(data.settings.fontSize + 1));

    // Lang buttons
    document.querySelectorAll('.rd-lang-btn').forEach(btn => {
      btn.addEventListener('click', () => setLang(btn.dataset.lang));
    });

    // Panel close buttons
    document.querySelectorAll('.rd-panel-close').forEach(btn => {
      btn.addEventListener('click', () => {
        const panel = btn.dataset.panel;
        if (panel === 'toc') document.getElementById('rd-toc')?.classList.remove('open');
        if (panel === 'notes') document.getElementById('rd-notes')?.classList.remove('open');
      });
    });

    // Note modal
    document.getElementById('rd-note-modal-close')?.addEventListener('click', closeNoteEditor);
    document.getElementById('rd-note-save')?.addEventListener('click', saveNote);
    document.getElementById('rd-note-delete')?.addEventListener('click', () => {
      if (editingHlId && confirm('删除此高亮？')) { removeHighlight(editingHlId); closeNoteEditor(); }
    });
    document.getElementById('rd-overlay')?.addEventListener('click', closeNoteEditor);

    // Note input — save on Ctrl+Enter
    document.getElementById('rd-note-input')?.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') saveNote();
    });

    // Search
    document.getElementById('rd-search-input')?.addEventListener('input', (e) => doSearch(e.target.value));
    document.getElementById('rd-search-prev')?.addEventListener('click', () => {
      if (searchMatches.length === 0) return;
      searchIdx = (searchIdx - 1 + searchMatches.length) % searchMatches.length;
      scrollToMatch(searchIdx);
    });
    document.getElementById('rd-search-next')?.addEventListener('click', () => {
      if (searchMatches.length === 0) return;
      searchIdx = (searchIdx + 1) % searchMatches.length;
      scrollToMatch(searchIdx);
    });
    document.getElementById('rd-search-close')?.addEventListener('click', closeSearch);

    // Keyboard shortcuts
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'TEXTAREA' || e.target.tagName === 'INPUT') return;
      if ((e.ctrlKey || e.metaKey) && e.key === 'f') { e.preventDefault(); openSearch(); return; }
      if (e.key === 'd' || e.key === 'D') toggleDark();
      if (e.key === 'f' || e.key === 'F') toggleFocus();
      if (e.key === 'n' || e.key === 'N') toggleNotes();
      if (e.key === 't' || e.key === 'T') toggleTOC();
      if (e.key === 'Escape') { closeSearch(); closePanels(); hidePopup(); closeNoteEditor(); }
    });

    // Click outside panels to close
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#rd-toc') && !e.target.closest('#rd-toc-btn')) {
        document.getElementById('rd-toc')?.classList.remove('open');
      }
    });
  }

  // ─── Boot ────────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', init);
})();
