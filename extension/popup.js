// popup.js - 设置页面逻辑

const statusEl = document.getElementById('status');
const previewEl = document.getElementById('preview');
const marketCountEl = document.getElementById('market-count');

// 显示状态消息
function showStatus(message, type) {
  statusEl.textContent = message;
  statusEl.className = 'status ' + type;
}

// 加载已保存的配置
async function loadConfig() {
  // token/repo/path 用 sync（小数据跨设备同步）
  // marketsConfig 用 local（大数据本地存储）
  const syncResult = await chrome.storage.sync.get(['token', 'repo', 'path']);
  const localResult = await chrome.storage.local.get(['marketsConfig']);
  const result = { ...syncResult, ...localResult };

  if (result.token) {
    document.getElementById('token').value = result.token;
  }
  if (result.repo) {
    document.getElementById('repo').value = result.repo;
  }
  if (result.path) {
    document.getElementById('path').value = result.path;
  }

  // 显示已缓存的市场数据
  if (result.marketsConfig) {
    renderPreview(result.marketsConfig);
  }
}

// 渲染市场预览
function renderPreview(config) {
  if (!config || !config.markets) {
    previewEl.innerHTML = '<div style="color: #666;">暂无数据</div>';
    marketCountEl.textContent = '0';
    return;
  }

  marketCountEl.textContent = config.markets.length;

  const html = config.markets.map(m => `
    <div class="market">
      <div class="market-name">
        <span class="badge ${m.enabled ? 'badge-enabled' : 'badge-disabled'}">
          ${m.enabled ? 'ON' : 'OFF'}
        </span>
        ${m.name}
      </div>
      <div class="market-info">
        方向: ${m.trade_side.toUpperCase()} | 最大持仓: $${m.max_position_value}
      </div>
    </div>
  `).join('');

  previewEl.innerHTML = html;
}

// 从 GitHub 获取配置
async function fetchConfig() {
  const token = document.getElementById('token').value.trim();
  const repo = document.getElementById('repo').value.trim();
  const path = document.getElementById('path').value.trim();

  if (!token || !repo) {
    showStatus('请填写 Token 和仓库信息', 'error');
    return null;
  }

  showStatus('正在获取配置...', 'loading');

  try {
    const response = await fetch(
      `https://api.github.com/repos/${repo}/contents/${path}`,
      {
        headers: {
          'Authorization': `Bearer ${token}`,
          'Accept': 'application/vnd.github.v3.raw'
        }
      }
    );

    if (!response.ok) {
      if (response.status === 401) {
        throw new Error('Token 无效或已过期');
      } else if (response.status === 404) {
        throw new Error('找不到文件，请检查仓库和路径');
      } else {
        throw new Error(`请求失败: ${response.status}`);
      }
    }

    const config = await response.json();
    showStatus(`成功获取 ${config.markets?.length || 0} 个市场配置`, 'success');
    return config;
  } catch (error) {
    showStatus(error.message, 'error');
    return null;
  }
}

// 保存配置
document.getElementById('save').addEventListener('click', async () => {
  const token = document.getElementById('token').value.trim();
  const repo = document.getElementById('repo').value.trim();
  const path = document.getElementById('path').value.trim();

  // 保存设置
  await chrome.storage.sync.set({ token, repo, path });

  // 尝试获取并缓存配置
  const config = await fetchConfig();
  if (config) {
    await chrome.storage.local.set({ marketsConfig: config });
    renderPreview(config);

    // 通知所有 Polymarket 标签页刷新
    chrome.tabs.query({ url: 'https://polymarket.com/*' }, (tabs) => {
      tabs.forEach(tab => {
        chrome.tabs.sendMessage(tab.id, { type: 'CONFIG_UPDATED', config });
      });
    });
  }
});

// 刷新数据
document.getElementById('refresh').addEventListener('click', async () => {
  const config = await fetchConfig();
  if (config) {
    await chrome.storage.local.set({ marketsConfig: config });
    renderPreview(config);

    // 通知所有 Polymarket 标签页刷新
    chrome.tabs.query({ url: 'https://polymarket.com/*' }, (tabs) => {
      tabs.forEach(tab => {
        chrome.tabs.sendMessage(tab.id, { type: 'CONFIG_UPDATED', config });
      });
    });
  }
});

// 初始化
loadConfig();
