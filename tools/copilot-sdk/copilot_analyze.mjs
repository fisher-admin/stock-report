#!/usr/bin/env node
/**
 * Copilot SDK 直调入口（选股 AI 专用）。
 *
 * stdin 读 prompt，调指定模型，模型返回的原始内容写 stdout。
 * 成功 exit 0；失败 exit 1，错误写 stderr（单行）。
 *
 * 用法：node copilot_analyze.mjs --model <id> --max-tokens <n> --timeout <s> < prompt.txt
 * 注意：SDK 1.0.31 未暴露 max-tokens 选项，--max-tokens 仅保留接口兼容，实际不传；
 * 提示词本身要求短 JSON 输出，实测单只约 500-1000 tokens。
 */
import { CopilotClient } from "@github/copilot-sdk";
import fs from "node:fs";

function parseArgs(argv) {
  const out = { model: "claude-haiku-5.5", maxTokens: 4500, timeout: 120 };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--model") out.model = argv[++i];
    else if (argv[i] === "--max-tokens") out.maxTokens = Number(argv[++i]) || 4500;
    else if (argv[i] === "--timeout") out.timeout = Number(argv[++i]) || 120;
  }
  return out;
}

const { model, timeout } = parseArgs(process.argv.slice(2));
const prompt = fs.readFileSync(0, "utf-8");
if (!prompt.trim()) {
  console.error("empty_prompt");
  process.exit(1);
}

const client = new CopilotClient();
const deadline = setTimeout(() => {
  console.error("sdk_timeout");
  process.exit(1);
}, Math.max(10, timeout) * 1000);

async function stopSoon() {
  try {
    await Promise.race([client.stop(), new Promise((r) => setTimeout(r, 5000))]);
  } catch {}
}

try {
  await client.start();
  const session = await client.createSession({ model });
  const res = await session.sendAndWait({ prompt });
  const content = res?.data?.content ?? "";
  clearTimeout(deadline);
  if (!content.trim()) {
    console.error("empty_response");
    await stopSoon();
    process.exit(1);
  }
  fs.writeSync(1, content);
  await stopSoon();
  process.exit(0);
} catch (err) {
  clearTimeout(deadline);
  await stopSoon();
  console.error(`sdk_failed: ${String(err?.message || err)}`.slice(0, 300));
  process.exit(1);
}
