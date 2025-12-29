// background.js - 后台服务

// 从 GitHub 获取配置
async function fetchConfigFromGitHub() {
  const result = await chrome.storage.sync.get(['token', 'repo', 'path']);

  if (!result.token || !result.repo) {
    console.log('Auto-Poly: 未配置 GitHub Token 或仓库');
    return null;
  }

  const path = result.path || 'markets_config.json';

  try {
    const response = await fetch(
      `https://api.github.com/repos/${result.repo}/contents/${path}`,
      {
        headers: {
          'Authorization': `Bearer ${result.token}`,
          'Accept': 'application/vnd.github.v3.raw'
        }
      }
    );

    if (!response.ok) {
      console.error('Auto-Poly: GitHub API 请求失败', response.status);
      return null;
    }

    const config = await response.json();
    return config;
  } catch (error) {
    console.error('Auto-Poly: 获取配置失败', error);
    return null;
  }
}

// 定期刷新配置 (每10分钟)
chrome.alarms.create('refreshConfig', { periodInMinutes: 10 });

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === 'refreshConfig') {
    console.log('Auto-Poly: 自动刷新配置');
    const config = await fetchConfigFromGitHub();
    if (config) {
      await chrome.storage.local.set({ marketsConfig: config });

      // 通知所有 Polymarket 标签页
      chrome.tabs.query({ url: 'https://polymarket.com/*' }, (tabs) => {
        tabs.forEach(tab => {
          chrome.tabs.sendMessage(tab.id, { type: 'CONFIG_UPDATED', config }).catch(() => {});
        });
      });
    }
  }
});

// 安装时初始化
chrome.runtime.onInstalled.addListener(async () => {
  console.log('Auto-Poly: 插件已安装');

  // 尝试获取配置
  const config = await fetchConfigFromGitHub();
  if (config) {
    await chrome.storage.local.set({ marketsConfig: config });
  }
});

// 监听来自 content script 的消息
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'GET_CONFIG') {
    chrome.storage.local.get(['marketsConfig']).then(result => {
      sendResponse(result.marketsConfig);
    });
    return true; // 异步响应
  }

  if (message.type === 'REFRESH_CONFIG') {
    // 页面加载时刷新配置
    fetchConfigFromGitHub().then(async (config) => {
      if (config) {
        await chrome.storage.local.set({ marketsConfig: config });
        sendResponse({ config });
      } else {
        // 刷新失败，返回缓存的配置
        const result = await chrome.storage.local.get(['marketsConfig']);
        sendResponse({ config: result.marketsConfig });
      }
    });
    return true; // 异步响应
  }

  if (message.type === 'ADD_MARKET') {
    // 添加新市场到 GitHub
    addMarketToGitHub(message.market).then(result => {
      sendResponse(result);
    });
    return true; // 异步响应
  }
});

// 添加市场到 GitHub 配置文件
async function addMarketToGitHub(marketInfo) {
  const syncResult = await chrome.storage.sync.get(['token', 'repo', 'path']);
  const { token, repo, path = 'markets_config.json' } = syncResult;

  if (!token || !repo) {
    return { success: false, error: 'No token' };
  }

  try {
    // 1. 获取当前文件内容和 SHA
    const getResponse = await fetch(
      `https://api.github.com/repos/${repo}/contents/${path}`,
      {
        headers: {
          'Authorization': `Bearer ${token}`,
          'Accept': 'application/vnd.github.v3+json'
        }
      }
    );

    if (!getResponse.ok) {
      return { success: false, error: 'Fetch failed' };
    }

    const fileData = await getResponse.json();
    const currentContent = JSON.parse(atob(fileData.content));
    const sha = fileData.sha;

    // 2. 检查是否已存在
    const exists = currentContent.markets.some(m => m.market_id === marketInfo.market_id);
    if (exists) {
      return { success: false, error: 'Already exists' };
    }

    // 3. 添加新市场（默认 enabled: true, trade_side: no, max: 30）
    const newMarket = {
      enabled: true,
      name: marketInfo.name,
      market_id: marketInfo.market_id,
      yes_token_id: marketInfo.yes_token_id,
      no_token_id: marketInfo.no_token_id,
      trade_side: "no",
      max_position_value: 30.0
    };

    currentContent.markets.push(newMarket);

    // 4. 更新文件
    const updateResponse = await fetch(
      `https://api.github.com/repos/${repo}/contents/${path}`,
      {
        method: 'PUT',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Accept': 'application/vnd.github.v3+json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          message: `Add market: ${marketInfo.name}`,
          content: btoa(unescape(encodeURIComponent(JSON.stringify(currentContent, null, 2)))),
          sha: sha
        })
      }
    );

    if (!updateResponse.ok) {
      const err = await updateResponse.json();
      return { success: false, error: err.message || 'Update failed' };
    }

    // 5. 更新本地缓存
    await chrome.storage.local.set({ marketsConfig: currentContent });

    return { success: true };
  } catch (error) {
    console.error('Auto-Poly: Add market failed', error);
    return { success: false, error: error.message };
  }
}
