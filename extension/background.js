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
    console.log('Auto-Poly BG: Received ADD_MARKET', message.market);
    addMarketToGitHub(message.market).then(result => {
      console.log('Auto-Poly BG: Result', result);
      sendResponse(result);
    }).catch(err => {
      console.error('Auto-Poly BG: Error', err);
      sendResponse({ success: false, error: err.message });
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
    // 1. 从 Polymarket API 获取完整市场信息
    console.log('Auto-Poly BG: Fetching market from API, slug:', marketInfo.slug);
    const apiResponse = await fetch(
      `https://gamma-api.polymarket.com/markets/slug/${marketInfo.slug}`,
      { headers: { 'Accept': 'application/json' } }
    );

    if (!apiResponse.ok) {
      return { success: false, error: 'API fetch failed' };
    }

    const apiData = await apiResponse.json();
    console.log('Auto-Poly BG: API data:', apiData);

    // 解析市场数据
    const clobTokenIds = JSON.parse(apiData.clobTokenIds);
    const fullMarketInfo = {
      name: apiData.question,
      market_id: apiData.conditionId,
      yes_token_id: clobTokenIds[0],
      no_token_id: clobTokenIds[1]
    };

    // 2. 获取当前 GitHub 文件内容
    console.log('Auto-Poly BG: Fetching GitHub file...');

    // 先获取文件 metadata (包含 SHA)
    const metaResponse = await fetch(
      `https://api.github.com/repos/${repo}/contents/${path}`,
      {
        headers: {
          'Authorization': `Bearer ${token}`,
          'Accept': 'application/vnd.github.v3+json'
        }
      }
    );

    if (!metaResponse.ok) {
      return { success: false, error: 'GitHub fetch failed' };
    }

    const metaData = await metaResponse.json();
    console.log('Auto-Poly BG: GitHub meta:', metaData.sha ? 'has sha' : 'no sha');

    let currentContent;
    let sha;

    if (metaData.content && metaData.sha) {
      // 标准响应：包含 base64 content 和 sha
      const cleanBase64 = metaData.content.replace(/\s/g, '');
      currentContent = JSON.parse(atob(cleanBase64));
      sha = metaData.sha;
    } else if (metaData.sha) {
      // 只有 sha，需要单独获取内容
      const rawResponse = await fetch(
        `https://api.github.com/repos/${repo}/contents/${path}`,
        {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Accept': 'application/vnd.github.v3.raw'
          }
        }
      );
      currentContent = await rawResponse.json();
      sha = metaData.sha;
    } else if (metaData.markets) {
      // 直接返回了文件内容（无 sha），需要单独获取 sha
      currentContent = metaData;
      const shaResponse = await fetch(
        `https://api.github.com/repos/${repo}/commits?path=${path}&per_page=1`,
        {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Accept': 'application/vnd.github.v3+json'
          }
        }
      );
      const commits = await shaResponse.json();
      // 获取文件的 blob sha
      const treeResponse = await fetch(
        `https://api.github.com/repos/${repo}/git/trees/${commits[0].sha}`,
        {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Accept': 'application/vnd.github.v3+json'
          }
        }
      );
      const tree = await treeResponse.json();
      const fileEntry = tree.tree.find(t => t.path === path);
      sha = fileEntry?.sha;

      if (!sha) {
        return { success: false, error: 'Cannot get SHA' };
      }
    } else {
      console.error('Auto-Poly BG: Unknown response format', metaData);
      return { success: false, error: 'Unknown format' };
    }

    console.log('Auto-Poly BG: Got SHA:', sha?.substring(0, 8));

    // 3. 检查是否已存在
    const exists = currentContent.markets.some(m => m.market_id === fullMarketInfo.market_id);
    if (exists) {
      return { success: false, error: 'Already exists' };
    }

    // 4. 添加新市场（默认 enabled: true, trade_side: no, max: 30）
    const newMarket = {
      enabled: true,
      name: fullMarketInfo.name,
      market_id: fullMarketInfo.market_id,
      yes_token_id: fullMarketInfo.yes_token_id,
      no_token_id: fullMarketInfo.no_token_id,
      trade_side: "no",
      max_position_value: 30.0
    };

    currentContent.markets.push(newMarket);

    // 5. 更新 GitHub 文件
    console.log('Auto-Poly BG: Updating GitHub file...');
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
          message: `Add market: ${fullMarketInfo.name}`,
          content: btoa(unescape(encodeURIComponent(JSON.stringify(currentContent, null, 2)))),
          sha: sha
        })
      }
    );

    if (!updateResponse.ok) {
      const err = await updateResponse.json();
      return { success: false, error: err.message || 'Update failed' };
    }

    // 6. 更新本地缓存
    await chrome.storage.local.set({ marketsConfig: currentContent });

    console.log('Auto-Poly BG: Success!');
    return { success: true };
  } catch (error) {
    console.error('Auto-Poly BG: Add market failed', error);
    return { success: false, error: error.message };
  }
}
