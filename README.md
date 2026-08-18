# mini-agent

用 C 语言实现的最小 agent 框架，直接调用 **OpenAI 兼容 API**，仅依赖 libcurl（无其他第三方库，JSON 构造/解析为内置手写实现）。

## 特性

- **SSE 流式输出**：`stream = true` 时增量解析 `data:` 事件并实时打印；`stream = false` 时等整包返回
- **多模型配置**：通过 INI 风格配置文件任意增删模型，每个模型可独立配置：
  接入点 endpoint、api_key、上下文大小 context_size、温度 temperature、
  思考深度 thinking（映射为 `reasoning_effort`）、流式开关 stream、超时 timeout
- **系统提示词**：`-s` 从文件加载
- **批量请求**：`-r` 从文件加载多个用户请求（以连续 **3 个换行符** `\n\n\n` 分隔），
  按顺序作为一次多轮对话依次执行，自动维护上下文；估算超出 `context_size` 时自动丢弃最早的消息（system 始终保留）
- **思考内容分流**：模型返回的 `reasoning_content`（如 DeepSeek 思考过程）打印到 stderr，正式回答打印到 stdout，方便 `./mini-agent ... > answers.md` 只收集答案

## 构建

依赖：C 编译器 + libcurl（macOS 自带；Linux 上安装 `libcurl-dev` / `curl-devel`）。

```sh
make            # 生成 ./mini-agent
```

## 快速开始

```sh
# 1. 在 config.ini 中配置你的模型（api_key 推荐 env:变量名 形式）
export DEEPSEEK_API_KEY=sk-xxxx

# 2. 运行：加载系统提示词 + 批量请求
./mini-agent -c config.ini -s system_prompt.txt -r requests.txt

# 演练模式（不发网络请求，只打印将发送的 JSON，便于检查配置）
./mini-agent -c config.ini -s system_prompt.txt -r requests.txt -n

# 指定使用配置中的其他模型
./mini-agent -c config.ini -m gpt-4o -r requests.txt
```

### 命令行参数

| 参数 | 说明 |
| --- | --- |
| `-c FILE` | 模型配置文件（默认 `./config.ini`） |
| `-m NAME` | 本次使用的模型（默认取配置 `[default]` 的 `model`） |
| `-s FILE` | 系统提示词文件（可选） |
| `-r FILE` | 批量用户请求文件（必填） |
| `-n` | 演练模式：只打印请求 JSON，不发网络请求 |
| `-v` | 打印请求体等调试信息 |
| `-h` | 帮助 |

## 配置文件格式

```ini
[default]
model = deepseek-chat            # 不带 -m 时使用的默认模型

[model deepseek-chat]            # [model 名称] 定义一个模型，可写任意多个
endpoint = https://api.deepseek.com/chat/completions  # chat/completions 完整 URL
api_key = env:DEEPSEEK_API_KEY   # 直接写 key 或 env:变量名 从环境变量读取
model = deepseek-chat            # 发给 API 的 model 字段（缺省用节名）
context_size = 64000             # 上下文窗口(token)，超出自动裁剪历史；0=不限制
temperature = 0.7                # 采样温度；注释掉或设为负数则不发送
thinking = medium                # 思考深度，原样作为 reasoning_effort 发送；不用则注释
stream = true                    # true=SSE 流式，false=整包返回
timeout = 120                    # 单次请求超时（秒）
```

注意：

- `endpoint` 是 **chat/completions 的完整 URL**（任何 OpenAI 兼容服务均可，包括 vLLM / ollama 等本地服务）
- OpenAI o 系列等推理模型不接受 `temperature` 参数，使用时请将其注释掉
- `thinking` 的取值（low/medium/high 等）取决于服务商对 `reasoning_effort` 的支持

## 请求文件格式

请求之间用 **连续 3 个换行符**（`\n\n\n`，即两个空行）分隔，首尾空白会被自动去除：

```text
第一个请求的内容。


第二个请求的内容。
可以有多行，只要相邻空行不超过两个。


第三个请求的内容。
```

CRLF（Windows 换行）会自动归一化为 LF。

## 输出约定

- **stdout**：仅模型回答（流式实时输出），可直接重定向保存
- **stderr**：思考内容（`[thinking]` 前缀）、请求进度、上下文裁剪提示与错误信息

## 测试

```sh
make test        # 离线单元测试：请求切分 / JSON 解析与转义 / 上下文裁剪 / 请求体构造
make mock-test   # 本地联调：自动启动 python3 mock 服务，验证 SSE 流式与非流式两条链路（无需外网）
```

## 实现说明

- 单文件 `agent.c`（约 900 行，含注释）：动态缓冲区 → 微型 JSON 解析器 → 请求切分 → 对话历史与裁剪 → INI 配置 → 请求体构造 → libcurl 调用与 SSE 解析 → 主流程
- SSE 解析：libcurl 写回调中增量切行，逐行处理 `data:` 负载；JSON 字符串内不可能出现裸换行，故按行切分是安全的
- token 估算：按 3 字节/token（兼顾中英文）+ 每条消息固定开销，仅用于上下文裁剪
- `\uXXXX` 反转义支持 UTF-16 代理对，UTF-8 全程原样透传

扩展方向：在 `call_model` 之上加工具调用（function calling）循环、把 SSE 解析抽象为回调接口，即可演进为更完整的 agent 框架。
