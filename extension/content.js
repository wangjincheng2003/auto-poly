// content.js - 在 Polymarket 页面显示配置信息

let marketsConfig = null;
let currentPanel = null;

// 等待页面数据加载完成
async function waitForPageData(maxWait = 5000) {
  const startTime = Date.now();

  while (Date.now() - startTime < maxWait) {
    // 检查 __NEXT_DATA__ 是否包含市场数据
    const nextDataEl = document.getElementById('__NEXT_DATA__');
    if (nextDataEl) {
      try {
        const data = JSON.parse(nextDataEl.textContent);
        const pageProps = data?.props?.pageProps;
        if (pageProps?.market || pageProps?.event || pageProps?.data?.market || pageProps?.data?.event) {
          return true;
        }
      } catch (e) {}
    }

    // 等待 200ms 后重试
    await new Promise(resolve => setTimeout(resolve, 200));
  }

  return false;
}

// 初始化
async function init() {
  // 从 storage 读取配置
  const result = await chrome.storage.local.get(['marketsConfig']);
  if (result.marketsConfig) {
    marketsConfig = result.marketsConfig;
  }

  // 等待页面数据加载完成后再显示面板
  await waitForPageData();
  checkAndShowPanel();

  // 尝试后台刷新（不阻塞显示）
  try {
    chrome.runtime.sendMessage({ type: 'REFRESH_CONFIG' }, (response) => {
      // 忽略错误（Service Worker 可能休眠）
      if (chrome.runtime.lastError) {
        console.log('Auto-Poly: Background unavailable, using cache');
        return;
      }
      if (response && response.config) {
        marketsConfig = response.config;
        checkAndShowPanel();
      }
    });
  } catch (e) {
    console.log('Auto-Poly: Using cached config');
  }
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

// 从页面获取 market_id
function getMarketIdFromPage() {
  try {
    // 方法1: 从 __NEXT_DATA__ 提取
    const nextDataEl = document.getElementById('__NEXT_DATA__');
    if (nextDataEl) {
      const data = JSON.parse(nextDataEl.textContent);
      const pageProps = data?.props?.pageProps;

      // 尝试不同的数据路径
      let market = pageProps?.market || pageProps?.data?.market;
      let event = pageProps?.event || pageProps?.data?.event;

      if (market?.conditionId) {
        return market.conditionId;
      }

      if (event?.markets?.[0]?.conditionId) {
        return event.markets[0].conditionId;
      }
    }

    // 方法2: 从页面 script 标签中查找
    const scripts = document.querySelectorAll('script');
    for (const script of scripts) {
      const text = script.textContent || '';
      const match = text.match(/"conditionId"\s*:\s*"(0x[a-f0-9]+)"/i);
      if (match) {
        return match[1];
      }
    }

    return null;
  } catch (e) {
    console.error('Auto-Poly: Failed to get market_id', e);
    return null;
  }
}

// 精确匹配市场（使用 market_id）
function findMatchingMarket() {
  if (!marketsConfig || !marketsConfig.markets) return null;

  const pageMarketId = getMarketIdFromPage();
  console.log('Auto-Poly: Page market_id:', pageMarketId);

  if (!pageMarketId) {
    return null;
  }

  // 精确匹配 market_id
  for (const market of marketsConfig.markets) {
    if (market.market_id === pageMarketId) {
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

  // 点击面板刷新（直接从 storage 读取）
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
new MutationObserver(async () => {
  const url = location.href;
  if (url !== lastUrl) {
    lastUrl = url;
    // 等待页面数据加载完成
    await waitForPageData();
    checkAndShowPanel();
  }
}).observe(document, { subtree: true, childList: true });

// 页面加载完成后初始化
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
