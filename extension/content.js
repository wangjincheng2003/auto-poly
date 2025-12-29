// content.js - 在 Polymarket 页面显示配置信息

let marketsConfig = null;
let currentPanel = null;

// 初始化
async function init() {
  // 先用缓存的配置快速显示
  const result = await chrome.storage.local.get(['marketsConfig']);
  if (result.marketsConfig) {
    marketsConfig = result.marketsConfig;
    checkAndShowPanel();
  }

  // 然后请求 background 从 GitHub 刷新最新配置
  chrome.runtime.sendMessage({ type: 'REFRESH_CONFIG' }, (response) => {
    if (response && response.config) {
      marketsConfig = response.config;
      checkAndShowPanel();
    }
  });
}

// 监听配置更新消息
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'CONFIG_UPDATED') {
    marketsConfig = message.config;
    checkAndShowPanel();
  }
});

// 从 URL 提取 slug
function getSlugFromUrl() {
  const url = window.location.pathname;
  // /event/xxx 或 /market/xxx
  const match = url.match(/\/(event|market)\/([^\/\?]+)/);
  return match ? match[2] : null;
}

// 获取页面标题
function getPageTitle() {
  // 尝试获取市场标题
  const titleEl = document.querySelector('h1') || document.querySelector('[class*="title"]');
  return titleEl ? titleEl.textContent.trim() : document.title;
}

// 模糊匹配市场名称
function findMatchingMarket() {
  if (!marketsConfig || !marketsConfig.markets) return null;

  const slug = getSlugFromUrl();
  const pageTitle = getPageTitle().toLowerCase();

  for (const market of marketsConfig.markets) {
    const marketName = market.name.toLowerCase();

    // 方法1: slug 包含市场名称的关键词
    if (slug) {
      const slugWords = slug.replace(/-/g, ' ').toLowerCase();
      // 计算匹配度
      const marketWords = marketName.split(/\s+/);
      const matchedWords = marketWords.filter(word =>
        word.length > 3 && slugWords.includes(word)
      );
      if (matchedWords.length >= 2) {
        return market;
      }
    }

    // 方法2: 页面标题包含市场名称
    if (pageTitle.includes(marketName) || marketName.includes(pageTitle)) {
      return market;
    }

    // 方法3: 关键词匹配
    const keywords = marketName.split(/\s+/).filter(w => w.length > 3);
    const matchCount = keywords.filter(kw => pageTitle.includes(kw)).length;
    if (matchCount >= 3) {
      return market;
    }
  }

  return null;
}

// 创建显示面板
function createPanel(market) {
  // 移除旧面板
  if (currentPanel) {
    currentPanel.remove();
  }

  const panel = document.createElement('div');
  panel.id = 'auto-poly-panel';
  panel.className = 'auto-poly-panel';

  const statusText = market.enabled ? 'ON' : 'OFF';
  const statusClass = market.enabled ? 'enabled' : 'disabled';

  panel.innerHTML = `
    <div class="auto-poly-header">
      <span class="auto-poly-logo">AP</span>
      <span class="auto-poly-title">Auto-Poly</span>
      <button class="auto-poly-close" id="auto-poly-close">×</button>
    </div>
    <div class="auto-poly-content">
      <div class="auto-poly-row">
        <span class="auto-poly-label">Status</span>
        <span class="auto-poly-value auto-poly-status-${statusClass}">${statusText}</span>
      </div>
      <div class="auto-poly-row">
        <span class="auto-poly-label">Side</span>
        <span class="auto-poly-value auto-poly-side-${market.trade_side}">${market.trade_side.toUpperCase()}</span>
      </div>
      <div class="auto-poly-row">
        <span class="auto-poly-label">Max</span>
        <span class="auto-poly-value">$${market.max_position_value}</span>
      </div>
    </div>
    <div class="auto-poly-footer">Tap to refresh</div>
  `;

  document.body.appendChild(panel);
  currentPanel = panel;

  // 关闭按钮
  document.getElementById('auto-poly-close').addEventListener('click', (e) => {
    e.stopPropagation();
    panel.classList.add('auto-poly-hidden');
  });

  // 点击面板刷新
  panel.querySelector('.auto-poly-footer').addEventListener('click', async () => {
    const result = await chrome.storage.local.get(['marketsConfig']);
    if (result.marketsConfig) {
      marketsConfig = result.marketsConfig;
      checkAndShowPanel();
    }
  });

  // 拖拽功能
  makeDraggable(panel);
}

// 创建"未匹配"面板
function createNoMatchPanel() {
  if (currentPanel) {
    currentPanel.remove();
  }

  const panel = document.createElement('div');
  panel.id = 'auto-poly-panel';
  panel.className = 'auto-poly-panel auto-poly-no-match';

  panel.innerHTML = `
    <div class="auto-poly-header">
      <span class="auto-poly-logo">AP</span>
      <span class="auto-poly-title">Auto-Poly</span>
      <button class="auto-poly-close" id="auto-poly-close">×</button>
    </div>
    <div class="auto-poly-content">
      <div class="auto-poly-no-config">Not configured</div>
      <button class="auto-poly-add-btn" id="auto-poly-add">+ Add Market</button>
    </div>
  `;

  document.body.appendChild(panel);
  currentPanel = panel;

  document.getElementById('auto-poly-close').addEventListener('click', (e) => {
    e.stopPropagation();
    panel.classList.add('auto-poly-hidden');
  });

  // 添加市场按钮
  document.getElementById('auto-poly-add').addEventListener('click', async () => {
    const btn = document.getElementById('auto-poly-add');
    btn.textContent = 'Adding...';
    btn.disabled = true;

    console.log('Auto-Poly: Extracting market info...');
    const marketInfo = extractMarketInfo();
    console.log('Auto-Poly: Market info:', marketInfo);

    if (!marketInfo) {
      btn.textContent = 'Failed - No Data';
      setTimeout(() => {
        btn.textContent = '+ Add Market';
        btn.disabled = false;
      }, 2000);
      return;
    }

    // 发送给 background 处理
    console.log('Auto-Poly: Sending to background...');
    chrome.runtime.sendMessage({
      type: 'ADD_MARKET',
      market: marketInfo
    }, (response) => {
      console.log('Auto-Poly: Response:', response, chrome.runtime.lastError);
      if (chrome.runtime.lastError) {
        btn.textContent = 'Error';
        console.error('Auto-Poly:', chrome.runtime.lastError);
        setTimeout(() => {
          btn.textContent = '+ Add Market';
          btn.disabled = false;
        }, 2000);
        return;
      }
      if (response && response.success) {
        btn.textContent = 'Added!';
        btn.classList.add('auto-poly-add-success');
        // 刷新配置并显示
        setTimeout(() => {
          chrome.runtime.sendMessage({ type: 'REFRESH_CONFIG' }, (res) => {
            if (res && res.config) {
              marketsConfig = res.config;
              checkAndShowPanel();
            }
          });
        }, 1000);
      } else {
        btn.textContent = response?.error || 'Failed';
        setTimeout(() => {
          btn.textContent = '+ Add Market';
          btn.disabled = false;
        }, 2000);
      }
    });
  });

  makeDraggable(panel);
}

// 从页面提取市场信息
function extractMarketInfo() {
  // 从 URL 提取 slug
  const slug = getSlugFromUrl();
  if (!slug) {
    console.log('Auto-Poly: No slug found in URL');
    return null;
  }

  return { slug };
}

// 让面板可拖拽
function makeDraggable(panel) {
  const header = panel.querySelector('.auto-poly-header');
  let isDragging = false;
  let offsetX, offsetY;

  header.addEventListener('mousedown', (e) => {
    if (e.target.classList.contains('auto-poly-close')) return;
    isDragging = true;
    offsetX = e.clientX - panel.offsetLeft;
    offsetY = e.clientY - panel.offsetTop;
    panel.style.cursor = 'grabbing';
  });

  document.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    panel.style.left = (e.clientX - offsetX) + 'px';
    panel.style.top = (e.clientY - offsetY) + 'px';
    panel.style.right = 'auto';
  });

  document.addEventListener('mouseup', () => {
    isDragging = false;
    panel.style.cursor = '';
  });
}

// 检查并显示面板
function checkAndShowPanel() {
  // 只在 event 或 market 页面显示
  if (!window.location.pathname.match(/\/(event|market)\//)) {
    if (currentPanel) {
      currentPanel.remove();
      currentPanel = null;
    }
    return;
  }

  if (!marketsConfig) {
    createNoMatchPanel();
    return;
  }

  const market = findMatchingMarket();
  if (market) {
    createPanel(market);
  } else {
    createNoMatchPanel();
  }
}

// 监听 URL 变化 (SPA 应用)
let lastUrl = location.href;
new MutationObserver(() => {
  const url = location.href;
  if (url !== lastUrl) {
    lastUrl = url;
    setTimeout(checkAndShowPanel, 500); // 等待页面内容加载
  }
}).observe(document, { subtree: true, childList: true });

// 页面加载完成后初始化
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
