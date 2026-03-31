# 智审法务 - 交互式 AI 合同审查平台

这是一个基于 Streamlit 的本地合同审查工具，支持：

- 合同正文审查与风险定位
- 审校模板库与自定义模板管理
- 扫描版 PDF / 图片 OCR
- 风险卡片、追问对话、修订建议应用
- DOCX 保格式导出

## 核心说明

- 项目要求使用 **Python 3.11 及以上**
- OCR 依赖为必选项，首次使用前请先初始化本地模型
- 扫描件 OCR 默认使用 **PaddleOCR 3.x + PP-OCRv5 mobile**
- 文本型 PDF 仍优先使用原生文本提取，不会强制走 OCR

## 环境要求

- Windows
- Python 3.11+

建议始终在同一个虚拟环境里执行以下命令：

```powershell
python -m pip install -r requirements.txt
python scripts/init_ocr.py
python -m streamlit run app.py
```

## 安装与启动

### 1. 安装依赖

```powershell
python -m pip install -r requirements.txt
```

`requirements.txt` 已包含：

- `paddlepaddle`
- `paddleocr==3.3.2`
- `streamlit`
- `openai`

### 2. 初始化 OCR

```powershell
python scripts/init_ocr.py
```

这个脚本会：

- 检查当前解释器是否为 Python 3.11+
- 预下载并预热本地 OCR 模型
- 避免用户第一次点击“开始审查”时现场下载模型

默认模型缓存目录：

```text
C:\Users\<你的用户名>\.paddlex\official_models
```

### 3. 启动应用

```powershell
python -m streamlit run app.py
```

默认访问地址通常是：

- `http://localhost:8501`

## OCR 机制

当前 OCR 行为如下：

- `.docx`：使用 `python-docx`
- 文本型 `.pdf`：优先使用 `PyPDF2`
- 扫描型 `.pdf`：自动切换到 PaddleOCR
- `.png/.jpg/.jpeg`：直接使用 PaddleOCR

应用运行时不会再偷偷下载模型。

如果 OCR 尚未初始化，页面会直接提示先执行：

```powershell
python scripts/init_ocr.py
```

## API 配置

侧边栏支持以下模型来源：

- Anthropic
- OpenAI 兼容接口
- Ollama 本地模型

注意：

- “OpenAI” 这一栏实际是 **OpenAI 兼容接口**
- 如果你填的是 OpenAI 官方 Key，`API Base URL` 应改为：

```text
https://api.openai.com/v1
```

- 如果你使用 DeepSeek，则可继续使用对应的兼容地址

## 项目结构

- `app.py`：主入口与 Streamlit 页面
- `legal_review/ocr.py`：OCR 检查、初始化、加载与调用
- `legal_review/templates.py`：审校模板库
- `legal_review/prompts.py`：审查提示词构造
- `legal_review/review_html.py`：风险高亮与审查展示
- `scripts/init_ocr.py`：OCR 初始化脚本

## 常见问题

### 1. 点击“开始审查”没反应

优先排查这几项：

- 是否用了同一个虚拟环境里的 `python` 启动 Streamlit
- 是否已经执行过 `python scripts/init_ocr.py`
- 是否填写了正确的 `API Base URL`
- 是否先用“直接粘贴合同文本”测试过 AI 调用链路

### 2. 为什么第一次 OCR 很慢

如果还没初始化 OCR，首次运行 `scripts/init_ocr.py` 需要下载并预热模型。这是一次性成本，之后会明显更快。

### 3. 为什么 `python scripts/init_ocr.py` 和页面表现不一致

通常是因为机器上有多个 Python 环境。直接在同一个虚拟环境里使用以下命令即可避免混乱：

```powershell
python -m pip install -r requirements.txt
python scripts/init_ocr.py
python -m streamlit run app.py
```
