// 供应商预设：仿 CC Switch 的「选供应商就自动填好地址与模型」。
// 只预填接口地址、凭据变量名和模型名，不预填价格——单价随供应商调价变动，
// 写死会直接污染成本核算，必须由管理员按官网当前定价确认。

export type ModelPreset = {
  id: string;
  /** 展示名，例如「智谱 GLM」 */
  name: string;
  /** 简介，一行说清适合什么场景 */
  hint: string;
  /** 首页地址留空表示自定义 */
  baseUrl: string;
  apiKeyEnv: string;
  models: string[];
  /** 品牌色，用于图标块 */
  accent: string;
  /** 图标块里的短标 */
  mark: string;
};

export const modelPresets: ModelPreset[] = [
  {
    id: "glm",
    name: "智谱 GLM",
    hint: "国内直连，glm-4-flash 免费额度适合跑量",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    apiKeyEnv: "TESTPILOT_GLM_API_KEY",
    models: ["glm-4.6", "glm-4-flash"],
    accent: "#3b5bdb",
    mark: "GLM",
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    hint: "性价比高，deepseek-chat 适合常规 Run Agent",
    baseUrl: "https://api.deepseek.com/v1",
    apiKeyEnv: "TESTPILOT_DEEPSEEK_API_KEY",
    models: ["deepseek-chat", "deepseek-reasoner"],
    accent: "#4c6ef5",
    mark: "DS",
  },
  {
    id: "kimi",
    name: "Kimi 开放平台",
    hint: "长上下文，适合大段需求的场景生成",
    baseUrl: "https://api.moonshot.cn/v1",
    apiKeyEnv: "TESTPILOT_KIMI_API_KEY",
    models: ["moonshot-v1-32k", "moonshot-v1-8k"],
    accent: "#0f8b8d",
    mark: "K",
  },
  {
    id: "openai",
    name: "OpenAI",
    hint: "官方接口，需要可用的网络出口",
    baseUrl: "https://api.openai.com/v1",
    apiKeyEnv: "TESTPILOT_OPENAI_API_KEY",
    models: ["gpt-4o-mini", "gpt-4o"],
    accent: "#0d3b66",
    mark: "AI",
  },
  {
    id: "qwen",
    name: "通义千问",
    hint: "DashScope 兼容 OpenAI 协议",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    apiKeyEnv: "TESTPILOT_QWEN_API_KEY",
    models: ["qwen-plus", "qwen-max"],
    accent: "#7048e8",
    mark: "QW",
  },
  {
    id: "custom",
    name: "自定义 / 中转",
    hint: "任何兼容 OpenAI Chat Completions 的地址",
    baseUrl: "",
    apiKeyEnv: "TESTPILOT_LLM_API_KEY",
    models: [],
    accent: "#5c6b82",
    mark: "自定",
  },
];

export type ProviderIdentity = { name: string; mark: string; accent: string; known: boolean };

/** 从接口地址反推供应商，用于在摘要卡上显示品牌名而不是一串 URL。 */
export function identifyProvider(baseUrl: string): ProviderIdentity {
  let host = "";
  try {
    host = new URL(baseUrl).hostname.toLowerCase();
  } catch {
    return { name: "未配置接口地址", mark: "?", accent: "#8b96a8", known: false };
  }
  const matched = modelPresets.find((preset) => {
    if (!preset.baseUrl) return false;
    try {
      return new URL(preset.baseUrl).hostname.toLowerCase() === host;
    } catch {
      return false;
    }
  });
  if (matched) {
    return { name: matched.name, mark: matched.mark, accent: matched.accent, known: true };
  }
  return { name: host, mark: host.slice(0, 2).toUpperCase(), accent: "#5c6b82", known: false };
}

/** 找出与当前配置同源（相同接口地址）的预设，用于回显选中态。 */
export function matchPreset(baseUrl: string): ModelPreset | undefined {
  let host = "";
  try {
    host = new URL(baseUrl).hostname.toLowerCase();
  } catch {
    return undefined;
  }
  return modelPresets.find((preset) => {
    if (!preset.baseUrl) return false;
    try {
      return new URL(preset.baseUrl).hostname.toLowerCase() === host;
    } catch {
      return false;
    }
  });
}
