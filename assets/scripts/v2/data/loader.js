// v2/data/loader.js — 按视图清单取数。
// 取数策略：先以 cache:'no-store' 拉取 4KB 的 run_manifest（唯一强制新鲜的文件），
// 拿到 run_id 后其余文件统一带 ?v=<run_id> 走浏览器默认缓存（替代 legacy 对 6.6MB 全量数据的 no-store）。

import { SOURCES, depsForView } from './manifest.js';
import { summarizeByKey } from './summarize.js';

// 本文件位于 assets/scripts/v2/data/，站点根目录在四级之上（../../../../）。
export function resolveUrl(relativePath, baseUrl) {
  return new URL(`../../../../${relativePath}`, baseUrl || import.meta.url);
}

async function fetchJson(url, { noStore = false, version = '' } = {}) {
  const target = new URL(url);
  if (noStore) {
    target.searchParams.set('ts', Date.now().toString());
  } else if (version) {
    target.searchParams.set('v', version);
  }
  const resp = await fetch(target, noStore ? { cache: 'no-store' } : undefined);
  if (!resp.ok) {
    const error = new Error(`HTTP ${resp.status}`);
    error.httpStatus = resp.status;
    throw error;
  }
  try {
    return await resp.json();
  } catch (parseError) {
    throw new Error(`JSON 解析失败（${parseError.message || parseError}）`);
  }
}

async function loadSource(key, { version, baseUrl }) {
  const spec = SOURCES[key];
  if (!spec) throw new Error(`未知数据源 ${key}`);
  try {
    return { doc: await fetchJson(resolveUrl(spec.path, baseUrl), { version }), reason: '' };
  } catch (primaryError) {
    if (spec.fallbackPath) {
      try {
        const doc = await fetchJson(resolveUrl(spec.fallbackPath, baseUrl), { version });
        return { doc: summarizeByKey(key, doc), reason: '' };
      } catch (fallbackError) {
        return { doc: null, reason: `${spec.path} 与回退文件均不可用（${fallbackError.message}）` };
      }
    }
    return { doc: null, reason: `${spec.path} 加载失败（${primaryError.message}）` };
  }
}

// 返回 { data: {key: doc|null}, missing: [{key,label,reason}], runId }
// required 数据源失败 → 抛错（由 app.js 渲染整页错误提示）。
export async function loadViewData(viewKey, baseUrl) {
  const deps = depsForView(viewKey);
  const data = {};
  const missing = [];

  // 1) run_manifest：唯一 no-store 的小文件，提供 run_id 作为缓存版本号。
  let runId = '';
  if (deps.required.includes('runManifest')) {
    try {
      data.runManifest = await fetchJson(resolveUrl(SOURCES.runManifest.path, baseUrl), { noStore: true });
      runId = String(data.runManifest?.run_id || data.runManifest?.generated_at || '');
    } catch (error) {
      throw new Error(`${SOURCES.runManifest.path} 加载失败（${error.message}）`);
    }
  }

  // 2) 其余 required + optional 并行加载，统一带版本参数。
  const rest = [
    ...deps.required.filter((key) => key !== 'runManifest'),
    ...deps.optional
  ];
  const results = await Promise.all(rest.map((key) => loadSource(key, { version: runId, baseUrl })));
  rest.forEach((key, idx) => {
    const { doc, reason } = results[idx];
    if (doc === null) {
      if (deps.required.includes(key)) {
        throw new Error(reason || `${SOURCES[key].path} 加载失败`);
      }
      missing.push({ key, label: SOURCES[key].label, reason: reason || '数据缺失' });
      data[key] = null;
    } else {
      data[key] = doc;
    }
  });

  return { data, missing, runId };
}
