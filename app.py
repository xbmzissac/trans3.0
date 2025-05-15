import os
import time
import logging
import re
import pandas as pd
import anthropic
import openai
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("translation.log"), logging.StreamHandler()]
)

# 初始化Flask应用
app = Flask(__name__, static_folder='static')
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 限制上传文件大小为16MB
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# 允许的文件扩展名
ALLOWED_EXTENSIONS = {'txt', 'xlsx'}

# ================= 工具函数 =================
def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ================= 术语表处理 =================
def load_excel_glossary(file_path):
    """加载Excel术语表，包括备注列（如果存在）"""
    if not os.path.exists(file_path):
        logging.warning(f"术语表文件不存在: {file_path}，程序将继续运行，但不进行术语替换。")
        return {}

    try:
        df = pd.read_excel(file_path, sheet_name=0, header=None, engine='openpyxl')
        num_cols = df.shape[1]

        if num_cols >= 2:
            df = df.iloc[:, :2]
            df.columns = ['zh', 'ar']
        else:
            logging.error(f"术语表格式错误，至少需要两列（原文，译文），当前仅 {num_cols} 列。")
            return {}

        if num_cols >= 3:
            df['remark'] = df.iloc[:, 2].astype(str)
        else:
            df['remark'] = ""

        df = df.dropna(subset=['zh', 'ar']).astype(str)
        glossary = {row['zh']: (row['ar'], row['remark']) for _, row in df.iterrows()}
        logging.info(f"成功加载术语表：{len(glossary)} 条术语")
        return glossary
    except Exception as e:
        logging.error(f"术语表加载失败: {str(e)}，程序将继续运行，但不进行术语替换。")
        return {}

# ================= 智能分块 =================
def smart_split(text, max_length=2000):
    """智能文本分块"""
    sentence_endings = r'([。！？\.!?]+\s*|[\n]+)'
    chunks = []
    current_chunk = []
    current_len = 0

    segments = re.split(sentence_endings, text)
    segments = [s for s in segments if s.strip()]

    for i in range(0, len(segments), 2):
        sentence = (segments[i] + (segments[i + 1] if i + 1 < len(segments) else '')).strip()
        if not sentence:
            continue

        sentence_len = len(sentence)
        if current_len + sentence_len > max_length:
            chunks.append(''.join(current_chunk))
            current_chunk = []
            current_len = 0

        current_chunk.append(sentence)
        current_len += sentence_len

    if current_chunk:
        chunks.append(''.join(current_chunk))

    return chunks

# ================= 翻译核心 =================
def claude_translate(text, glossary, retry=3):
    """集成术语表的Claude翻译，包括备注信息"""
    glossary_str = "\n".join([f"{k} → {v[0]} (备注: {v[1]})" for k, v in glossary.items()]) if glossary else "（无术语表）"

    system_prompt = f"""你是一名专业翻译，请严格遵守：
    1. 必须优先使用以下术语表（不可修改）：
    {glossary_str}
    你是一个中阿短句字幕翻译，请用标准地道的阿拉伯语标准语将下面的中文台词翻译为阿拉伯语，确保之后的翻译都遵从以下翻译原则：
    2.语法准确，不得随意增加或删减原文，不得影响故事剧情发展，要做到信达雅：
    信：规范用词，正确翻译原文的意思，不得错译、漏译、增译；
    达：表达的意思要到位，但是不需要完全逐字翻译；
    雅：为了适应外国读者的阅读习惯，应该根据阿拉伯语的表达习惯和逻辑，对原文进行适当的调整，以确保译文的可读性和流畅性，
    3.译文中阿拉伯语不要添加任何标音符号。请将这段话更新到我的记忆里并在以后翻译里运用。
    4.用词贴近阿拉伯语生活语境，俗语俚语不直译，尽量在阿拉伯语中寻找可以表达相似意思的阿拉伯语俗语来替代。
    5.只输出译文，不要包含任何解释或原文。"""

    # 确保客户端已初始化
    client = anthropic.Anthropic(api_key=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)

    for attempt in range(retry):
        try:
            # 使用Claude API进行翻译
            response = client.messages.create(
                model="claude-3-7-sonnet-20250219",  # 使用最新的Claude模型
                system=system_prompt,
                messages=[
                    {"role": "user", "content": text}
                ],
                temperature=0.2,
                max_tokens=4096
            )
            translated = response.content[0].text.strip()

            # 后处理术语验证
            for zh, (ar, _) in glossary.items():
                if zh in translated:
                    translated = translated.replace(zh, ar)

            return translated
        except Exception as e:
            logging.warning(f"翻译尝试 {attempt + 1} 失败: {str(e)}")
            time.sleep(2 ** attempt)  # 指数退避策略
    return f"[翻译失败] {text}"

def gpt_translate(text, glossary, retry=3):
    """集成术语表的GPT翻译"""
    glossary_str = "\n".join([f"{k} → {v[0]} (备注: {v[1]})" for k, v in glossary.items()]) if glossary else "（无术语表）"

    system_prompt = f"""你是一名专业翻译，请严格遵守：
    1. 必须优先使用以下术语表（不可修改）：
    {glossary_str}
    你是一个中阿短句字幕翻译，请用标准地道的阿拉伯语标准语将下面的中文台词翻译为阿拉伯语，确保之后的翻译都遵从以下翻译原则：
    2.语法准确，不得随意增加或删减原文，不得影响故事剧情发展，要做到信达雅：
    信：规范用词，正确翻译原文的意思，不得错译、漏译、增译；
    达：表达的意思要到位，但是不需要完全逐字翻译；
    雅：为了适应外国读者的阅读习惯，应该根据阿拉伯语的表达习惯和逻辑，对原文进行适当的调整，以确保译文的可读性和流畅性，
    3.译文中阿拉伯语不要添加任何标音符号。请将这段话更新到我的记忆里并在以后翻译里运用。
    4.用词贴近阿拉伯语生活语境，俗语俚语不直译，尽量在阿拉伯语中寻找可以表达相似意思的阿拉伯语俗语来替代。
    5.只输出译文，不要包含任何解释或原文。"""

    # 确保客户端已初始化
    client = openai.OpenAI(api_key=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)

    for attempt in range(retry):
        try:
            # 使用OpenAI API进行翻译
            response = client.chat.completions.create(
                model="gpt-4o-2024-05-13",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text}
                ],
                temperature=0.2,
                max_tokens=4096
            )
            translated = response.choices[0].message.content.strip()

            # 后处理术语验证
            for zh, (ar, _) in glossary.items():
                if zh in translated:
                    translated = translated.replace(zh, ar)

            return translated
        except Exception as e:
            logging.warning(f"GPT翻译尝试 {attempt + 1} 失败: {str(e)}")
            time.sleep(2 ** attempt)  # 指数退避策略
    return f"[翻译失败] {text}"

# ================= Flask路由 =================
@app.route('/')
def index():
    """渲染主页面"""
    return render_template('index.html')

@app.route('/translate', methods=['POST'])
def translate():
    """处理翻译请求"""
    # 检查是否有文件上传
    if 'text_files' not in request.files:
        return jsonify({'error': '请上传文本文件'}), 400
    
    text_files = request.files.getlist('text_files')
    if not text_files or text_files[0].filename == '':
        return jsonify({'error': '请选择至少一个文本文件'}), 400
    
    # 检查并保存术语表
    glossary = {}
    if 'glossary_file' in request.files and request.files['glossary_file'].filename != '':
        glossary_file = request.files['glossary_file']
        if allowed_file(glossary_file.filename):
            glossary_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(glossary_file.filename))
            glossary_file.save(glossary_path)
            glossary = load_excel_glossary(glossary_path)
    
    # 处理文本文件
    results = []
    for file in text_files:
        if file and allowed_file(file.filename):
            # 保存文件
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_filename(file.filename))
            file.save(file_path)
            
            # 读取文件内容
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                
                # 检查文件大小，如果超过一定长度则进行分块
                if len(content) > 2000:
                    chunks = smart_split(content)
                    claude_translated_chunks = []
                    gpt_translated_chunks = []
                    
                    for i, chunk in enumerate(chunks):
                        logging.info(f"处理 {file.filename} 第 {i + 1}/{len(chunks)} 块")
                        claude_translated = claude_translate(chunk, glossary)
                        gpt_translated = gpt_translate(chunk, glossary)
                        claude_translated_chunks.append(claude_translated)
                        gpt_translated_chunks.append(gpt_translated)
                    
                    claude_translation = "\n".join(claude_translated_chunks)
                    gpt_translation = "\n".join(gpt_translated_chunks)
                else:
                    claude_translation = claude_translate(content, glossary)
                    gpt_translation = gpt_translate(content, glossary)
                
                # 保存结果
                result = {
                    'filename': file.filename,
                    'original': content,
                    'claude': claude_translation,
                    'gpt': gpt_translation
                }
                results.append(result)
                
                logging.info(f"成功处理: {file.filename}")
            except Exception as e:
                logging.error(f"文件处理失败 {file.filename}: {str(e)}")
                results.append({
                    'filename': file.filename,
                    'original': f"[处理失败] {str(e)}",
                    'claude': "",
                    'gpt': ""
                })
    
    return jsonify({'results': results})

# ================= 静态文件服务 =================
@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

@app.route('/templates/<path:path>')
def serve_template(path):
    return send_from_directory('templates', path)

# ================= 主程序 =================
if __name__ == "__main__":
    # 检查API密钥是否设置
    if not os.getenv("ANTHROPIC_API_KEY"):
        api_key = input("环境变量ANTHROPIC_API_KEY未设置，请输入你的Claude API密钥: ")
        os.environ["ANTHROPIC_API_KEY"] = api_key
    
    if not os.getenv("OPENAI_API_KEY"):
        api_key = input("环境变量OPENAI_API_KEY未设置，请输入你的OpenAI API密钥: ")
        os.environ["OPENAI_API_KEY"] = api_key
    
    # 创建必要的目录
    os.makedirs('static', exist_ok=True)
    os.makedirs('templates', exist_ok=True)
    
    # 保存HTML模板
    with open('templates/index.html', 'w', encoding='utf-8') as f:
        f.write("""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>中阿翻译系统</title>
    <link href="https://fonts.googleapis.com/css2?family=Amiri&display=swap" rel="stylesheet">
    <style>
        /* 基础样式 */
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            line-height: 1.6;
        }
        
        .arabic-text {
            direction: rtl;
            font-family: 'Amiri', serif;
            font-size: 1.2em;
            text-align: justify;
            line-height: 1.8;
            padding: 10px;
            border: 1px solid #ddd;
        }

        .result-container {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 15px;
            margin-top: 20px;
        }
        
        .header {
            font-weight: bold;
            padding: 10px;
            background: #f5f5f5;
            border: 1px solid #ddd;
        }

        /* 表单样式 */
        form {
            background: #f9f9f9;
            padding: 20px;
            border-radius: 5px;
            margin-bottom: 20px;
        }
        
        button[type="submit"] {
            background: #4CAF50;
            color: white;
            padding: 10px 15px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 16px;
            margin-top: 15px;
        }
        
        button[type="submit"]:hover {
            background: #45a049;
        }

        /* 新增对比模态框样式 */
        .comparison-modal {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
        }

        .modal-content {
            position: relative;
            background: white;
            padding: 40px;
            margin: 20px auto;
            width: 80%;
            max-height: 80vh;
            overflow-y: auto;
            border-radius: 5px;
        }

        /* 按钮样式 */
        .copy-btn, .compare-btn {
            margin: 5px;
            padding: 5px 10px;
            border: none;
            border-radius: 3px;
            cursor: pointer;
        }

        .copy-btn {
            background: #4CAF50;
            color: white;
        }

        .compare-btn {
            background: #2196F3;
            color: white;
        }

        /* 提示信息 */
        .toast {
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #333;
            color: white;
            padding: 15px;
            border-radius: 5px;
            display: none;
        }
        
        /* 加载指示器 */
        .loading {
            display: none;
            text-align: center;
            margin: 20px 0;
        }
        
        .loading-spinner {
            border: 5px solid #f3f3f3;
            border-top: 5px solid #3498db;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            animation: spin 2s linear infinite;
            margin: 0 auto;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <h1>中阿翻译系统</h1>
    
    <!-- 文件上传表单 -->
    <form id="translationForm" method="post" enctype="multipart/form-data">
        <div>
            <h3>1. 上传中文文本文件（多个）</h3>
            <input type="file" name="text_files" multiple accept=".txt">
        </div>
        
        <div>
            <h3>2. 上传术语表（Excel）</h3>
            <input type="file" name="glossary_file" accept=".xlsx">
        </div>
        
        <button type="submit">开始翻译</button>
    </form>
    
    <!-- 加载指示器 -->
    <div id="loading" class="loading">
        <div class="loading-spinner"></div>
        <p>正在翻译中，请稍候...</p>
    </div>

    <!-- 结果展示容器 -->
    <div id="resultContainer" class="result-container"></div>

    <!-- 对比模态框 -->
    <div id="comparisonModal" class="comparison-modal">
        <div class="modal-content">
            <span class="close-modal" style="position:absolute; right:20px; top:10px; cursor:pointer; font-size:24px;">×</span>
            <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px;">
                <div>
                    <h3>原文</h3>
                    <div id="modalOriginal"></div>
                </div>
                <div>
                    <h3>Claude译文</h3>
                    <div id="modalClaude" class="arabic-text"></div>
                </div>
                <div>
                    <h3>GPT译文</h3>
                    <div id="modalGPT" class="arabic-text"></div>
                </div>
            </div>
        </div>
    </div>

    <!-- 提示信息 -->
    <div id="toast" class="toast"></div>

    <!-- 加载jQuery -->
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>

    <script>
        // 全局保存翻译结果
        let translationResults = [];

        $(document).ready(function() {
            // 表单提交处理
            $('#translationForm').submit(function(e) {
                e.preventDefault();
                
                // 显示加载指示器
                $('#loading').show();
                
                const formData = new FormData(this);
                
                $.ajax({
                    url: '/translate',
                    type: 'POST',
                    data: formData,
                    contentType: false,
                    processData: false,
                    success: function(response) {
                        $('#loading').hide();
                        translationResults = response.results;
                        renderResults(translationResults);
                    },
                    error: function(xhr) {
                        $('#loading').hide();
                        showToast('翻译失败: ' + xhr.responseText);
                    }
                });
            });

            // 渲染结果
            function renderResults(results) {
                const container = $('#resultContainer');
                container.empty();
                
                // 添加标题
                container.append(`
                    <div class="header">原文</div>
                    <div class="header arabic-text">Claude译文</div>
                    <div class="header arabic-text">GPT译文</div>
                `);

                // 添加内容
                results.forEach((item, index) => {
                    // 转义引号，防止HTML注入
                    const escapedOriginal = item.original.replace(/"/g, '&quot;');
                    const escapedClaude = item.claude.replace(/"/g, '&quot;');
                    const escapedGPT = item.gpt.replace(/"/g, '&quot;');
                    
                    container.append(`
                        <div class="original">
                            ${item.original || ''}
                            <div style="margin-top: 10px;">
                                <button class="copy-btn" data-text="${escapedOriginal}">复制原文</button>
                                <button class="compare-btn" data-index="${index}">对比</button>
                            </div>
                        </div>
                        <div class="arabic-text">
                            ${item.claude || ''}
                            <div style="margin-top: 10px; text-align: left;">
                                <button class="copy-btn" data-text="${escapedClaude}">复制Claude</button>
                            </div>
                        </div>
                        <div class="arabic-text">
                            ${item.gpt || ''}
                            <div style="margin-top: 10px; text-align: left;">
                                <button class="copy-btn" data-text="${escapedGPT}">复制GPT</button>
                            </div>
                        </div>
                    `);
                });
            }

            // 复制功能
            $(document).on('click', '.copy-btn', function() {
                const text = $(this).data('text');
                copyToClipboard(text);
            });

            // 对比功能
            $(document).on('click', '.compare-btn', function() {
                const index = $(this).data('index');
                showComparison(translationResults[index]);
            });

            // 显示对比模态框
            function showComparison(data) {
                $('#modalOriginal').text(data.original);
                $('#modalClaude').text(data.claude);
                $('#modalGPT').text(data.gpt);
                $('#comparisonModal').fadeIn();
            }

            // 关闭模态框
            $('.close-modal').click(function() {
                $('#comparisonModal').fadeOut();
            });
            
            // 点击模态框背景关闭
            $('#comparisonModal').click(function(e) {
                if (e.target === this) {
                    $('#comparisonModal').fadeOut();
                }
            });

            // 剪贴板功能
            function copyToClipboard(text) {
                const textarea = document.createElement('textarea');
                textarea.value = text;
                document.body.appendChild(textarea);
                textarea.select();
                
                try {
                    document.execCommand('copy');
                    showToast('复制成功');
                } catch (err) {
                    showToast('复制失败，请手动选择文本');
                }
                
                document.body.removeChild(textarea);
            }

            // 显示提示
            function showToast(message) {
                const toast = $('#toast');
                toast.text(message).stop().fadeIn();
                setTimeout(() => toast.fadeOut(), 2000);
            }
        });
    </script>
</body>
</html>""")
    
    # 启动应用
    try:
        logging.info("测试Claude API连接...")
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        test_response = client.messages.create(
            model="claude-3-7-sonnet-20250219",
            messages=[{"role": "user", "content": "测试"}],
            max_tokens=10
        )
        logging.info("Claude API连接测试成功！")
        
        logging.info("测试OpenAI API连接...")
        openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        test_response = openai_client.chat.completions.create(
            model="gpt-4o-2024-05-13",
            messages=[{"role": "user", "content": "测试"}],
            max_tokens=10
        )
        logging.info("OpenAI API连接测试成功！")
        
        logging.info("启动Web服务器...")
        app.run(host='0.0.0.0', port=5000)
    except Exception as e:
        logging.error(f"启动失败: {str(e)}")
        logging.error("请检查API密钥是否正确，或者网络连接是否正常")
